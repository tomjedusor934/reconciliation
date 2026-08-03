from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.models.user import User
from app.schemas.source_connection import (
    SourceConnectionResponse,
    SourceConnectionWrite,
    SourceConnectionWriteUpdate,
)
from app.services.source_connection_service import source_connection_service

router = APIRouter()


@router.get("/", response_model=List[SourceConnectionResponse])
def list_connections(
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return [
        source_connection_service.to_public(c)
        for c in source_connection_service.list_connections(db)
    ]


@router.post("/", response_model=SourceConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_connection(
    payload: SourceConnectionWrite,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    conn = source_connection_service.create_connection(db, payload=payload)
    return source_connection_service.to_public(conn)


@router.put("/{connection_id}", response_model=SourceConnectionResponse)
def update_connection(
    connection_id: int,
    payload: SourceConnectionWriteUpdate,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    conn = source_connection_service.get_connection(db, connection_id)
    if not conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")
    conn = source_connection_service.update_connection(db, conn=conn, payload=payload)
    return source_connection_service.to_public(conn)


class TestQueryRequest(BaseModel):
    query: str


class TestQueryResponse(BaseModel):
    columns: List[str]
    rows: List[List]
    row_count: int
    truncated: bool


_FORBIDDEN_KEYWORDS = [
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE",
    "CREATE", "GRANT", "REVOKE", "EXEC", "EXECUTE", "MERGE",
]


def _validate_query_is_select(query: str) -> None:
    """Raise ValueError if query contains DML/DDL statements."""
    cleaned = query.strip().rstrip(";").strip()
    upper = cleaned.upper()
    if not upper.startswith("SELECT") and not upper.startswith("WITH"):
        raise ValueError("Only SELECT queries (or WITH ... SELECT) are allowed.")
    for kw in _FORBIDDEN_KEYWORDS:
        # Check for keyword as a standalone word
        import re
        if re.search(rf'\b{kw}\b', upper):
            raise ValueError(f"Forbidden keyword detected: {kw}")


@router.post("/{connection_id}/test-query", response_model=TestQueryResponse)
def test_query(
    connection_id: int,
    payload: TestQueryRequest,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    conn = source_connection_service.get_connection(db, connection_id)
    if not conn:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connection not found")

    try:
        _validate_query_is_select(payload.query)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    from sqlalchemy import text

    max_rows = 50
    try:
        # Cached engine built from the connection's resolved credentials
        # (decrypted on demand; a raw DSN is used as-is for non-structured
        # connections). Requires the MSSQL driver (pyodbc + msodbcsql18).
        engine = source_connection_service.get_engine(conn)
        with engine.connect() as connection:
            # Execute in a transaction that we always rollback
            trans = connection.begin()
            try:
                result = connection.execute(text(payload.query))
                columns = list(result.keys())
                rows = [list(row) for row in result.fetchmany(max_rows + 1)]
                truncated = len(rows) > max_rows
                if truncated:
                    rows = rows[:max_rows]
            finally:
                trans.rollback()

        return TestQueryResponse(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Query execution failed: {str(exc)}",
        )
