"""Reconciliation entry — live table for ingested transactions + émargement table."""
import enum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base

# Sentinel reco_id for finacle movements whose TransactionParticulars schema is
# known but not yet handled. Stored as-is, retried each run by the ingest_finacle
# DAG, and excluded from auto-matching (alongside NULL reco_id).
UNRESOLVED_RECO_ID = "Not Supported"


class EntryStatus(str, enum.Enum):
    PENDING = "pending"      # not matched yet
    MATCHED = "matched"      # auto-matched
    FORCED = "forced"        # manually forced into a match group
    EXCLUDED = "excluded"    # excluded by a user (with comment)


class EntryDirection(str, enum.Enum):
    DEBIT = "debit"
    CREDIT = "credit"


class ReconciliationEntry(Base):
    """Live table — ingestion inserts parsed entries here. Once reconciliation
    is validated the entries are moved to the émargement table."""
    __tablename__ = "reconciliation_entry"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    flow_id = Column(Integer, ForeignKey("reco.flow.id"), nullable=False)
    ingestion_run_id = Column(Integer, ForeignKey("reco.ingestion_run.id"), nullable=True)

    reco_id = Column(String(128), nullable=True)
    account = Column(String(64), nullable=True)
    currency = Column(String(8), nullable=False)
    amount = Column(Numeric(20, 4), nullable=False)
    direction = Column(Enum(EntryDirection, name="entry_direction"), nullable=True)

    value_date = Column(DateTime(timezone=True), nullable=False)
    operation_date = Column(DateTime(timezone=True), nullable=True)

    event_type = Column(String(32), nullable=True)
    external_ref = Column(String(128), nullable=True)
    file_name = Column(String(512), nullable=True)

    # Finacle movement context (also feeds the reco_id retry; can be empty)
    transaction_particulars = Column(String(512), nullable=True)
    ref_no = Column(String(128), nullable=True)
    remarks_1 = Column(String(255), nullable=True)
    # Raw std.Movement.TransactionID — the business reference, NOT an identity:
    # finacle recycles it daily and one id covers several legs, so it carries no
    # index/constraint and stays out of every hash. ``external_ref`` derives from
    # it (suffixed / MID: fallback) and remains the identity key.
    transaction_id = Column(String(128), nullable=True)

    payload_raw = Column(JSONB, nullable=True)
    source_hash = Column(String(64), nullable=False)

    status = Column(Enum(EntryStatus, name="entry_status"), default=EntryStatus.PENDING, nullable=False)
    match_group_id = Column(BigInteger, nullable=True)
    matched_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_hash", name="uq_reconciliation_entry_source_hash"),
        Index("ix_reconciliation_entry_flow_status_date", "flow_id", "status", "value_date"),
        Index("ix_reconciliation_entry_reco_id", "reco_id"),
        Index("ix_reconciliation_entry_match_group", "match_group_id"),
        Index("ix_reconciliation_entry_value_date", "value_date"),
        {"schema": "reco"},
    )


class ReconciliationEntryEmargement(Base):
    """Émargement table — entries land here once reconciliation is done and
    validated (matched, forced, or excluded)."""
    __tablename__ = "reconciliation_entry_emargement"

    id = Column(BigInteger, primary_key=True, autoincrement=False)
    flow_id = Column(Integer, nullable=False)
    ingestion_run_id = Column(Integer, nullable=True)
    reco_id = Column(String(128), nullable=True)
    account = Column(String(64), nullable=True)
    currency = Column(String(8), nullable=False)
    amount = Column(Numeric(20, 4), nullable=False)
    direction = Column(Enum(EntryDirection, name="entry_direction"), nullable=True)
    value_date = Column(DateTime(timezone=True), nullable=False)
    operation_date = Column(DateTime(timezone=True), nullable=True)
    event_type = Column(String(32), nullable=True)
    external_ref = Column(String(128), nullable=True)
    file_name = Column(String(512), nullable=True)
    transaction_particulars = Column(String(512), nullable=True)
    ref_no = Column(String(128), nullable=True)
    remarks_1 = Column(String(255), nullable=True)
    transaction_id = Column(String(128), nullable=True)
    payload_raw = Column(JSONB, nullable=True)
    source_hash = Column(String(64), nullable=False)
    status = Column(Enum(EntryStatus, name="entry_status"), nullable=False)
    match_group_id = Column(BigInteger, nullable=True)
    matched_at = Column(DateTime(timezone=True), nullable=True)
    emarged_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("source_hash", name="uq_reconciliation_entry_emargement_source_hash"),
        Index("ix_emargement_flow_status_date", "flow_id", "status", "value_date"),
        Index("ix_emargement_reco_id", "reco_id"),
        Index("ix_emargement_match_group", "match_group_id"),
        Index("ix_emargement_value_date", "value_date"),
        {"schema": "reco"},
    )

    def __repr__(self) -> str:
        return f"<ReconciliationEntryEmargement(id={self.id}, flow_id={self.flow_id}, reco_id={self.reco_id}, amount={self.amount}, value_date={self.value_date}, status={self.status})>"
