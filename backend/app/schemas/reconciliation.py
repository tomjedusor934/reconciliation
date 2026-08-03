from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict

from app.models.ingestion_run import IngestionStatus
from app.models.match_group import MatchMode
from app.models.reconciliation_entry import EntryDirection, EntryStatus


# ---------- IngestionRun ----------
class IngestionRunResponse(BaseModel):
    id: int
    flow_id: int
    flow_source_id: Optional[int]
    source_file: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]
    status: IngestionStatus
    rows_in: int
    rows_ok: int
    rows_ko: int
    rows_duplicate: int
    error: Optional[str]
    model_config = ConfigDict(from_attributes=True)


# ---------- ReconciliationEntry ----------
class ReconciliationEntryResponse(BaseModel):
    id: int
    flow_id: int
    ingestion_run_id: Optional[int]
    reco_id: Optional[str]
    account: Optional[str]
    currency: str
    amount: Decimal
    direction: Optional[EntryDirection]
    value_date: datetime
    operation_date: Optional[datetime]
    event_type: Optional[str]
    external_ref: Optional[str]
    file_name: Optional[str]
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    transaction_id: Optional[str] = None
    payload_raw: Optional[Dict[str, Any]]
    status: EntryStatus
    match_group_id: Optional[int]
    matched_at: Optional[datetime]
    # {payment status: count} for the movement's payments (std.Payment sync);
    # None for flows without payment tracking. Attached by the list service.
    payment_statuses: Optional[Dict[str, int]] = None
    # Span of the CreatedOn of those same payments (both equal when the
    # group carries a single timestamp). Naive: the datamart wall clock is kept
    # as-is so the hour displayed is the hour filtered on. Attached by the service.
    payment_timestamp_min: Optional[datetime] = None
    payment_timestamp_max: Optional[datetime] = None
    # 'live' | 'emargement' — which table the row was read from. Attached by the
    # deep search so an émargé movement is visibly such; None elsewhere.
    source: Optional[str] = None
    model_config = ConfigDict(from_attributes=True)


class ReconciliationEntryFilter(BaseModel):
    flow_id: Optional[int] = None
    status: Optional[EntryStatus] = None
    reco_id: Optional[str] = None
    currency: Optional[str] = None
    account: Optional[str] = None
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    search: Optional[str] = None


# ---------- MatchGroup ----------
class MatchGroupResponse(BaseModel):
    id: int
    flow_id: int
    reco_id: Optional[str]
    currency: str
    total: Decimal
    mode: MatchMode
    created_by_user_id: Optional[int]
    created_at: datetime
    comment: Optional[str]
    reconciliation_run_id: Optional[int]
    model_config = ConfigDict(from_attributes=True)


class ForceMatchRequest(BaseModel):
    """Manual forcing of a match: explicit list of entry IDs."""
    entry_ids: List[int]
    comment: Optional[str] = None


# ---------- Exclusion ----------
class ExclusionCreate(BaseModel):
    entry_id: int
    reason: str  # mandatory


class UnexcludeRequest(BaseModel):
    entry_id: int
    reason: str  # mandatory — justification for reversing the exclusion


class ExclusionResponse(BaseModel):
    id: int
    entry_id: int
    entry_value_date: datetime
    reason: str
    created_by_user_id: Optional[int]
    created_at: datetime
    cancelled_at: Optional[datetime] = None
    cancelled_by_user_id: Optional[int] = None
    model_config = ConfigDict(from_attributes=True)


# ---------- ReconciliationRun ----------
class ReconciliationRunResponse(BaseModel):
    id: int
    started_at: datetime
    finished_at: Optional[datetime]
    entries_scanned: int
    groups_created: int
    entries_matched: int
    duration_ms: Optional[int]
    triggered_by: Optional[str]
    model_config = ConfigDict(from_attributes=True)


# ---------- Dashboard ----------
class IngestionRunSummary(BaseModel):
    run_id: int
    row_count: int
    run_date: datetime
    source_code: Optional[str] = None


class FlowKPI(BaseModel):
    flow_id: int
    flow_code: str
    flow_name: str
    pending_count: int
    matched_count: int
    excluded_count: int
    pending_amount_total: Decimal
    pending_amount_credit: Decimal = Decimal("0")
    pending_amount_debit: Decimal = Decimal("0")
    last_ingestion_at: Optional[datetime] = None
    new_entries_delta: int = 0
    total_entries_current: int = 0
    oldest_unmatched_date: Optional[datetime] = None
    ingestion_runs: List[IngestionRunSummary] = []
    is_active: bool


class DashboardResponse(BaseModel):
    flows: List[FlowKPI]
    total_matched_count: int = 0
    last_reconciliation_run: Optional[ReconciliationRunResponse] = None


class IngestionCalendarDay(BaseModel):
    day: int
    count: int


class IngestionCalendarResponse(BaseModel):
    flow_id: int
    year: int
    month: int
    days: List[IngestionCalendarDay] = []


class TransversalGroup(BaseModel):
    reco_id: str
    currency: str
    entries: List[ReconciliationEntryResponse]
    sum: Decimal
    is_balanced: bool


# ---------- Deep search (transversal) ----------
class SearchMatch(BaseModel):
    """Where the searched value was found — the trace explaining a result."""
    source_table: str   # entry | entry_emargement | entry_payment_status | movement_lot_key | …
    field: str          # the column that matched (po_id, reco_id, key PO/PACS008/MSGID, …)
    value: str


class SearchPayment(BaseModel):
    reco_id: str
    po_id: str
    status: Optional[str] = None
    amount: Optional[Decimal] = None
    payment_timestamp: Optional[datetime] = None


class DeepSearchResponse(BaseModel):
    """Everything the searched value is attached to, across live and émargement."""
    query: str
    matches: List[SearchMatch] = []
    # Lot summaries (same shape as the lots list); dict-typed to avoid a circular
    # import with schemas.lot — the service fills it from lot_service._row_to_summary.
    lots: List[Dict[str, Any]] = []
    groups: List[TransversalGroup] = []
    payments: List[SearchPayment] = []
    # True when the broad ILIKE pass ran, and when a limit truncated the results.
    broad_used: bool = False
    truncated: bool = False


class PaginatedEntriesResponse(BaseModel):
    items: List[ReconciliationEntryResponse]
    total_count: int


# ---------- Audit ----------
class AuditLogResponse(BaseModel):
    id: int
    table_name: str
    row_pk: Optional[str]
    op: str
    old_data: Optional[Dict[str, Any]]
    new_data: Optional[Dict[str, Any]]
    user_id: Optional[int]
    ts: datetime
    model_config = ConfigDict(from_attributes=True)


class UIActionLogResponse(BaseModel):
    id: int
    user_id: Optional[int]
    action: str
    target_type: Optional[str]
    target_id: Optional[str]
    details: Optional[Dict[str, Any]]
    ts: datetime
    model_config = ConfigDict(from_attributes=True)
