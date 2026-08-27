"""Persisted trace of one RCP reattribution run.

WHY. The analysis is a batch — minutes, not seconds — and the operator has no
reason to sit in front of it. The in-memory job registry only survives while the
browser keeps polling and the backend keeps running: leave the page for twenty
minutes and the finished report is gone, which is exactly what happened in prod.
So every run is written here when it starts and updated when it ends, and the UI
can list past runs and re-open one whenever.

The full report lives in ``result`` (JSONB). Measured on the 2026-08-13 extract:
2.6 MB of raw JSON for 1 627 movements, which Postgres TOASTs down to ~0.2 MB —
cheap enough to keep a history rather than one row.

The LOGS are deliberately NOT persisted: they are a live commentary, useful
while watching, and they would double the row for no lasting value. What a
finished run needs to explain itself is kept — the ``phase`` it reached and the
``error`` it died on.
"""
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base

KIND_ANALYZE = "analyze"
KIND_COMMIT = "commit"

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_ERROR = "error"


class RcpRun(Base):
    __tablename__ = "rcp_run"
    __table_args__ = {"schema": "reco"}

    # Same uuid as the in-memory job, so a poll can fall back to this row once
    # the registry has forgotten (restart, TTL) without the UI knowing.
    id = Column(String(36), primary_key=True)
    kind = Column(String(16), nullable=False)     # analyze | commit
    status = Column(String(16), nullable=False)   # running | done | error
    phase = Column(String(128), nullable=False, server_default="")
    user_id = Column(Integer, nullable=True)
    # What the run was fed, so a row is recognisable in a list.
    label = Column(String(512), nullable=False, server_default="")

    # Counters denormalised for the history list — reading them must not mean
    # loading a 2.6 MB payload.
    movements = Column(Integer, nullable=False, server_default="0")
    actionable = Column(Integer, nullable=False, server_default="0")
    applied = Column(Integer, nullable=False, server_default="0")
    failed = Column(Integer, nullable=False, server_default="0")

    error = Column(Text, nullable=False, server_default="")
    result = Column(JSONB, nullable=True)

    started_at = Column(DateTime(timezone=True), nullable=False)
    finished_at = Column(DateTime(timezone=True), nullable=True)
