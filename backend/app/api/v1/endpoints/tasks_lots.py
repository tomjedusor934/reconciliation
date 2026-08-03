"""Internal movement-lot endpoints called by the ingest_finacle_bb DAG.

Auth via X-Internal-Token, like the other /tasks endpoints. Kept in a separate
module so tasks.py doesn't keep growing; mounted under the same /tasks prefix.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.endpoints.tasks import TaskResponse
from app.models.flow import FlowSource
from app.schemas.lot import LotBatchIn, LotKeyMapResponse
from app.services.flow_service import flow_service
from app.services.lot_service import CrossLotKeyConflict, lot_service

router = APIRouter()


@router.get("/finacle-bb/lots/keys", response_model=LotKeyMapResponse)
def get_lot_key_map(
    flow_source_id: int,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Every key of every active lot of the source — the DAG seeds its
    union-find with this map so late movements join existing lots."""
    source = db.query(FlowSource).filter(FlowSource.id == flow_source_id).first()
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow source not found")
    keys = lot_service.get_key_map(db, flow_source_id=flow_source_id)
    return LotKeyMapResponse(ok=True, keys=keys)


@router.post("/finacle-bb/lots/batch", response_model=TaskResponse)
def push_lot_batch(
    payload: LotBatchIn,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Persist one clustering batch (lots + merges + members + keys) atomically.

    Merges relink the absorbed lot's live PENDING entries to the survivor via a
    targeted UPDATE. A key attached to more than one active lot after the write
    means the DAG clustered against a stale key map → 409, batch rolled back,
    next run re-clusters."""
    flow = flow_service.get_flow_by_code(db, code=payload.flow_code)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    source = next((s for s in flow.sources if s.code == payload.source_code), None)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")

    try:
        result = lot_service.apply_lot_batch(
            db,
            flow=flow,
            source=source,
            lots=payload.lots,
            merges=payload.merges,
            members=payload.members,
        )
    except CrossLotKeyConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return TaskResponse(ok=True, data=result)
