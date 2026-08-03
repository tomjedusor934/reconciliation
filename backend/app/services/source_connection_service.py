"""Source connections: CRUD + a cached SQLAlchemy engine per connection.

The secret (DB password) is stored Fernet-encrypted in ``extra["password_enc"]``
(same crypto helper as SSO secrets) and NEVER returned by any response — the
API exposes only ``has_password``. ``get_engine`` is the reusable brick the
future transversal/operational datamart enrichments will build on.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple, Union

from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, encrypt
from app.models.source_connection import SourceConnection
from app.repositories.source_connection_repository import source_connection_repository
from app.schemas.source_connection import (
    SourceConnectionWrite,
    SourceConnectionWriteUpdate,
)

logger = logging.getLogger(__name__)

# Structured fields persisted (non-secret) in ``extra`` so the edit form can be
# pre-filled and the DSN rebuilt; the password is kept apart (encrypted).
_STRUCTURED_FIELDS = (
    "host",
    "port",
    "database",
    "username",
    "odbc_driver",
    "encrypt",
    "trust_server_certificate",
)

_DEFAULT_DRIVER = "ODBC Driver 18 for SQL Server"


class SourceConnectionService:
    def __init__(self) -> None:
        # code -> Engine. Invalidated on update so credential changes take hold.
        self._engines: Dict[str, Engine] = {}

    # ---- reads --------------------------------------------------------------
    def list_connections(self, db: Session) -> List[SourceConnection]:
        return source_connection_repository.get_multi(db)

    def get_connection(self, db: Session, connection_id: int) -> Optional[SourceConnection]:
        return source_connection_repository.get(db, id=connection_id)

    # ---- writes -------------------------------------------------------------
    def create_connection(
        self, db: Session, *, payload: SourceConnectionWrite
    ) -> SourceConnection:
        extra, dsn = self._assemble(payload, base_extra={})
        # Build the model directly (not via jsonable_encoder) so the enum member
        # is passed through unchanged.
        conn = SourceConnection(
            code=payload.code,
            name=payload.name,
            type=payload.type,
            dsn=dsn,
            extra=extra or None,
        )
        db.add(conn)
        db.commit()
        db.refresh(conn)
        self._engines.pop(conn.code, None)
        return conn

    def update_connection(
        self, db: Session, *, conn: SourceConnection, payload: SourceConnectionWriteUpdate
    ) -> SourceConnection:
        data = payload.model_dump(exclude_unset=True)
        if data.get("name") is not None:
            conn.name = data["name"]
        if data.get("type") is not None:
            conn.type = data["type"]
        extra, dsn = self._assemble(payload, base_extra=dict(conn.extra or {}))
        conn.extra = extra or None
        if dsn is not None:
            conn.dsn = dsn
        db.add(conn)
        db.commit()
        db.refresh(conn)
        self._engines.pop(conn.code, None)
        return conn

    def _assemble(
        self,
        payload: Union[SourceConnectionWrite, SourceConnectionWriteUpdate],
        *,
        base_extra: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """Merge structured fields into ``extra`` (encrypting a new password)
        and derive the masked display DSN. Returns (extra, dsn); dsn is None
        when nothing changed the connection string (update leaves it as-is)."""
        extra = dict(base_extra)
        for field in _STRUCTURED_FIELDS:
            value = getattr(payload, field, None)
            if value is not None:
                extra[field] = value
        password = getattr(payload, "password", None)
        if password:
            extra["password_enc"] = encrypt(password)

        raw_dsn = getattr(payload, "dsn", None)
        if extra.get("host"):
            secret = decrypt(extra.get("password_enc")) or ""
            # Stored DSN is the masked form (safe to show); real one is rebuilt
            # on demand by resolve_url().
            dsn: Optional[str] = self._build_url(extra, secret).render_as_string(
                hide_password=True
            )
        elif raw_dsn is not None:
            dsn = raw_dsn
        else:
            dsn = None
        return extra, dsn

    @staticmethod
    def _build_url(extra: Dict[str, Any], password: str) -> URL:
        return URL.create(
            "mssql+pyodbc",
            username=extra.get("username"),
            password=password,
            host=extra.get("host"),
            port=extra.get("port"),
            database=extra.get("database"),
            query={
                "driver": extra.get("odbc_driver") or _DEFAULT_DRIVER,
                "Encrypt": "yes" if extra.get("encrypt", True) else "no",
                "TrustServerCertificate": (
                    "yes" if extra.get("trust_server_certificate", True) else "no"
                ),
            },
        )

    # ---- engine -------------------------------------------------------------
    def resolve_url(self, conn: SourceConnection) -> str:
        """The runnable connection URL (with the decrypted secret). Never log
        or return this to a client."""
        extra = conn.extra or {}
        if extra.get("host"):
            secret = decrypt(extra.get("password_enc")) or ""
            return self._build_url(extra, secret).render_as_string(hide_password=False)
        if conn.dsn:
            return conn.dsn
        raise ValueError("Connection has no host or DSN configured")

    def get_engine(self, conn: SourceConnection) -> Engine:
        cached = self._engines.get(conn.code)
        if cached is not None:
            return cached
        engine = create_engine(self.resolve_url(conn), pool_pre_ping=True)
        self._engines[conn.code] = engine
        return engine

    def invalidate(self, code: str) -> None:
        self._engines.pop(code, None)

    # ---- serialization ------------------------------------------------------
    @staticmethod
    def to_public(conn: SourceConnection) -> Dict[str, Any]:
        """Response dict with the secret stripped from ``extra``."""
        extra = dict(conn.extra or {})
        has_password = bool(extra.pop("password_enc", None))
        return {
            "id": conn.id,
            "code": conn.code,
            "name": conn.name,
            "type": conn.type,
            "dsn": conn.dsn,
            "extra": extra,
            "has_password": has_password,
        }


source_connection_service = SourceConnectionService()
