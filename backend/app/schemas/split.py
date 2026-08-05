"""Pydantic schemas for movement splits (a real movement and its ghosts)."""
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ── Internal (ingest_finacle_bb DAG) payloads ───────────────────────

class SplitChildIn(BaseModel):
    """One ghost to materialise. Its identity is ``external_ref`` — the parent's
    with a per-bucket suffix — and the backend hashes it exactly like any other
    finacle movement, so the ghost entry and its lot member land on the same
    source_hash without either side sending a hash over the wire."""
    external_ref: str = Field(min_length=1, max_length=128)
    lot_id: str = Field(min_length=1, max_length=36)
    amount: Decimal
    direction: Optional[str] = None
    payment_count: int = 0
    bucket_kind: str = ""
    bucket_pacs008: str = ""
    bucket_msgid: str = ""
    bucket_po: str = ""


class SplitParentIn(BaseModel):
    """The real movement. Its ``source_hash`` is NOT sent: the backend derives it
    from the identity fields exactly as it does for a lot member, which is what
    guarantees the withdrawal hits the row the finacle push created."""
    movement_type: str = Field(min_length=1, max_length=16)
    external_ref: Optional[str] = None
    account: Optional[str] = None
    currency: str = "EUR"
    amount: Decimal
    direction: Optional[str] = None
    value_date: datetime
    operation_date: Optional[datetime] = None
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    event_type: Optional[str] = None
    transaction_id: Optional[str] = None
    payload_raw: Optional[Dict[str, Any]] = None
    # Payments in the groups this movement resolved — not "payments of this
    # movement" when the aggregate key is shared.
    payment_count: int = 0
    # Σ SettlementAmount over those groups, signed like the movement. Differing
    # from ``amount`` means std.Payment and the accounting disagree.
    payment_amount: Decimal = Decimal("0")
    # Movements of the run resolving through the same aggregate key. 1 = healthy.
    shared_key_movements: int = 1
    children: List[SplitChildIn] = Field(default_factory=list)


class SplitBatchIn(BaseModel):
    flow_code: str
    source_code: str
    # Stamped on the ghost entries this batch creates, so they belong to the run
    # that produced them like any other ingested movement.
    run_id: Optional[int] = None
    parents: List[SplitParentIn] = Field(default_factory=list)


# ── UI responses ─────────────────────────────────────────────────────

class SplitParentOut(BaseModel):
    source_hash: str
    flow_id: int
    movement_type: str
    external_ref: Optional[str] = None
    account: Optional[str] = None
    currency: str
    amount: Decimal
    direction: Optional[str] = None
    value_date: datetime
    operation_date: Optional[datetime] = None
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    payment_count: int = 0
    # Above 1, several movements claim the same payment group (Finacle booked one
    # batch as N entries): the allocation is weighted on a shared basis.
    shared_key_movements: int = 1
    # The real movement was already émargé and could not be withdrawn: its
    # ghosts double count against it until an operator arbitrates.
    parent_emarged: bool = False


class SplitChildOut(BaseModel):
    entry_id: Optional[int] = None
    source_hash: str
    lot_id: Optional[str] = None
    amount: Decimal
    currency: str
    direction: Optional[str] = None
    value_date: datetime
    external_ref: Optional[str] = None
    entry_status: Optional[str] = None
    match_group_id: Optional[int] = None
    payment_count: Optional[int] = None
    bucket_kind: Optional[str] = None
    bucket_pacs008: Optional[str] = None
    bucket_msgid: Optional[str] = None
    bucket_po: Optional[str] = None
    bucket_ref: Optional[str] = None
    synthetic_only: bool = False


class SplitConservation(BaseModel):
    """Two distinct controls, deliberately not mixed.

    Conservation: the ghosts must add back up to the movement. They share out
    its own amount, so ``missing_amount`` is 0 unless a ghost was reaped or
    removed out of band.

    Data quality: ``payment_gap`` = what finacle booked minus what std.Payment
    accounts for. Non-zero is a signal about the datamart (charges, FX, a missing
    payment, a shared aggregate key) — never a hole in the reconciliation.
    """
    parent_amount: Decimal
    children_amount: Decimal
    missing_amount: Decimal
    payment_amount: Decimal
    payment_gap: Decimal
    child_count: int


class SplitDetailResponse(BaseModel):
    parent: SplitParentOut
    children: List[SplitChildOut] = Field(default_factory=list)
    conservation: SplitConservation
