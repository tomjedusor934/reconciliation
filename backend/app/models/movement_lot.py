"""Movement lots — transitive clusters of batch-booked Finacle movements.

Built by the ingest_finacle_bb DAG for ``finacle_batch_booking_true`` sources:
each movement resolves a SET of keys (PACS008 / MSGID / PO) against std.Payment,
movements sharing any key belong to the same lot, and the lot uuid IS the
reco_id carried by the member entries — the standard sum-to-zero engine then
matches per lot with no engine change.

Members join ``reconciliation_entry`` / ``reconciliation_entry_emargement``
via ``source_hash`` (same finacle hash formula, computed backend-side).
Keys carry no lot_id on purpose: a key's lot is derived through its member,
so a lot merge only moves members and nothing can go out of sync.
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
LOT_STATUS_MERGED = "merged"
KEY_TYPES = ("PACS008", "MSGID", "PO")


class MovementLot(Base):
    __tablename__ = "movement_lot"

    id = Column(String(36), primary_key=True)  # uuid4 string == the entries' reco_id
    flow_id = Column(Integer, ForeignKey("reco.flow.id"), nullable=False)
    flow_source_id = Column(Integer, ForeignKey("reco.flow_source.id"), nullable=False)
    currency = Column(String(8), nullable=False, default="EUR")

    status = Column(String(16), nullable=False, default=LOT_STATUS_ACTIVE)  # active | merged
    merged_into_lot_id = Column(String(36), ForeignKey("reco.movement_lot.id"), nullable=True)
    merged_at = Column(DateTime(timezone=True), nullable=True)
    # A merge relinked live entries, but the absorbed lot already had emarged
    # entries that keep the old reco_id — surfaced in the UI, never auto-fixed.
    merge_conflict = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
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
