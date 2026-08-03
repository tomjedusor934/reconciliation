"""Ingestion run — trace of one parser/extractor execution."""
import enum

from sqlalchemy import Column, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.db.base import Base


class IngestionStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class IngestionRun(Base):
    __tablename__ = "ingestion_run"
    __table_args__ = {"schema": "reco"}

    id = Column(Integer, primary_key=True, index=True)
    flow_id = Column(Integer, ForeignKey("reco.flow.id", ondelete="CASCADE"), nullable=False, index=True)
    flow_source_id = Column(Integer, ForeignKey("reco.flow_source.id", ondelete="SET NULL"), nullable=True, index=True)
    source_file = Column(String(512), nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(Enum(IngestionStatus, name="ingestion_status"), default=IngestionStatus.PENDING, nullable=False)
    rows_in = Column(Integer, default=0, nullable=False)
    rows_ok = Column(Integer, default=0, nullable=False)
    rows_ko = Column(Integer, default=0, nullable=False)
    rows_duplicate = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)

    flow = relationship("Flow", lazy="joined")
    flow_source = relationship("FlowSource", lazy="joined")

    def __repr__(self) -> str:
        return (
            f"<IngestionRun id={self.id} flow_id={self.flow_id} "
            f"flow_source_id={self.flow_source_id} source_file={self.source_file} "
            f"status={self.status} rows_in={self.rows_in} rows_ok={self.rows_ok} "
            f"rows_ko={self.rows_ko} rows_duplicate={self.rows_duplicate}>"
        )