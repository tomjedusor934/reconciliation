"""Finacle DB extractor — pulls new movements per account through SQLAlchemy.

This is a skeleton: it builds an engine from `SourceConnection.dsn`, runs the
query stored in `flow.finacle_query` (parameterized with `:since` cursor and
account list) and yields ParsedEntry instances.

When the Finacle credentials and exact ODS schema are provided, replace the
TODO with the real query and column mapping.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, List, Optional

from sqlalchemy import create_engine, text

from app.services.parsers.base_parser import BaseParser, ParsedEntry, ParseResult


class FinacleDBExtractor(BaseParser):
    """Not file-based — `parse_file` accepts a URL-like spec instead of a path."""

    def __init__(
        self,
        config: Optional[dict] = None,
        *,
        dsn: Optional[str] = None,
        query: Optional[str] = None,
    ) -> None:
        super().__init__(config=config or {})
        self.dsn = dsn
        self.query = query

    def parse_file(self, file_path: str) -> ParseResult:
        """`file_path` is unused — kept for interface compatibility."""
        return self.extract(since=None)

    def extract(
        self,
        *,
        since: Optional[datetime] = None,
        accounts: Optional[List[str]] = None,
    ) -> ParseResult:
        if not self.dsn or not self.query:
            raise ValueError("FinacleDBExtractor requires `dsn` and `query`")

        result = ParseResult()
        col_map = self.config.get("column_map", {})
        default_currency = self.config.get("default_currency", "EUR")

        engine = create_engine(self.dsn, pool_pre_ping=True)
        try:
            with engine.connect() as conn:
                rows = conn.execute(
                    text(self.query),
                    {"since": since, "accounts": accounts or []},
                ).mappings().all()
                for idx, row in enumerate(rows, start=1):
                    try:
                        result.entries.append(
                            self._map_row(row, col_map, default_currency)
                        )
                    except Exception as exc:  # noqa: BLE001
                        result.errors.append(f"row #{idx}: {exc}")
        finally:
            engine.dispose()
        return result

    @staticmethod
    def _map_row(row, col_map, default_currency: str) -> ParsedEntry:
        def g(key: str):
            col = col_map.get(key, key)
            return row.get(col)

        amount = Decimal(str(g("amount")))
        value_date = g("value_date")
        if isinstance(value_date, str):
            value_date = datetime.fromisoformat(value_date)
        if value_date.tzinfo is None:
            value_date = value_date.replace(tzinfo=timezone.utc)

        return ParsedEntry(
            reco_id=str(g("reco_id")) if g("reco_id") else None,
            account=str(g("account")) if g("account") else None,
            currency=str(g("currency") or default_currency),
            amount=amount,
            value_date=value_date,
            operation_date=value_date,
            direction="credit" if amount >= 0 else "debit",
            event_type=str(g("event_type")) if g("event_type") else None,
            external_ref=str(g("external_ref")) if g("external_ref") else None,
            file_name=None,
            payload_raw={k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in dict(row).items()},
        )
