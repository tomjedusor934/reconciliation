"""MOSEL/ATM identity: TMSEXT must keep timestamp-distinct records distinct.

Regression for the ingest crash where two ATM lines sharing
EXTTXNREF/PERIODE/amount/DATOPN/TYPEVT but differing only in TMSEXT collapsed
onto one ``source_hash`` and violated ``uq_reconciliation_entry_source_hash``.

DB-free: imports never touch app.main (which connects to Postgres at import).
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from app.repositories.reconciliation_entry_repository import ReconciliationEntryRepository
from app.services.parsers.base_parser import ParsedEntry
from app.services.parsers.cobol_mosel_parser import CobolMoselParser

FLOW_ID = 7


# --- record builder (fixed-width, offsets per cobol_mosel_parser) -------------
def _record(*, tmsext: str, typevt: str = "DEPOSITS", periode: str = "202607110000",
            datopn: str = "20260710", devise: str = "EUR", montant: str = "100.00",
            extref: str = "000815998928", onoff: str = "O") -> str:
    return (
        "1"
        + " " * 30                 # REFCTR
        + tmsext.ljust(30)         # TMSEXT
        + typevt.ljust(8)
        + periode.ljust(12)
        + datopn.ljust(8)
        + devise.ljust(3)
        + montant.ljust(27)
        + extref.ljust(20)
        + onoff.ljust(1)
    )


def _entry(**overrides) -> ParsedEntry:
    payload = dict(
        reco_id="000815998928",
        account="0010110035001",
        currency="EUR",
        amount=Decimal("100.00"),
        value_date=datetime(2026, 7, 10, tzinfo=timezone.utc),
        operation_date=datetime(2026, 7, 10, tzinfo=timezone.utc),
        direction="credit",
        event_type="DEPOSITS",
        external_ref="202607110000",
    )
    payload.update(overrides)
    return ParsedEntry(**payload)


# --- 1. hash discriminator ----------------------------------------------------
def test_discriminator_splits_otherwise_identical_entries():
    a = _entry(source_discriminator="2026071014064000")
    b = _entry(source_discriminator="2026071014064200")
    assert a.compute_source_hash(FLOW_ID) != b.compute_source_hash(FLOW_ID)


def test_no_discriminator_keeps_prior_hash():
    # A source that never sets source_discriminator must hash exactly as before —
    # unset and empty-string both collapse to the legacy identity.
    assert _entry().compute_source_hash(FLOW_ID) == _entry(
        source_discriminator=""
    ).compute_source_hash(FLOW_ID)


def test_equal_discriminator_collides():
    a = _entry(source_discriminator="2026071014064000")
    b = _entry(source_discriminator="2026071014064000")
    assert a.compute_source_hash(FLOW_ID) == b.compute_source_hash(FLOW_ID)


# --- 2. parser threads TMSEXT into identity -----------------------------------
def test_parser_two_lines_produce_distinct_hashes(tmp_path):
    f = tmp_path / "mosel.txt"
    f.write_text(
        _record(tmsext="2026071014064000") + "\n"
        + _record(tmsext="2026071014064200") + "\n",
        encoding="utf-8",
    )
    parser = CobolMoselParser({"event_type_account_map": {"DEPOSITS": "0010110035001"}})
    result = parser.parse_file(str(f))

    assert not result.errors
    assert len(result.entries) == 2
    e0, e1 = result.entries
    assert e0.source_discriminator == "2026071014064000"
    assert e1.source_discriminator == "2026071014064200"
    assert e0.payload_raw["TMSEXT"] != e1.payload_raw["TMSEXT"]
    assert e0.compute_source_hash(FLOW_ID) != e1.compute_source_hash(FLOW_ID)


# --- 3. bulk_insert never 500s on an identical line ---------------------------
def _mock_session(rowcount: int) -> MagicMock:
    db = MagicMock()
    # both existence queries (live + émargement) return no rows
    db.query.return_value.filter.return_value.all.return_value = []
    db.execute.return_value.rowcount = rowcount
    return db


def test_bulk_insert_collapses_identical_source_hash(caplog):
    repo = ReconciliationEntryRepository()
    rows = [
        {"source_hash": "same", "reco_id": "a"},
        {"source_hash": "same", "reco_id": "b"},
    ]
    db = _mock_session(rowcount=1)
    with caplog.at_level(logging.WARNING):
        inserted, skipped = repo.bulk_insert(db, rows)

    assert inserted == 1
    assert skipped == 1
    assert "in-batch duplicate source_hash" in caplog.text
    db.execute.assert_called_once()  # both duplicates folded into one INSERT
    db.commit.assert_called_once()
