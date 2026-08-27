"""Pydantic schemas for movement splits (claim groups, their parents, their ghosts)."""
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# ── Internal (ingest_finacle_bb DAG) payloads ───────────────────────

class SplitChildIn(BaseModel):
    """One ghost to materialise. Its identity is ``external_ref`` — a pure
    function of (claim key, bucket), see ``group_ghost_ref`` in the DAG — and
    the backend hashes it exactly like any other finacle movement, anchored on
    the group's canonical parent, so the ghost entry and its lot member land on
    the same source_hash without either side sending a hash over the wire."""
    external_ref: str = Field(min_length=1, max_length=128)
    # The reco_id the ghost will carry: a lot uuid on a batch-booking flow, the
    # flow's own reconciliation key on a classic bulk flow (which has no lots at
    # all — see services/rcp_link_service.py). Bounded like
    # ``reconciliation_entry.reco_id``, not like a uuid.
    lot_id: str = Field(min_length=1, max_length=128)
    amount: Decimal
    direction: Optional[str] = None
    payment_count: int = 0
    bucket_kind: str = ""
    bucket_pacs008: str = ""
    bucket_msgid: str = ""
    bucket_po: str = ""


class SplitParentIn(BaseModel):
    """One real movement of a claim group. Its ``source_hash`` is NOT sent: the
    backend derives it from the identity fields exactly as it does for a lot
    member, which is what guarantees the withdrawal hits the row the finacle
    push created. Carries no children — ghosts belong to the GROUP."""
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


class SplitGroupIn(BaseModel):
    """One claim group: every split parent of the run resolving one aggregate
    key, plus the group's ghosts (one per bucket, at the bucket's exact payment
    sum). A group always travels WHOLE — the backend reaps, for every group it
    is given, the PENDING ghosts absent from the payload.

    ``account``/dates are the RUN's canonical parent's (oldest by value_date):
    a fallback for a brand-new group. When the group already exists, the backend
    re-anchors the ghosts on the STORED canonical so their hashes never move.
    """
    claim_key_type: str = Field(min_length=1, max_length=16)
    claim_key_value: str = Field(min_length=1, max_length=128)
    account: Optional[str] = None
    currency: str = "EUR"
    value_date: datetime
    operation_date: Optional[datetime] = None
    event_type: Optional[str] = None
    parents: List[SplitParentIn] = Field(default_factory=list)
    children: List[SplitChildIn] = Field(default_factory=list)


class SplitBatchIn(BaseModel):
    flow_code: str
    source_code: str
    # Stamped on the ghost entries this batch creates, so they belong to the run
    # that produced them like any other ingested movement.
    run_id: Optional[int] = None
    groups: List[SplitGroupIn] = Field(default_factory=list)


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
    # batch as N entries): the group reconciliation is the meaningful control.
    shared_key_movements: int = 1
    # The claim group this parent belongs to.
    claim_key_type: Optional[str] = None
    claim_key_value: Optional[str] = None
    # The real movement was already émargé and could not be withdrawn: its
    # group's ghosts double count against it until an operator arbitrates.
    parent_emarged: bool = False


class SplitSiblingOut(BaseModel):
    """A parent of the same claim group, compact (the group list in the drawer)."""
    source_hash: str
    movement_type: str
    external_ref: Optional[str] = None
    amount: Decimal
    currency: str
    value_date: datetime
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


class SplitGroupOut(BaseModel):
    """The claim group's reconciliation — the control that replaced per-parent
    conservation. ``delta`` non-zero means the booked movements and the ghosts
    that stand for them do not add up: every lot carrying one of the group's
    ghosts is tagged ``parent_mismatch`` by the reconcile run."""
    claim_key_type: str
    claim_key_value: str
    canonical_source_hash: Optional[str] = None
    parents: List[SplitSiblingOut] = Field(default_factory=list)
    parent_total: Decimal = Decimal("0")
    children_total: Decimal = Decimal("0")
    delta: Decimal = Decimal("0")
    # Σ SettlementAmount the group's payments account for (max over the parents'
    # payment_amount views — informational).
    payment_amount: Decimal = Decimal("0")


class SplitDetailResponse(BaseModel):
    parent: SplitParentOut
    group: SplitGroupOut
    children: List[SplitChildOut] = Field(default_factory=list)
