"""Movement lots — one lot per (PACS008 × MSGID) bucket of batch-booked movements.

Built by the ingest_finacle_bb DAG for ``finacle_batch_booking_true`` sources.
A pacs008 batch carries many payment orders; those orders are grouped by their
``std.Payment.MessageID``, and each (pacs008, MessageID) pair is ONE lot. The
lot uuid IS the reco_id carried by the member entries, so the standard
sum-to-zero engine matches per bucket with no engine change.

The bucket is a pure function of its identity (see ``bucket_id`` in the DAG:
``uuid5(ns, "<flow_source_id>|<kind>|<pacs>|<msgid>|<po>|<ref>")``), so the same
bucket always yields the same uuid: nothing to cluster, nothing to merge, no
state to read back between runs. This REPLACES the union-find over
{PACS008, MSGID, PO} keys, whose transitivity glued unrelated pacs008 together
through label-like MessageIDs ('LUXEMBOURG', 'ESCH/ALZETTE') and produced
50k-member lots that could never balance.

A real movement whose payments span several buckets is not placed in any of
them: it is registered as a ``MovementSplit`` parent and replaced by one ghost
entry per bucket (see ``app/models/movement_split.py``).

Members join ``reconciliation_entry`` / ``reconciliation_entry_emargement``
via ``source_hash`` (same finacle hash formula, computed backend-side).
Keys carry no lot_id on purpose: a key's lot is derived through its member.
They are no longer the clustering mechanism — purely descriptive now, feeding
deep search and the key drawer.
"""
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)

from app.db.base import Base

LOT_STATUS_ACTIVE = "active"
# Never written by the bucket pipeline — kept for the lots produced by the
# retired union-find clustering, which could absorb one lot into another.
LOT_STATUS_MERGED = "merged"
KEY_TYPES = ("PACS008", "MSGID", "PO")

# Bucket identity kinds. Absent components are stored as '' (never NULL) so the
# uniqueness constraint actually holds — Postgres treats NULLs as distinct.
BUCKET_PAIR = "PAIR"              # pacs008 AND MessageID — the nominal case
BUCKET_PACS_ONLY = "PACS_ONLY"    # payment carries no MessageID
BUCKET_MSGID_ONLY = "MSGID_ONLY"  # payment carries no pacs008
BUCKET_PO = "PO"                  # neither: single payment, keyed on PaymentNumber
BUCKET_RESIDUAL = "RESIDUAL"      # ref = parent source_hash; holds a split's leftover
BUCKET_LEGACY = "LEGACY"          # lot from the retired union-find clustering
BUCKET_KINDS = (
    BUCKET_PAIR,
    BUCKET_PACS_ONLY,
    BUCKET_MSGID_ONLY,
    BUCKET_PO,
    BUCKET_RESIDUAL,
    BUCKET_LEGACY,
)


class MovementLot(Base):
    __tablename__ = "movement_lot"

    id = Column(String(36), primary_key=True)  # uuid5 of the bucket == the entries' reco_id
    flow_id = Column(Integer, ForeignKey("reco.flow.id"), nullable=False)
    flow_source_id = Column(Integer, ForeignKey("reco.flow_source.id"), nullable=False)
    currency = Column(String(8), nullable=False, default="EUR")

    # Bucket identity — what the lot IS, rather than what happened to cluster
    # into it. '' means "this component is not part of the identity" (see the
    # module docstring for why it is never NULL).
    bucket_kind = Column(String(16), nullable=False, server_default=BUCKET_LEGACY)
    bucket_pacs008 = Column(String(128), nullable=False, server_default="")
    bucket_msgid = Column(String(128), nullable=False, server_default="")
    bucket_po = Column(String(64), nullable=False, server_default="")
    bucket_ref = Column(String(64), nullable=False, server_default="")
    # Every member is a ghost: the lot balances by construction (both sides are
    # derived from the same std.Payment amounts) and proves nothing on its own —
    # the real control is the parent-level conservation in ``movement_split``.
    synthetic_only = Column(Boolean, nullable=False, server_default="false")

    status = Column(String(16), nullable=False, default=LOT_STATUS_ACTIVE)  # active | merged
    merged_into_lot_id = Column(String(36), ForeignKey("reco.movement_lot.id"), nullable=True)
    merged_at = Column(DateTime(timezone=True), nullable=True)
    # A merge relinked live entries, but the absorbed lot already had emarged
    # entries that keep the old reco_id — surfaced in the UI, never auto-fixed.
    merge_conflict = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "flow_source_id", "bucket_kind", "bucket_pacs008", "bucket_msgid",
            "bucket_po", "bucket_ref",
            name="uq_movement_lot_bucket",
        ),
        Index("ix_movement_lot_source_status", "flow_source_id", "status"),
        Index("ix_movement_lot_flow", "flow_id"),
        {"schema": "reco"},
    )


class MovementLotMember(Base):
    """One movement inside a lot (display fields mirror reconciliation_entry)."""
    __tablename__ = "movement_lot_member"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    lot_id = Column(
        String(36), ForeignKey("reco.movement_lot.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    source_hash = Column(String(64), nullable=False)

    movement_type = Column(String(16), nullable=False)  # SCTXB/SDDXB/SDXBB/NDGB/NDRJ/SWIFT/BKRTP
    external_ref = Column(String(128), nullable=True)
    account = Column(String(64), nullable=True)
    currency = Column(String(8), nullable=False)
    amount = Column(Numeric(20, 4), nullable=False)  # signed (debit < 0 / credit > 0)
    direction = Column(String(8), nullable=True)     # 'debit' / 'credit'
    value_date = Column(DateTime(timezone=True), nullable=False)
    operation_date = Column(DateTime(timezone=True), nullable=True)

    transaction_particulars = Column(String(512), nullable=True)
    ref_no = Column(String(128), nullable=True)
    remarks_1 = Column(String(255), nullable=True)

    # Set on ghosts only: source_hash of the real movement this member is a
    # slice of (``movement_split.source_hash``). NULL = the member IS a real
    # movement. Unlike a real movement's, a ghost's amount is mutable: its
    # payment group grows as std.Payment fills in.
    split_parent_hash = Column(String(64), nullable=True, index=True)
    payment_count = Column(Integer, nullable=True)  # payments behind a ghost

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("source_hash", name="uq_movement_lot_member_source_hash"),
        {"schema": "reco"},
    )


class MovementLotKey(Base):
    """One reconciliation key extracted for one member (keys only ever accrue)."""
    __tablename__ = "movement_lot_key"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    member_id = Column(
        BigInteger, ForeignKey("reco.movement_lot_member.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    key_type = Column(String(8), nullable=False)    # PACS008 | MSGID | PO
    key_value = Column(String(128), nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("member_id", "key_type", "key_value", name="uq_movement_lot_key_member"),
        Index("ix_movement_lot_key_value", "key_type", "key_value"),
        {"schema": "reco"},
    )
