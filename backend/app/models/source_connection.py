"""Source connection — credentials/path for ingestion (DB or filesystem)."""
import enum

from sqlalchemy import Column, Enum, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class SourceConnectionType(str, enum.Enum):
    POSTGRES = "postgres"
    ORACLE = "oracle"
    MSSQL = "mssql"
    FOLDER = "folder"


class SourceConnection(Base):
    __tablename__ = "source_connection"
    __table_args__ = {"schema": "reco"}

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(64), unique=True, index=True, nullable=False)
    name = Column(String(255), nullable=False)
    type = Column(Enum(SourceConnectionType, name="source_connection_type"), nullable=False)

    # SQLAlchemy URL (e.g. oracle+cx_oracle://user:pwd@host/svc) or a folder path.
    # Secrets should be wrapped in env-var placeholders resolved at runtime.
    dsn = Column(Text, nullable=True)

    extra = Column(JSONB, nullable=True)
