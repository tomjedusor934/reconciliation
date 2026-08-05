"""Pydantic schemas for movement lots (Finacle Batch Booking True)."""
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

# ── UI responses ─────────────────────────────────────────────────────

class LotSummary(BaseModel):
    lot_id: str
    flow_id: int
    flow_source_id: int
    currency: str
    currencies: List[str] = Field(default_factory=list)
    status: str  # pending | matched | merged (derived)
    is_balanced: bool
    member_count: int
    pending_count: int
    matched_count: int
    excluded_count: int
    total_debit: Decimal
    total_credit: Decimal
    net_amount: Decimal
    pending_payment_amount: Decimal = Decimal("0")  # Σ amount of members with a PDNG payment
    pending_payment_count: int = 0
    first_value_date: Optional[datetime] = None
    last_value_date: Optional[datetime] = None
    merge_conflict: bool = False
    merged_into_lot_id: Optional[str] = None
    # What the bucket stands for. LEGACY = lot inherited from the retired
    # union-find clustering, with no (pacs008, MessageID) identity to show.
    bucket_kind: str = "LEGACY"
    bucket_pacs008: Optional[str] = None
    bucket_msgid: Optional[str] = None
    bucket_po: Optional[str] = None
    bucket_ref: Optional[str] = None
    # Every member is a ghost → the lot nets to zero by construction; the real
    # evidence is the parent conservation in GET /splits/{parent_source_hash}.
    synthetic_only: bool = False
    created_at: datetime
    updated_at: datetime


class PaginatedLotsResponse(BaseModel):
    items: List[LotSummary]
    total_count: int


class LotMemberOut(BaseModel):
    id: int
    source_hash: str
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
    # Set on ghosts: the real movement this member is a slice of.
    split_parent_hash: Optional[str] = None
    split_parent_external_ref: Optional[str] = None
    split_parent_amount: Optional[Decimal] = None
    payment_count: Optional[int] = None
    entry_status: Optional[str] = None  # pending|matched|forced|excluded|None (not ingested yet)
    entry_id: Optional[int] = None
    match_group_id: Optional[int] = None


class LotKeyOut(BaseModel):
    id: int
    member_id: int
    key_type: str  # PACS008 | MSGID | PO
    key_value: str


class LotDetailResponse(BaseModel):
    lot: LotSummary
    members: List[LotMemberOut]
    keys: List[LotKeyOut]
    # False for giant lots (members/keys not inlined): the UI must use
    # GET /lots/{id}/graph + GET /lots/{id}/members instead.
    members_included: bool = True


class PaginatedLotMembersResponse(BaseModel):
    items: List[LotMemberOut]
    total_count: int


class LotKeyTypeSummaryOut(BaseModel):
    """One of the (up to) 3 mega link nodes: PACS008 / MSGID / PO."""
    key_type: str  # PACS008 | MSGID | PO
    distinct_count: int  # nb of distinct key_value — the "number of links" shown
    member_link_count: int  # total member↔key links of this type (secondary)


class LotTypeDirectionGroupOut(BaseModel):
    """One movement node: members grouped by (movement_type, direction)."""
    movement_type: str
    direction: str  # debit | credit
    member_count: int
    total_amount: Decimal
    pending_count: int
    matched_count: int
    excluded_count: int
    pending_payment_amount: Decimal = Decimal("0")  # Σ amount of this group's PDNG-payment members


class LotGraphEdgeOut(BaseModel):
    """A link: (movement_type, direction) group → a key-type node."""
    movement_type: str
    direction: str
    key_type: str


class LotGraphMeta(BaseModel):
    member_count: int
    type_counts: Dict[str, int] = Field(default_factory=dict)
    pending_payment_amount: Decimal = Decimal("0")  # lot-wide Σ (aggregate pending node)
    pending_payment_count: int = 0


class LotGraphResponse(BaseModel):
    lot_id: str
    key_types: List[LotKeyTypeSummaryOut] = Field(default_factory=list)
    groups: List[LotTypeDirectionGroupOut] = Field(default_factory=list)
    edges: List[LotGraphEdgeOut] = Field(default_factory=list)
    meta: LotGraphMeta


class LotKeyValueOut(BaseModel):
    key_value: str
    member_count: int  # how many members of the lot carry this exact value


class PaginatedLotKeyValuesResponse(BaseModel):
    items: List[LotKeyValueOut]
    total_count: int


# ── Internal (ingest_finacle_bb DAG) payloads ───────────────────────

class LotIn(BaseModel):
    """A bucket to create. ``lot_id`` is the uuid5 the DAG derived from the
    identity below, so the identity is descriptive here, never re-derived."""
    lot_id: str = Field(min_length=1, max_length=36)
    bucket_kind: str  # validated against movement_lot.BUCKET_KINDS in the service
    bucket_pacs008: str = Field(default="", max_length=128)
    bucket_msgid: str = Field(default="", max_length=128)
    bucket_po: str = Field(default="", max_length=64)
    bucket_ref: str = Field(default="", max_length=64)


class LotKeyIn(BaseModel):
    key_type: str  # validated against movement_lot.KEY_TYPES in the service
    key_value: str = Field(min_length=1, max_length=128)


class LotMemberIn(BaseModel):
    lot_id: str = Field(min_length=1, max_length=36)
    movement_type: str = Field(min_length=1, max_length=16)
    external_ref: Optional[str] = None
    account: Optional[str] = None
    currency: str = "EUR"
    amount: Decimal
    value_date: datetime
    operation_date: Optional[datetime] = None
    direction: Optional[str] = None
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    # Ghosts only: the real movement's external_ref. The hash itself is derived
    # backend-side (the DAG has no way to compute it) from this plus the account
    # and dates — which a ghost shares with its parent, being the same movement.
    split_parent_external_ref: Optional[str] = Field(default=None, max_length=128)
    payment_count: Optional[int] = None
    keys: List[LotKeyIn] = Field(default_factory=list)


class LotBatchIn(BaseModel):
    flow_code: str
    source_code: str
    lots: List[LotIn] = Field(default_factory=list)
    members: List[LotMemberIn] = Field(default_factory=list)
