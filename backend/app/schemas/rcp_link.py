"""Schemas for the RCP reattribution tool (see services/rcp_link_service.py)."""
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ── Analyze response ────────────────────────────────────────────────

class RcpFileReportOut(BaseModel):
    """What one uploaded file actually held — makes a bad extract visible
    instead of silent (the 2026-08-12 reject dump had a msgid column with no
    value in any of its 17 195 rows)."""
    file_name: str
    rows: int = 0
    rows_with_msgid: int = 0
    distinct_msgids: int = 0
    has_msgid_column: bool = True
    amount_column: str = ""
    delimiter: str = ""
    error: str = ""
    # {serviceid: rows} over the whole workbook, kept and skipped types alike.
    services: Dict[str, int] = Field(default_factory=dict)


class RcpControlOut(BaseModel):
    """Part 1: the movement's dump rows vs what it booked."""
    msgid: str
    status: str
    expected_count: int
    found_count: int
    expected_amount: Decimal
    found_amount: Decimal
    delta_count: int
    delta_amount: Decimal
    files: List[str] = Field(default_factory=list)
    service_id: str = ""
    direction: str = ""
    tran_id: str = ""
    sp_date: str = ""


class RcpEntryOut(BaseModel):
    # None when the movement has no live row left: it was withdrawn in favour of
    # its ghosts and is described by its ``movement_split`` parent instead.
    id: Optional[int] = None
    flow_id: int
    flow_source_id: Optional[int] = None
    source_hash: str
    reco_id: Optional[str] = None
    account: Optional[str] = None
    currency: str
    amount: Decimal
    direction: Optional[str] = None
    value_date: datetime
    operation_date: Optional[datetime] = None
    external_ref: Optional[str] = None
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    status: Optional[str] = None


class RcpTargetOut(BaseModel):
    """One destination the movement will be split into, worth its payments'
    exact sum. ``target_id`` is a lot uuid when ``target_kind`` is LOT, and the
    flow's reconciliation key when it is RECO (classic bulk flow, no lots)."""
    target_id: str
    target_kind: str = "LOT"
    bucket_kind: str = ""
    bucket_pacs008: str = ""
    bucket_msgid: str = ""
    bucket_po: str = ""
    bucket_ref: str = ""
    label: str = ""
    resolved_via: str = ""
    amount: Decimal
    payment_count: int = 0
    pos: List[str] = Field(default_factory=list)


class RcpUnresolvedOut(BaseModel):
    po: str
    amount: Decimal
    reason: str


class RcpProposalOut(BaseModel):
    msgid: str
    status: str
    message: str = ""
    service_id: str = ""
    # Which flow the movement was found in, and therefore whether it is split
    # into lots or onto reconciliation keys.
    flow_id: Optional[int] = None
    flow_code: str = ""
    source_code: str = ""
    target_kind: str = ""
    direction: str = ""
    tran_id: str = ""
    sp_date: str = ""
    num_records: int = 0
    settlement_amount: Decimal
    control_status: str = ""
    control_delta_count: int = 0
    control_delta_amount: Decimal = Decimal("0")
    entry: Optional[RcpEntryOut] = None
    candidates: List[RcpEntryOut] = Field(default_factory=list)
    targets: List[RcpTargetOut] = Field(default_factory=list)
    unresolved_payments: List[RcpUnresolvedOut] = Field(default_factory=list)
    resolved_amount: Decimal = Decimal("0")


class RcpAnalyzeResponse(BaseModel):
    link_file: RcpFileReportOut
    dump_files: List[RcpFileReportOut] = Field(default_factory=list)
    duplicate_msgids: List[str] = Field(default_factory=list)
    controls: List[RcpControlOut] = Field(default_factory=list)
    control_summary: Dict[str, int] = Field(default_factory=dict)
    proposals: List[RcpProposalOut] = Field(default_factory=list)
    summary: Dict[str, int] = Field(default_factory=dict)
    # Empty when std.Payment answered; otherwise why it did not, so a run that
    # fell back on entry_payment_status / lot keys says so out loud.
    datamart_error: str = ""


# ── Job envelope ────────────────────────────────────────────────────

class RcpJobResponse(BaseModel):
    """A background run. ``result`` carries the analyze report or the commit
    outcome once ``status`` is 'done' — it stays null while running, so the poll
    response stays small until the very end."""
    job_id: str
    kind: str                      # analyze | commit
    status: str                    # running | done | error
    phase: str = ""                # what is running, shown as-is
    done: int = 0
    total: int = 0
    result: Optional[Dict[str, Any]] = None
    error: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # Running commentary of the batch — what it read, found, and failed on.
    logs: List[str] = Field(default_factory=list)


class RcpRunOut(BaseModel):
    """One past run in the history list — counters only, never the report."""
    job_id: str
    kind: str
    status: str
    phase: str = ""
    error: str = ""
    label: str = ""
    movements: int = 0
    actionable: int = 0
    applied: int = 0
    failed: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ── Commit request/response ─────────────────────────────────────────

class RcpCommitTargetIn(BaseModel):
    # Lot uuid or reconciliation key — the server re-reads which one it is from
    # the movement's own flow, never from the payload.
    target_id: str = Field(min_length=1, max_length=128)
    amount: Decimal
    payment_count: int = 0
    pos: List[str] = Field(default_factory=list)


class RcpCommitItemIn(BaseModel):
    msgid: str = Field(min_length=1, max_length=128)
    # The movement is addressed by its hash, never by id: it is the same
    # identity the split uses to withdraw it.
    entry_source_hash: str = Field(min_length=1, max_length=64)
    targets: List[RcpCommitTargetIn] = Field(default_factory=list)


class RcpCommitRequest(BaseModel):
    items: List[RcpCommitItemIn] = Field(default_factory=list)


class RcpCommitLotOut(BaseModel):
    target_id: str
    target_kind: str = "LOT"
    amount: Decimal
    payment_count: int = 0


class RcpCommitResultOut(BaseModel):
    msgid: str
    applied: bool
    error: str = ""
    ghost_total: Optional[Decimal] = None
    booked_amount: Optional[Decimal] = None
    parents_emarged: int = 0
    targets: List[RcpCommitLotOut] = Field(default_factory=list)


class RcpCommitResponse(BaseModel):
    applied: int
    failed: int
    results: List[RcpCommitResultOut] = Field(default_factory=list)


# ── Orphans: movements the ingestion could not key at all ────────────
# (see services/rcp_orphan_service.py — no upload, the movement itself is
# the input)

class RcpOrphanOut(BaseModel):
    """One stranded movement and where it belongs, with the evidence.

    ``rule`` and ``keys`` are shown to the operator, not just used: a
    PaymentNumber Finacle wrote into ``ref_no`` and a digit run scraped out of a
    free-text label must not look alike on screen.
    """
    source_hash: str
    entry_id: Optional[int] = None
    flow_id: int
    source_code: str = ""
    reco_id: Optional[str] = None
    amount: Decimal
    currency: str = "EUR"
    direction: Optional[str] = None
    value_date: datetime
    external_ref: Optional[str] = None
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    rule: str = ""
    keys: List[str] = Field(default_factory=list)
    target_kind: str = ""
    target_id: str = ""
    target_label: str = ""
    candidates: List[str] = Field(default_factory=list)
    status: str
    message: str = ""


class RcpOrphanAnalyzeRequest(BaseModel):
    flow_id: int
    connection_id: Optional[int] = None
    # A guard on the payload, not a paging window: the report goes to the
    # browser whole, and a flow with 50 000 stranded movements is a parser
    # problem to fix, not a list to scroll.
    limit: int = Field(default=5000, ge=1, le=20000)


class RcpOrphanAnalyzeResponse(BaseModel):
    flow_id: int
    proposals: List[RcpOrphanOut] = Field(default_factory=list)
    summary: Dict[str, int] = Field(default_factory=dict)
    datamart_error: str = ""


class RcpOrphanCommitItemIn(BaseModel):
    # The movement is addressed by its hash, never by id — the same identity
    # every other write in this tool uses.
    source_hash: str = Field(min_length=1, max_length=64)
    target_id: str = Field(min_length=1, max_length=128)
    # Carried through to the audit trail so a retargeting can be explained
    # months later: which rule proposed it, and on which key.
    rule: str = Field(default="", max_length=32)
    key: str = Field(default="", max_length=128)


class RcpOrphanCommitRequest(BaseModel):
    items: List[RcpOrphanCommitItemIn] = Field(default_factory=list)


class RcpOrphanCommitResultOut(BaseModel):
    source_hash: str
    applied: bool
    error: str = ""
    target_id: str = ""
    target_kind: str = ""


class RcpOrphanCommitResponse(BaseModel):
    applied: int
    failed: int
    results: List[RcpOrphanCommitResultOut] = Field(default_factory=list)
