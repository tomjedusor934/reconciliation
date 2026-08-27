"""Split parents — real movements withdrawn in favour of per-bucket ghosts.

A pacs008 bulk is booked as ONE movement on the float account but settles many
payment orders spread over several ``MessageID`` groups, i.e. over several
(PACS008 × MSGID) buckets. Such a movement cannot sit in any single bucket, so
the ingest_finacle_bb DAG registers it HERE, with its real booked amount, and
REMOVES it from ``reconciliation_entry`` (it would double count against the
ghosts standing in for it). This table is the only remaining trace of it, and
what the UI walks to show "this ghost stands for movement PF0051006".

GHOSTS BELONG TO A CLAIM GROUP, NOT TO ONE PARENT. Finacle books one batch as
N entries carrying the same aggregate key (184 NDGB sharing one ``Remarks_1``)
and every one of them resolves the WHOLE payment group — there is no
per-movement attribution. So all the split movements resolving one key form a
CLAIM GROUP (``claim_key_type``/``claim_key_value``, scoped by
``flow_source_id``) and the group emits ONE ghost per bucket, priced at the
bucket's exact payment sum. Each ghost's ``split_parent_hash`` points at the
group's CANONICAL parent — the oldest row by ``(value_date, external_ref)`` —
whose account and dates also anchor the ghost hashes, keeping ghost identities
stable across runs while parents keep joining the group.

The control is therefore GROUP-LEVEL: Σ(parents.amount) vs Σ(ghosts that
exist). The DAG guarantees neither (a charge, an FX difference or an
over-fetched label MessageID all leave a delta); the second reconciliation
measures the delta after every run and tags the lots carrying the group's
ghosts (``movement_lot.parent_mismatch``) so a matched lot whose parent group
does not add up stays visibly not-fully-validated.

``payment_amount`` vs ``amount`` remains the per-movement data-quality signal
(does std.Payment agree with the accounting?), and ``shared_key_movements``
records the group size as seen by the run that pushed the row.
"""
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class MovementSplit(Base):
    """One real movement that was replaced by its ghosts."""
    __tablename__ = "movement_split"

    # The finacle source_hash of the REAL movement — the same value its
    # reconciliation_entry carried before being withdrawn.
    source_hash = Column(String(64), primary_key=True)
    flow_id = Column(Integer, ForeignKey("reco.flow.id"), nullable=False)
    flow_source_id = Column(Integer, ForeignKey("reco.flow_source.id"), nullable=False)

    movement_type = Column(String(16), nullable=False)  # SCTXB/SDDXB/SDXBB/NDGB/…
    external_ref = Column(String(128), nullable=True)
    account = Column(String(64), nullable=True)
    currency = Column(String(8), nullable=False)
    amount = Column(Numeric(20, 4), nullable=False)  # signed, as booked by finacle
    direction = Column(String(8), nullable=True)     # 'debit' / 'credit'
    value_date = Column(DateTime(timezone=True), nullable=False)
    operation_date = Column(DateTime(timezone=True), nullable=True)

    transaction_particulars = Column(String(512), nullable=True)
    ref_no = Column(String(128), nullable=True)
    remarks_1 = Column(String(255), nullable=True)
    payload_raw = Column(JSONB, nullable=True)

    # The claim group this parent belongs to: the aggregate key its payments
    # were resolved through, uppercased (PACS008 / MSGID / PO + value). Ghosts
    # are emitted once per (flow_source_id, claim_key_type, claim_key_value);
    # empty on rows written before claim groups existed.
    claim_key_type = Column(String(16), nullable=False, server_default="")
    claim_key_value = Column(String(128), nullable=False, server_default="")

    # Ghosts of the CLAIM GROUP this parent belongs to (identical on every
    # parent of the group — only the group has children, not the movement).
    child_count = Column(Integer, nullable=False, server_default="0")
    # Payments in the GROUPS this movement resolved — not "payments of this
    # movement": when an aggregate key is shared, every movement resolves the
    # same group (see shared_key_movements).
    payment_count = Column(Integer, nullable=False, server_default="0")
    # Σ of the group's ghosts. NOT expected to equal ``amount`` (nor the group's
    # Σ amount): the delta is what the second reconciliation surfaces.
    child_amount = Column(Numeric(20, 4), nullable=False, server_default="0")
    # Σ SettlementAmount over those groups, signed like the movement. Differing
    # from ``amount`` means std.Payment and the accounting disagree (charges, FX,
    # a payment missing from the datamart, or a shared aggregate key).
    payment_amount = Column(Numeric(20, 4), nullable=False, server_default="0")
    # How many movements resolve through this movement's aggregate key, as seen
    # by the run that pushed this row. 1 is the healthy case; Finacle booking
    # one batch as N entries makes all N claim the same payment group.
    shared_key_movements = Column(Integer, nullable=False, server_default="1")

    # The real movement was already émargé when the split was registered, so it
    # was NOT withdrawn (émargé history is never rewritten — same rule as the
    # retired merge_conflict flag). Its ghosts double count against it until an
    # operator arbitrates.
    parent_emarged = Column(Boolean, nullable=False, server_default="false")

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        Index("ix_movement_split_source_date", "flow_source_id", "value_date"),
        Index("ix_movement_split_external_ref", "external_ref"),
        Index("ix_movement_split_claim", "flow_source_id", "claim_key_type", "claim_key_value"),
        {"schema": "reco"},
    )
