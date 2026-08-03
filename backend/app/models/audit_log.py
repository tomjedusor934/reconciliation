"""Audit logs — populated by Postgres triggers (data) and the API (UI actions)."""
from sqlalchemy import BigInteger, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class AuditLog(Base):
    """Generic data-layer audit, fed by per-table triggers."""
    __tablename__ = "audit_log"
    __table_args__ = {"schema": "audit"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    table_name = Column(String(128), nullable=False, index=True)
    row_pk = Column(String(64), nullable=True, index=True)
    op = Column(String(8), nullable=False)  # INSERT / UPDATE / DELETE
    old_data = Column(JSONB, nullable=True)
    new_data = Column(JSONB, nullable=True)
    user_id = Column(Integer, nullable=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)


class UIActionLog(Base):
    """Application-level audit — UI actions like force, exclude, login, export."""
    __tablename__ = "ui_action_log"
    __table_args__ = {"schema": "audit"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(Integer, nullable=True, index=True)
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(64), nullable=True)
    target_id = Column(String(64), nullable=True)
    details = Column(JSONB, nullable=True)
    ts = Column(DateTime(timezone=True), nullable=False, index=True)
