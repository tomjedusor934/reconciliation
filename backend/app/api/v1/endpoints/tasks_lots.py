"""Internal movement-lot endpoints called by the ingest_finacle_bb DAG.

Auth via X-Internal-Token, like the other /tasks endpoints. Kept in a separate
module so tasks.py doesn't keep growing; mounted under the same /tasks prefix.

There is no key-map endpoint any more: a bucket id is a uuid5 of its identity,
so the DAG names its buckets on its own and never has to read back what earlier
runs clustered.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.endpoints.tasks import TaskResponse
from app.models.flow import Flow, FlowSource
from app.schemas.lot import LotBatchIn
from app.schemas.split import SplitBatchIn
from app.services.flow_service import flow_service
from app.services.lot_service import lot_service
from app.services.split_service import split_service

router = APIRouter()


def _resolve_source(db: Session, *, flow_code: str, source_code: str):
    flow: Flow = flow_service.get_flow_by_code(db, code=flow_code)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    source: FlowSource = next((s for s in flow.sources if s.code == source_code), None)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return flow, source


@router.post("/finacle-bb/splits/batch", response_model=TaskResponse)
def push_split_batch(
    payload: SplitBatchIn,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Register each claim group's parents, materialise its ghosts, withdraw
    the real movements.

    Pushed BEFORE the lot batch: the real movements must be out of
    ``reconciliation_entry`` before the group's ghosts are counted as lot
    members, so no intermediate state has both. Also reaps the ghosts a group
    no longer produces (a bucket that disappeared between two runs).
    """
    flow, source = _resolve_source(
        db, flow_code=payload.flow_code, source_code=payload.source_code
    )
    try:
        result = split_service.apply_split_batch(
            db, flow=flow, source=source, groups=payload.groups, run_id=payload.run_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return TaskResponse(ok=True, data=result)


@router.post("/finacle-bb/lots/batch", response_model=TaskResponse)
def push_lot_batch(
    payload: LotBatchIn,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Persist one bucket batch (buckets + members + keys) atomically.

    Idempotent by construction: the same bucket always carries the same uuid5,
    so a re-pushed batch after a crash lands on the same rows.
    """
    flow, source = _resolve_source(
        db, flow_code=payload.flow_code, source_code=payload.source_code
    )
    try:
        result = lot_service.apply_lot_batch(
            db,
            flow=flow,
            source=source,
            lots=payload.lots,
            members=payload.members,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return TaskResponse(ok=True, data=result)
