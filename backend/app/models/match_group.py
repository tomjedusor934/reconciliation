"""Match group — set of reconciliation entries proven to balance to zero."""
import enum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.db.base import Base


class MatchMode(str, enum.Enum):
    AUTO = "auto"
    FORCED = "forced"


class MatchGroup(Base):
    __tablename__ = "match_group"
    __table_args__ = {"schema": "reco"}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    flow_id = Column(Integer, ForeignKey("reco.flow.id"), nullable=False, index=True)
    reco_id = Column(String(128), nullable=True, index=True)
    currency = Column(String(8), nullable=False)
    total = Column(Numeric(20, 4), default=0, nullable=False)
    mode = Column(Enum(MatchMode, name="match_mode"), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("user.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    comment = Column(Text, nullable=True)
    reconciliation_run_id = Column(Integer, ForeignKey("reco.reconciliation_run.id"), nullable=True)

    flow = relationship("Flow", lazy="joined")
