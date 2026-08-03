"""Flow, FlowSource & FlowSourceAccount — definition of a reconciliation channel."""
import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.db.base import Base


class FlowSourceType(str, enum.Enum):
    FILE = "file"            # parser-based ingestion (CSV/XML/Cobol)
    FINACLE_DB = "finacle_db"  # direct DB query


class ParserType(str, enum.Enum):
    COBOL_MOSEL = "cobol_mosel"
    CSV = "csv"
    XML = "xml"
    MT940 = "mt940"
    FINACLE_DB = "finacle_db"
    FINACLE_BATCH_BOOKING_TRUE = "finacle_batch_booking_true"
    EXCEL = "excel"


class MatchKeyStrategy(str, enum.Enum):
    """How rows are grouped to attempt automatic matching."""
    RECO_ID_AMOUNT = "reco_id_amount"          # group by reco_id + currency, sum=0
    FILE_REF_AMOUNT = "file_ref_amount"        # group by file + ref + amount
    REF_AMOUNT = "ref_amount"                  # group by ref + amount


class Flow(Base):
    __tablename__ = "flow"
    __table_args__ = {"schema": "reco"}

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)
    default_currency = Column(String(8), nullable=True)

    sources = relationship(
        "FlowSource", back_populates="flow", cascade="all, delete-orphan",
        order_by="FlowSource.id",
    )


class FlowSource(Base):
    """A single ingestion source within a flow (file parser or DB extractor)."""
    __tablename__ = "flow_source"
    __table_args__ = (
        UniqueConstraint("flow_id", "code", name="uq_flow_source_code"),
        {"schema": "reco"},
    )

    id = Column(Integer, primary_key=True, index=True)
    flow_id = Column(Integer, ForeignKey("reco.flow.id", ondelete="CASCADE"), nullable=False, index=True)
    code = Column(String(64), nullable=False)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    source_type = Column(Enum(FlowSourceType, name="flow_source_type", create_constraint=False), nullable=False)
    parser_type = Column(Enum(ParserType, name="parser_type", create_constraint=False), nullable=False)
    match_key_strategy = Column(
        Enum(MatchKeyStrategy, name="match_key_strategy", create_constraint=False),
        default=MatchKeyStrategy.RECO_ID_AMOUNT,
        nullable=False,
    )
    inbox_subfolder = Column(String(128), nullable=True)
    file_pattern = Column(String(128), nullable=True)
    parser_config = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    flow = relationship("Flow", back_populates="sources")
    accounts = relationship(
        "FlowSourceAccount", back_populates="source", cascade="all, delete-orphan",
        order_by="FlowSourceAccount.id",
    )


class FlowSourceAccount(Base):
    """Reference account attached to a Finacle-DB source (WHERE account IN ...)."""
    __tablename__ = "flow_source_account"
    __table_args__ = (
        UniqueConstraint("flow_source_id", "account_number", name="uq_flow_source_account"),
        {"schema": "reco"},
    )

    id = Column(Integer, primary_key=True, index=True)
    flow_source_id = Column(
        Integer, ForeignKey("reco.flow_source.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_number = Column(String(64), nullable=False)
    label = Column(String(255), nullable=True)

    source = relationship("FlowSource", back_populates="accounts")
