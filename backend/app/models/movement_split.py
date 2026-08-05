"""Split parents — real movements replaced by one ghost entry per bucket.

A pacs008 bulk is booked as ONE movement on the float account but settles many
payment orders spread over several ``MessageID`` groups, i.e. over several
(PACS008 × MSGID) buckets. Such a movement cannot sit in any single bucket, so
the ingest_finacle_bb DAG:

1. registers it HERE, with its real booked amount, and
2. pushes one GHOST ``reconciliation_entry`` per bucket, sharing out that amount
   weighted by each bucket's payment sum, carrying
   ``split_parent_hash = <this row's source_hash>``.

The real movement is then REMOVED from ``reconciliation_entry`` (it would double
count against its own ghosts). This table is the only remaining trace of it, and
what the UI walks to show "this ghost is 3/8 of movement PF0051006".

Conservation is the control this design rests on: ``Σ children == amount``,
always — and now by construction, since the ghosts only ever re-allocate the
movement's own money. Pricing a ghost AT its bucket's payment sum instead let a
movement emit a slice unrelated to what the bank booked: 184 NDGB movements
resolved the same 20 000-payment group and each claimed the whole of it,
inflating one bucket to -334 M€.

Whether ``std.Payment`` agrees with the accounting is a DIFFERENT question, and
it lives here as data (``payment_amount`` vs ``amount``) rather than as an
imaginary movement. ``shared_key_movements`` records how many movements resolved
through the same aggregate key, which is what makes such a gap likely.
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

    child_count = Column(Integer, nullable=False, server_default="0")
    # Payments in the GROUPS this movement resolved — not "payments of this
    # movement": when an aggregate key is shared, every movement resolves the
    # same group (see shared_key_movements).
    payment_count = Column(Integer, nullable=False, server_default="0")
    # Σ of the ghosts — equals ``amount`` by construction.
    child_amount = Column(Numeric(20, 4), nullable=False, server_default="0")
    # Σ SettlementAmount over those groups, signed like the movement. Differing
    # from ``amount`` means std.Payment and the accounting disagree (charges, FX,
    # a payment missing from the datamart, or a shared aggregate key).
    payment_amount = Column(Numeric(20, 4), nullable=False, server_default="0")
    # How many movements of the run resolve through this movement's aggregate
    # key. 1 is the healthy case; Finacle booking one batch as N entries makes
    # all N claim the same payment group, so the weights are shared.
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
        {"schema": "reco"},
    )
