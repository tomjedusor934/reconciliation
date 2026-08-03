from typing import Any, Dict, Optional

from pydantic import BaseModel, ConfigDict

from app.models.source_connection import SourceConnectionType


class SourceConnectionBase(BaseModel):
    code: str
    name: str
    type: SourceConnectionType
    dsn: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


class SourceConnectionCreate(SourceConnectionBase):
    # Maps 1:1 to the model columns — used internally by the repository. The
    # API write path uses SourceConnectionWrite (structured + secret) instead.
    pass


class SourceConnectionUpdate(BaseModel):
    name: Optional[str] = None
    type: Optional[SourceConnectionType] = None
    dsn: Optional[str] = None
    extra: Optional[Dict[str, Any]] = None


# --- API write payloads (structured MSSQL form; secret is write-only) --------

class SourceConnectionWrite(BaseModel):
    """Create payload. Give structured fields (host/…/password) to build a
    SQL Server connection, OR a raw ``dsn`` for other types (folder path, full
    SQLAlchemy URL). The password is never returned by any response."""
    code: str
    name: str
    type: SourceConnectionType = SourceConnectionType.MSSQL
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None  # write-only; stored Fernet-encrypted
    odbc_driver: str = "ODBC Driver 18 for SQL Server"
    encrypt: bool = True
    trust_server_certificate: bool = True
    dsn: Optional[str] = None


class SourceConnectionWriteUpdate(BaseModel):
    """Update payload — every field optional. ``code`` is immutable (it is the
    engine-cache key); omit ``password`` to keep the stored one unchanged."""
    name: Optional[str] = None
    type: Optional[SourceConnectionType] = None
    host: Optional[str] = None
    port: Optional[int] = None
    database: Optional[str] = None
    username: Optional[str] = None
    password: Optional[str] = None
    odbc_driver: Optional[str] = None
    encrypt: Optional[bool] = None
    trust_server_certificate: Optional[bool] = None
    dsn: Optional[str] = None


class SourceConnectionResponse(BaseModel):
    id: int
    code: str
    name: str
    type: SourceConnectionType
    dsn: Optional[str] = None
    # ``extra`` is sanitized (the encrypted secret is stripped); ``has_password``
    # tells the UI whether a secret is stored without ever exposing it.
    extra: Optional[Dict[str, Any]] = None
    has_password: bool = False
    model_config = ConfigDict(from_attributes=True)
