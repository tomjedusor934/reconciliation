"""Temporary operator tool: reattribute return/reject movements (RCP/RCC/RRS/WCC)
to what they undo (see app/services/rcp_link_service.py for the why).

NONE of these endpoints is long. ``/analyze`` and ``/commit`` only READ the
upload and start a background job, then answer 202 with a job id; ``/jobs/{id}``
is a dict lookup the browser polls. Running the batch inside the request is what
produced the 504 in prod: nginx cuts at ``proxy_read_timeout`` (300 s) and the
operator learns nothing about what happened. Now no proxy timeout can bite, and
the UI can say which phase is running.

``/orphans/*`` is the same machinery aimed at a different population: the
movements the ingestion could not key at all ('Not Supported' / no reco_id).
There is no upload — the movement's own ref_no / particulars are the input (see
app/services/rcp_orphan_service.py). Both halves share the job registry, the run
history and the polling endpoint below.

``/jobs/{id}`` falls back to ``reco.rcp_run`` when the in-memory registry no
longer knows the id (backend restart, TTL), and ``/runs`` lists past runs so a
report survives the operator walking away — which is the point of not making
them watch. A persisted run carries no logs; those are live commentary.

Superuser-only — the commit splits real movements.
"""
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.models.user import User
from app.repositories.rcp_run_repository import rcp_run_repository
from app.schemas.rcp_link import (
    RcpCommitRequest,
    RcpJobResponse,
    RcpOrphanAnalyzeRequest,
    RcpOrphanCommitRequest,
    RcpRunOut,
)
from app.services.rcp_job_service import rcp_job_service
from app.services.rcp_link_service import rcp_link_service
from app.services.rcp_orphan_service import rcp_orphan_service

logger = logging.getLogger(__name__)

router = APIRouter()

# The dumps are full table extracts (13 MB on 2026-08-13, and more are coming);
# the cap only exists to keep a mistaken upload from eating the worker's memory.
# nginx must allow at least as much (client_max_body_size in nginx/nginx.conf).
MAX_TOTAL_UPLOAD_BYTES = 300 * 1024 * 1024


@router.post("/analyze", response_model=RcpJobResponse, status_code=status.HTTP_202_ACCEPTED)
def analyze(
    link_file: UploadFile = File(..., description="SP_LINK_REPORT xlsx"),
    dump_files: List[UploadFile] = File(default=[], description="return/reject dumps"),
    connection_id: Optional[int] = Form(None),
    flow_id: Optional[int] = Form(None),
    _: User = Depends(deps.get_current_active_superuser),
):
    """Start the analysis. Reads the upload, returns a job id. Writes nothing.

    The files are read here, in the request, and handed to the job as bytes: an
    ``UploadFile`` is backed by a temporary file the framework closes when the
    response is sent, so the worker thread could not read it afterwards.
    """
    if not dump_files:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Au moins un fichier de paiements (return/reject) est requis.",
        )

    total = 0
    link_content = link_file.file.read()
    total += len(link_content)
    dumps = []
    for upload in dump_files:
        content = upload.file.read()
        total += len(content)
        if total > MAX_TOTAL_UPLOAD_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Volume uploadé trop important.",
            )
        dumps.append((upload.filename or "dump", content))
    link = (link_file.filename or "link.xlsx", link_content)

    def work(db: Session, progress: Any) -> Dict[str, Any]:
        return rcp_link_service.analyze(
            db,
            link_file=link,
            dump_files=dumps,
            connection_id=connection_id,
            flow_id=flow_id,
            progress=progress,
        )

    label = ", ".join([link[0]] + [name for name, _ in dumps])
    job = rcp_job_service.start("analyze", work, user_id=_.id, label=label)
    logger.info(
        "[rcp] analyse %s lancée: %s + %d dump(s), %.1f Mo",
        job.id, link[0], len(dumps), total / 1024 / 1024,
    )
    return job.public()


@router.post("/commit", response_model=RcpJobResponse, status_code=status.HTTP_202_ACCEPTED)
def commit(
    payload: RcpCommitRequest,
    current_user: User = Depends(deps.get_current_active_superuser),
):
    """Start the commit. Each movement is re-validated against the database
    before anything is written, and lands whole or not at all."""
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Aucune réattribution sélectionnée.",
        )
    items = list(payload.items)
    user_id = current_user.id

    def work(db: Session, progress: Any) -> Dict[str, Any]:
        return rcp_link_service.commit(db, items=items, user_id=user_id, progress=progress)

    job = rcp_job_service.start(
        "commit", work, user_id=user_id, label=f"{len(items)} mouvement(s)"
    )
    logger.info("[rcp] commit %s lancé: %d mouvement(s)", job.id, len(items))
    return job.public()


@router.post(
    "/orphans/analyze", response_model=RcpJobResponse, status_code=status.HTTP_202_ACCEPTED
)
def analyze_orphans(
    payload: RcpOrphanAnalyzeRequest,
    current_user: User = Depends(deps.get_current_active_superuser),
):
    """Start the analysis of a flow's movements that the ingestion could not key
    ('Not Supported' / no reco_id). No upload: the movements themselves are the
    input. Returns a job id, writes nothing."""
    flow_id, connection_id, limit = payload.flow_id, payload.connection_id, payload.limit

    def work(db: Session, progress: Any) -> Dict[str, Any]:
        return rcp_orphan_service.analyze(
            db, flow_id=flow_id, connection_id=connection_id,
            limit=limit, progress=progress,
        )

    job = rcp_job_service.start(
        "analyze", work, user_id=current_user.id, label=f"non rattachés, flux {flow_id}"
    )
    logger.info("[rcp] analyse des orphelins %s lancée: flux %s", job.id, flow_id)
    return job.public()


@router.post(
    "/orphans/commit", response_model=RcpJobResponse, status_code=status.HTTP_202_ACCEPTED
)
def commit_orphans(
    payload: RcpOrphanCommitRequest,
    current_user: User = Depends(deps.get_current_active_superuser),
):
    """Retarget the ticked movements. Each one is re-validated against the
    database — and refused outright if it is no longer stranded."""
    if not payload.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Aucun mouvement sélectionné.",
        )
    items = list(payload.items)
    user_id = current_user.id

    def work(db: Session, progress: Any) -> Dict[str, Any]:
        return rcp_orphan_service.commit(db, items=items, user_id=user_id, progress=progress)

    job = rcp_job_service.start(
        "commit", work, user_id=user_id, label=f"{len(items)} mouvement(s) non rattaché(s)"
    )
    logger.info("[rcp] commit des orphelins %s lancé: %d mouvement(s)", job.id, len(items))
    return job.public()


@router.get("/jobs/{job_id}", response_model=RcpJobResponse)
def get_job(
    job_id: str,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    """Status of one job — the endpoint the UI polls. Cheap by construction.

    Falls back to the persisted run: after a backend restart the registry is
    empty, but a finished analysis is still worth handing back rather than
    telling the operator to start over.
    """
    job = rcp_job_service.get(job_id)
    if job is not None:
        return job.public()
    row = rcp_run_repository.get(db, job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Traitement inconnu ou purgé — relancez l'analyse.",
        )
    return rcp_run_repository.to_public(row, with_result=True)


@router.get("/runs", response_model=List[RcpRunOut])
def list_runs(
    limit: int = 20,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    """Recent runs, WITHOUT their report — one payload is ~2.6 MB of JSON."""
    return [
        rcp_run_repository.to_public(row)
        for row in rcp_run_repository.list_recent(db, limit=min(limit, 100))
    ]


@router.get("/runs/{run_id}", response_model=RcpJobResponse)
def get_run(
    run_id: str,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    """One past run, report included — what re-opens a report from the history."""
    row = rcp_run_repository.get(db, run_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Traitement introuvable."
        )
    return rcp_run_repository.to_public(row, with_result=True)
