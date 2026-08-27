"""Internal endpoints called by Airflow DAGs (tasks-only). Auth via X-Internal-Token."""
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.core.config import settings
from app.models.flow import FlowSourceType
from app.models.ingestion_run import IngestionStatus
from app.repositories.flow_repository import flow_repository
from app.repositories.ingestion_run_repository import ingestion_run_repository
from app.repositories.payment_bulk_key_repository import payment_bulk_key_repository
from app.repositories.reconciliation_entry_repository import reconciliation_entry_repository
from app.services.archive_service import emargement_service
from app.services.airflow_client_service import AirflowClientError, airflow_client_service
from app.services.flow_service import flow_service
from app.services.ingestion_service import ingestion_service
from app.services.parsers.base_parser import ParsedEntry
from app.services.reconciliation_service import (
    ReconciliationAlreadyRunning,
    reconciliation_service,
)

router = APIRouter()


class TaskResponse(BaseModel):
    ok: bool
    detail: Optional[str] = None
    data: Optional[dict] = None


class ActiveFlowInfo(BaseModel):
    flow_id: int
    flow_code: str
    has_file_sources: bool
    has_finacle_sources: bool


class ActiveFlowsResponse(BaseModel):
    ok: bool
    flows: List[ActiveFlowInfo]


class FinacleSourceInfo(BaseModel):
    flow_id: int
    flow_code: str
    source_id: int
    source_code: str
    # Routes the source to the right extractor: "finacle_db" (legacy 1-key
    # classification), "finacle_batch_booking_true" (lot clustering DAG) or
    # "wero" (payment reconciliation DAG). Each extractor self-selects on this
    # value, so a source is never picked up by two of them.
    parser_type: Optional[str] = None
    # Per-source extractor settings (FlowSource.parser_config). Unused by the
    # finacle extractors; the WERO one takes every datamart identifier from it
    # so an unconfirmed table/column can be retuned from the UI without a DAG
    # redeploy.
    parser_config: Dict[str, Any] = Field(default_factory=dict)
    accounts: List[str]
    last_success_at: Optional[datetime] = None
    # Lower bound (ISO date) for a source's first extraction (no watermark yet).
    # Sourced from the backend .env so the date is configured in one place.
    backfill_since: Optional[str] = None


class FinacleSourcesResponse(BaseModel):
    ok: bool
    sources: List[FinacleSourceInfo]


class FinacleRunStart(BaseModel):
    flow_code: str
    source_code: str
    dag_run_id: Optional[str] = None


class FinacleEntryIn(BaseModel):
    reco_id: Optional[str] = None
    account: Optional[str] = None
    currency: str = "EUR"
    amount: Decimal
    value_date: datetime
    operation_date: Optional[datetime] = None
    direction: Optional[str] = None  # 'debit' / 'credit'
    event_type: Optional[str] = None
    external_ref: Optional[str] = None
    file_name: Optional[str] = None
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    transaction_id: Optional[str] = None
    payload_raw: Dict[str, Any] = Field(default_factory=dict)


class FinacleBatchIn(BaseModel):
    entries: List[FinacleEntryIn] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class FinacleRunComplete(BaseModel):
    failed: bool = False
    error: Optional[str] = None


class FinacleUnresolvedResponse(BaseModel):
    ok: bool
    entries: List[FinacleEntryIn]


class FinacleBulkKeysResponse(BaseModel):
    ok: bool
    keys: Dict[str, str] = Field(default_factory=dict)  # po_id -> bulk reco_id


class FinacleBulkKeysIn(BaseModel):
    flow_code: str
    source_code: str
    keys: Dict[str, str] = Field(default_factory=dict)
    # std.Payment.InitModule per po_id (e.g. 'MLTLIP') — traceability only.
    init_modules: Dict[str, str] = Field(default_factory=dict)


# --- MT940 (DAG-extracted file sources) -----------------------------------
class Mt940SourceInfo(BaseModel):
    flow_id: int
    flow_code: str
    source_id: int
    source_code: str
    inbox_subfolder: Optional[str] = None
    parser_config: Dict[str, Any] = Field(default_factory=dict)


class Mt940SourcesResponse(BaseModel):
    ok: bool
    sources: List[Mt940SourceInfo]


class Mt940InboxResponse(BaseModel):
    ok: bool
    files: List[str]


class Mt940RunOpen(BaseModel):
    flow_code: str
    source_code: str
    file_name: str


class Mt940RunComplete(BaseModel):
    file_name: str
    failed: bool = False
    error: Optional[str] = None


class Mt940UnresolvedResponse(BaseModel):
    ok: bool
    keys: List[str]


class Mt940ResolveIn(BaseModel):
    flow_code: str
    source_code: str
    resolved: Dict[str, str] = Field(default_factory=dict)


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive datetimes are treated as UTC — keeps source_hash stable."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _to_parsed(e: FinacleEntryIn) -> ParsedEntry:
    return ParsedEntry(
        reco_id=e.reco_id,
        account=e.account,
        currency=e.currency,
        amount=e.amount,
        value_date=_to_utc(e.value_date),
        operation_date=_to_utc(e.operation_date) or _to_utc(e.value_date),
        direction=e.direction,
        event_type=e.event_type,
        external_ref=e.external_ref,
        file_name=e.file_name,
        transaction_particulars=e.transaction_particulars,
        ref_no=e.ref_no,
        remarks_1=e.remarks_1,
        transaction_id=e.transaction_id,
        payload_raw=e.payload_raw,
    )


def _get_running_run(db: Session, run_id: int):
    run = ingestion_run_repository.get(db, run_id=run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if run.status != IngestionStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Run {run_id} is not running (status={run.status.value})",
        )
    return run


@router.post("/ingest/{flow_code}", response_model=TaskResponse)
def trigger_ingest(
    flow_code: str,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Ingest files from all active file-type sources of the flow."""
    flow = flow_service.get_flow_by_code(db, code=flow_code)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    runs = ingestion_service.ingest_inbox_for_flow(db, flow=flow)
    return TaskResponse(ok=True, data={"runs": [r.id for r in runs], "count": len(runs)})


@router.get("/finacle/sources", response_model=FinacleSourcesResponse)
def list_finacle_sources(
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Active finacle_db sources of active flows, with their reference accounts
    and ingestion watermark — consumed by the ingest_finacle DAG."""
    infos: List[FinacleSourceInfo] = []
    for flow in flow_repository.list_active(db):
        for source in flow.sources:
            if not source.is_active or source.source_type != FlowSourceType.FINACLE_DB:
                continue
            infos.append(
                FinacleSourceInfo(
                    flow_id=flow.id,
                    flow_code=flow.code,
                    source_id=source.id,
                    source_code=source.code,
                    parser_type=source.parser_type.value if source.parser_type else None,
                    parser_config=source.parser_config or {},
                    accounts=[a.account_number for a in source.accounts],
                    last_success_at=ingestion_run_repository.last_success_for_source(
                        db, flow_source_id=source.id
                    ),
                    backfill_since=settings.RECO_FINACLE_BACKFILL_SINCE or None,
                )
            )
    return FinacleSourcesResponse(ok=True, sources=infos)


@router.post("/finacle/runs", response_model=TaskResponse)
def start_finacle_run(
    payload: FinacleRunStart,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Open a RUNNING ingestion run for a Finacle source (data pushed by the DAG)."""
    flow = flow_service.get_flow_by_code(db, code=payload.flow_code)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    source = next((s for s in flow.sources if s.code == payload.source_code), None)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    try:
        run = ingestion_service.start_finacle_run(
            db, flow=flow, source=source, dag_run_id=payload.dag_run_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return TaskResponse(ok=True, data={"run_id": run.id})


@router.post("/finacle/runs/{run_id}/batch", response_model=TaskResponse)
def push_finacle_batch(
    run_id: int,
    payload: FinacleBatchIn,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Persist one batch of entries extracted from the datamart by the DAG."""
    run = _get_running_run(db, run_id)
    entries = [_to_parsed(e) for e in payload.entries]
    inserted, dup = ingestion_service.ingest_finacle_batch(
        db, run=run, entries=entries, errors=payload.errors
    )
    return TaskResponse(
        ok=True,
        data={"inserted": inserted, "duplicates": dup, "errors": len(payload.errors)},
    )


@router.post("/finacle/runs/{run_id}/complete", response_model=TaskResponse)
def complete_finacle_run(
    run_id: int,
    payload: FinacleRunComplete,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Close a Finacle run (SUCCESS / PARTIAL / FAILED)."""
    run = _get_running_run(db, run_id)
    run = ingestion_service.complete_finacle_run(
        db, run=run, failed=payload.failed, error=payload.error
    )
    return TaskResponse(
        ok=True,
        data={
            "run_id": run.id,
            "status": run.status.value,
            "rows_in": run.rows_in,
            "rows_ok": run.rows_ok,
            "rows_ko": run.rows_ko,
            "rows_duplicate": run.rows_duplicate,
        },
    )


@router.get("/finacle/unresolved", response_model=FinacleUnresolvedResponse)
def list_finacle_unresolved(
    flow_code: str,
    source_code: str,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Live PENDING finacle entries whose reco_id is still unresolved (NULL or
    'Not Supported') — the DAG re-enriches them and re-pushes (upsert updates
    reco_id in place)."""
    flow = flow_service.get_flow_by_code(db, code=flow_code)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    source = next((s for s in flow.sources if s.code == source_code), None)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")

    rows = reconciliation_entry_repository.list_unresolved_finacle(db, flow_source_id=source.id)
    entries = [
        FinacleEntryIn(
            reco_id=r.reco_id,
            account=r.account,
            currency=r.currency,
            amount=r.amount,
            value_date=r.value_date,
            operation_date=r.operation_date,
            direction=r.direction.value if r.direction is not None else None,
            event_type=r.event_type,
            external_ref=r.external_ref,
            transaction_particulars=r.transaction_particulars,
            ref_no=r.ref_no,
            remarks_1=r.remarks_1,
            # Kept on the round-trip: the DAG re-pushes these entries as-is, so
            # dropping it here would blank the column through the upsert.
            transaction_id=r.transaction_id,
            payload_raw=r.payload_raw or {},
        )
        for r in rows
    ]
    return FinacleUnresolvedResponse(ok=True, entries=entries)


@router.get("/finacle/bulk-keys", response_model=FinacleBulkKeysResponse)
def list_finacle_bulk_keys(
    flow_code: str,
    source_code: str,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """{po_id -> reco_id} of the bulk bookings detected so far, so the DAG keys a
    bulk member on the bulk's MessageID from pass 2 on — including a movement that
    arrives long after the run which discovered the bulk."""
    _get_flow_source(db, flow_code, source_code)
    return FinacleBulkKeysResponse(
        ok=True, keys=payment_bulk_key_repository.get_map(db)
    )


@router.post("/finacle/bulk-keys", response_model=TaskResponse)
def push_finacle_bulk_keys(
    payload: FinacleBulkKeysIn,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Persist the run's {po_id -> bulk reco_id} map and re-key in place the flow's
    live PENDING entries still stored under a member PO id — BOTH legs of the flow
    (the file leg resolves to the same PaymentNumber). No re-push: source_hash is
    untouched, and non-PENDING entries are never modified."""
    flow, _source = _get_flow_source(db, payload.flow_code, payload.source_code)
    pairs = {k: v for k, v in (payload.keys or {}).items() if k and v}
    if not pairs:
        return TaskResponse(ok=True, data={"keys_upserted": 0, "entries_updated": 0})

    # The DAG's scan fans out on MessageID, so a push carries a bulk's FULL
    # membership — counting the pairs is the group size, no need to wire it.
    member_counts = Counter(pairs.values())
    rows = [
        {
            "po_id": po_id,
            "reco_id": reco_id,
            "init_module": payload.init_modules.get(po_id),
            "member_count": member_counts[reco_id],
        }
        for po_id, reco_id in pairs.items()
    ]
    inserted, updated = payment_bulk_key_repository.upsert_many(db, rows)
    db.commit()  # the repository never commits — this endpoint owns the transaction
    entries_updated = reconciliation_entry_repository.regroup_bulk_pos(
        db, flow_id=flow.id, mapping=pairs
    )
    return TaskResponse(
        ok=True,
        data={
            "keys_upserted": inserted + updated,
            "keys_inserted": inserted,
            "entries_updated": entries_updated,
        },
    )


@router.get("/mt940/sources", response_model=Mt940SourcesResponse)
def list_mt940_sources(
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Active file sources flagged extract_via_dag (MT940) — the DAG fetches their
    inbox files and pushes parsed entries back."""
    infos: List[Mt940SourceInfo] = []
    for flow in flow_repository.list_active(db):
        for source in flow.sources:
            if not source.is_active or source.source_type != FlowSourceType.FILE:
                continue
            if not (source.parser_config or {}).get("extract_via_dag"):
                continue
            infos.append(
                Mt940SourceInfo(
                    flow_id=flow.id,
                    flow_code=flow.code,
                    source_id=source.id,
                    source_code=source.code,
                    inbox_subfolder=source.inbox_subfolder,
                    parser_config=source.parser_config or {},
                )
            )
    return Mt940SourcesResponse(ok=True, sources=infos)


def _get_flow_source(db: Session, flow_code: str, source_code: str):
    flow = flow_service.get_flow_by_code(db, code=flow_code)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    source = next((s for s in flow.sources if s.code == source_code), None)
    if not source:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found")
    return flow, source


@router.get("/mt940/inbox", response_model=Mt940InboxResponse)
def list_mt940_inbox(
    flow_code: str,
    source_code: str,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """File names currently sitting in a DAG-extracted source's inbox."""
    flow, source = _get_flow_source(db, flow_code, source_code)
    files = ingestion_service.list_mt940_inbox(flow, source)
    return Mt940InboxResponse(ok=True, files=files)


@router.post("/mt940/runs", response_model=TaskResponse)
def open_mt940_run(
    payload: Mt940RunOpen,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Open a RUNNING run for one MT940 file and return its raw content to the DAG
    (the file may be large; this is where it leaves the backend)."""
    flow, source = _get_flow_source(db, payload.flow_code, payload.source_code)
    try:
        run, content = ingestion_service.open_mt940_run(
            db, flow=flow, source=source, file_name=payload.file_name
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return TaskResponse(ok=True, data={"run_id": run.id, "content": content})


@router.post("/mt940/runs/{run_id}/batch", response_model=TaskResponse)
def push_mt940_batch(
    run_id: int,
    payload: FinacleBatchIn,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Persist one batch of MT940 entries extracted + resolved by the DAG."""
    run = _get_running_run(db, run_id)
    entries = [_to_parsed(e) for e in payload.entries]
    inserted, dup = ingestion_service.ingest_mt940_batch(
        db, run=run, entries=entries, errors=payload.errors
    )
    return TaskResponse(
        ok=True,
        data={"inserted": inserted, "duplicates": dup, "errors": len(payload.errors)},
    )


@router.post("/mt940/runs/{run_id}/complete", response_model=TaskResponse)
def complete_mt940_run(
    run_id: int,
    payload: Mt940RunComplete,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Close an MT940 run and move the file to processed/error."""
    run = _get_running_run(db, run_id)
    run = ingestion_service.complete_mt940_run(
        db, run=run, file_name=payload.file_name, failed=payload.failed, error=payload.error
    )
    return TaskResponse(
        ok=True,
        data={
            "run_id": run.id,
            "status": run.status.value,
            "rows_in": run.rows_in,
            "rows_ok": run.rows_ok,
            "rows_ko": run.rows_ko,
            "rows_duplicate": run.rows_duplicate,
        },
    )


@router.get("/mt940/unresolved", response_model=Mt940UnresolvedResponse)
def list_mt940_unresolved(
    flow_code: str,
    source_code: str,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """remarks_1 keys of the source's live PENDING entries with unresolved
    reco_id — the DAG retries the std.Payment TransactionRef join on them and
    posts back the resolved pairs via /mt940/resolve."""
    flow, source = _get_flow_source(db, flow_code, source_code)
    keys = reconciliation_entry_repository.list_unresolved_mt940_keys(
        db, flow_source_id=source.id
    )
    return Mt940UnresolvedResponse(ok=True, keys=keys)


@router.post("/mt940/resolve", response_model=TaskResponse)
def resolve_mt940_entries(
    payload: Mt940ResolveIn,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Fill reco_id in place on PENDING entries whose remarks_1 key the DAG
    resolved against std.Payment (no re-push — keeps source_hash untouched)."""
    flow, source = _get_flow_source(db, payload.flow_code, payload.source_code)
    updated = reconciliation_entry_repository.resolve_mt940_keys(
        db, flow_source_id=source.id, resolved=payload.resolved
    )
    return TaskResponse(ok=True, data={"updated": updated})


@router.post("/reconcile", response_model=TaskResponse)
def trigger_reconcile(
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Auto-reconciliation. Single-writer via an advisory lock.

    Lock already held → 200 + skipped, NOT 409. A 409 would mean "do it again";
    here there is nothing to redo, another run is already doing the work. And
    mechanically: reco_common.call_backend calls raise_for_status(), so a 409
    fails the task → retry → the run is still going → 409 again → red DAG over a
    benign no-op. Same shape as the skips the DAGs already emit
    (orchestrate_ingestion.py:60).
    """
    try:
        run = reconciliation_service.run_auto(db, triggered_by="airflow")
    except ReconciliationAlreadyRunning as exc:
        # MUST stay above any `except ValueError` (→ 400) added later:
        # ReconciliationAlreadyRunning is a subclass of it.
        return TaskResponse(
            ok=True,
            detail=str(exc),
            data={"skipped": True, "reason": "already_running"},
        )
    return TaskResponse(
        ok=True,
        data={
            "run_id": run.id,
            "groups_created": run.groups_created,
            "entries_matched": run.entries_matched,
        },
    )


@router.post("/emargement", response_model=TaskResponse)
def trigger_emargement(
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Sweep any remaining matched/forced/excluded entries from the live table
    to the émargement table (safety net — normally done inline).

    Shares run_auto's writer lock (its unindexable DELETE sweeps the table in
    physical order — a 3rd lock order). Skipped during a run: the sweep is
    idempotent and will come back.
    """
    try:
        moved = emargement_service.sweep_matched(db)
    except ReconciliationAlreadyRunning as exc:
        return TaskResponse(
            ok=True,
            detail=str(exc),
            data={"skipped": True, "reason": "already_running"},
        )
    return TaskResponse(ok=True, data={"moved": moved})


@router.post("/active-flows", response_model=ActiveFlowsResponse)
def list_active_flows(
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Return all active flows with source type info — used by the orchestrator DAG."""
    from app.repositories.flow_repository import flow_repository
    active_flows = flow_repository.list_active(db)
    infos = []
    for f in active_flows:
        active_sources = [s for s in f.sources if s.is_active]
        has_file = any(s.source_type.value == "file" for s in active_sources)
        has_finacle = any(s.source_type.value == "finacle_db" for s in active_sources)
        infos.append(ActiveFlowInfo(
            flow_id=f.id,
            flow_code=f.code,
            has_file_sources=has_file,
            has_finacle_sources=has_finacle,
        ))
    return ActiveFlowsResponse(ok=True, flows=infos)

"""
 this method is keep for backward compatibility with the orchestrate_ingestion DAG, but not supposed to be used otherwise — the DAG orchestrator call by it self and not anymore from the app
"""
@router.post("/orchestrate", response_model=TaskResponse)
def trigger_orchestrate(
    _: None = Depends(deps.verify_internal_token),
):
    """Trigger the Airflow orchestrate_ingestion DAG (ingest all flows → reconcile → emargement)."""
    try:
        result = airflow_client_service.trigger_dag("orchestrate_ingestion")
    except AirflowClientError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    dag_run_id = result.get("dag_run_id") or result.get("run_id") or result.get("dagRunId")
    return TaskResponse(ok=True, data={"dag_run_id": dag_run_id, "state": result.get("state", "queued")})
