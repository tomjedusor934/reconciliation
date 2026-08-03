"""Reconciliation run — trace of one execution of the daily auto engine."""
from sqlalchemy import Column, DateTime, Integer, String

from app.db.base import Base


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_run"
    __table_args__ = {"schema": "reco"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    entries_scanned = Column(Integer, default=0, nullable=False)
    groups_created = Column(Integer, default=0, nullable=False)
    entries_matched = Column(Integer, default=0, nullable=False)
    duration_ms = Column(Integer, nullable=True)
    triggered_by = Column(String(64), nullable=True)  # 'airflow' / 'manual:<user>'
