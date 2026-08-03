"""Base parser interface + DTO shared by all ingestion sources."""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional


class ParserError(Exception):
    """Generic parser error."""


@dataclass
class ParsedEntry:
    """Normalized representation of one transaction extracted from a source."""

    reco_id: Optional[str]
    account: Optional[str]
    currency: str
    amount: Decimal
    value_date: datetime
    operation_date: Optional[datetime] = None
    direction: Optional[str] = None  # 'debit' / 'credit'
    event_type: Optional[str] = None
    external_ref: Optional[str] = None
    file_name: Optional[str] = None
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    transaction_id: Optional[str] = None  # raw std.Movement.TransactionID (never hashed)
    source_discriminator: Optional[str] = None  # extra identity token (e.g. MOSEL TMSEXT)
    payload_raw: Dict[str, Any] = field(default_factory=dict)

    def compute_source_hash(self, flow_id: int, *, include_reco_id: bool = True) -> str:
        """Stable hash for dedup (file sources). Deterministic for the same row.

        ``include_reco_id=False`` excludes reco_id from the identity — used for
        sources whose reco_id is resolved AFTER ingestion (e.g. MT940 keys looked
        up against finacle entries), so filling reco_id later doesn't change the
        hash and re-ingesting the same file stays a no-op.

        ``source_discriminator`` (when set) is appended so records that are
        otherwise identical on the reconciliation identity but represent distinct
        transactions stay distinct — e.g. MOSEL ATM lines sharing EXTTXNREF but
        carrying different TMSEXT timestamps. It is appended last and only when
        present, so sources that never set it keep their exact prior hash.
        """
        parts = [str(flow_id), self.external_ref or ""]
        if include_reco_id:
            parts.append(self.reco_id or "")
        parts += [
            str(self.amount),
            self.value_date.isoformat() if self.value_date else "",
            self.account or "",
            self.event_type or "",
        ]
        if self.source_discriminator:
            parts.append(self.source_discriminator)
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def compute_finacle_hash(self, flow_id: int) -> str:
        """Stable identity for a finacle movement leg: flow + external_ref + account
        + transaction date.

        reco_id / amount are intentionally excluded so the SAME movement maps to the
        SAME row across runs even as its reco_id enrichment changes (and without being
        sensitive to Decimal scale through the DB during retry). The transaction date
        (operation_date, value_date fallback) is included **as a plain date** because
        Finacle recycles ``TransactionID`` daily: without it, two different movements
        sharing a recycled TransactionID collapse onto one row, and the upsert then
        keeps the first row's amount/value_date while overwriting its reco_id. Date-only
        keeps the hash stable across the naive↔UTC round-trip of the retry path (both
        the fresh and retry batches go through ``_to_parsed``/``_to_utc``).
        """
        day = self.operation_date or self.value_date
        day_key = day.date().isoformat() if day else ""
        key = "|".join([str(flow_id), self.external_ref or "", self.account or "", day_key])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class ParseResult:
    entries: List[ParsedEntry] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


class BaseParser(ABC):
    """Abstract parser. Subclasses parse a single file (or a DB cursor) into ParsedEntry."""

    def __init__(self, config: Optional[Dict[str, Any]] = None) -> None:
        self.config: Dict[str, Any] = config or {}

    @abstractmethod
    def parse_file(self, file_path: str) -> ParseResult:
        """Return parsed entries + collected line-level errors."""

    # Helper for source-of-truth file name normalization
    @staticmethod
    def basename(path: str) -> str:
        import os

        return os.path.basename(path)
