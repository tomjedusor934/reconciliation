"""A movement replaced by its ghosts must not come back at the next ingestion.

WHY THIS EXISTS. The reattribution tool (and the batch-booking DAG before it)
withdraws a real movement from ``reconciliation_entry`` and stands N ghosts in
its place. Nothing in ``upsert_finacle`` knew about that: the next run
re-inserted the very row the ghosts replace, and the flow counted both — a
double count that only a second pass of the correction could undo.

The guard reads ``movement_split``; whether a group still HAS ghosts is a
question for Postgres and is locked by compiling the statement
(tests/test_sql_compiles.py). What is locked here is the wiring: which rows
survive the filter, and that the skipped ones are reported rather than lost.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from app.repositories import reconciliation_entry_repository as module
from app.repositories.reconciliation_entry_repository import (
    reconciliation_entry_repository,
)

NOW = datetime(2026, 8, 20, 12, 0, 0, tzinfo=timezone.utc)


def _row(source_hash: str, amount: str = "-100.00") -> dict:
    return {
        "flow_id": 16,
        "ingestion_run_id": 9,
        "reco_id": "lot-a",
        "account": "0010130015001",
        "currency": "EUR",
        "amount": Decimal(amount),
        "direction": "debit",
        "value_date": NOW,
        "operation_date": NOW,
        "event_type": "TR",
        "external_ref": f"REF-{source_hash[:4]}",
        "transaction_particulars": "SCTXB/I/BLK1",
        "ref_no": None,
        "remarks_1": "BLK1",
        "transaction_id": f"TX-{source_hash[:4]}",
        "payload_raw": {},
        "source_hash": source_hash,
        "split_parent_hash": None,
        "status": "pending",
    }


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    """Answers the émargement pre-check and records what would be written."""

    def __init__(self, emarged=()):
        self._emarged = [(h,) for h in emarged]
        self.statements = []

    def query(self, *entities):
        return _FakeQuery(self._emarged)

    def execute(self, statement):
        self.statements.append(statement)
        return _FakeResult(statement)

    def commit(self):
        pass


class _FakeResult:
    def __init__(self, statement):
        self._statement = statement

    def fetchall(self):
        # One "freshly inserted" flag per row the statement carries.
        return [(True,)] * len(inserted_hashes(self._statement))


def inserted_hashes(statement):
    params = statement.compile(dialect=postgresql.dialect()).params
    return [v for k, v in params.items() if k.startswith("source_hash")]


@pytest.fixture
def replaced(monkeypatch):
    """Whatever the test declares as still replaced by its ghosts."""
    seen = {}

    def declare(hashes):
        def fake(db, *, source_hashes):
            seen["asked"] = list(source_hashes)
            return {h for h in source_hashes if h in hashes}

        monkeypatch.setattr(
            module.movement_split_repository, "parents_still_replaced", fake
        )
        return seen

    return declare


def test_a_movement_replaced_by_its_ghosts_is_not_re_inserted(replaced):
    replaced({"a" * 64})
    db = _FakeSession()

    inserted, updated, skipped = reconciliation_entry_repository.upsert_finacle(
        db, [_row("a" * 64), _row("b" * 64)]
    )

    assert skipped == 1
    assert inserted == 1 and updated == 0
    assert inserted_hashes(db.statements[0]) == ["b" * 64]


def test_nothing_is_written_when_every_row_is_a_withdrawn_parent(replaced):
    replaced({"a" * 64, "b" * 64})
    db = _FakeSession()

    inserted, updated, skipped = reconciliation_entry_repository.upsert_finacle(
        db, [_row("a" * 64), _row("b" * 64)]
    )

    assert (inserted, updated, skipped) == (0, 0, 2)
    assert db.statements == []


def test_the_guard_is_inert_when_no_split_claims_the_movement(replaced):
    replaced(set())
    db = _FakeSession()

    inserted, _updated, skipped = reconciliation_entry_repository.upsert_finacle(
        db, [_row("a" * 64), _row("b" * 64)]
    )

    assert skipped == 0 and inserted == 2
    assert inserted_hashes(db.statements[0]) == ["a" * 64, "b" * 64]


def test_a_group_with_no_ghost_left_lets_its_movement_come_back(replaced):
    """The only undo there is: drop the ghosts and the next ingestion restores
    the real movement. So the guard must ask about ghosts, not about parents."""
    asked = replaced(set())
    db = _FakeSession()

    reconciliation_entry_repository.upsert_finacle(db, [_row("a" * 64)])

    assert asked["asked"] == ["a" * 64]
    assert inserted_hashes(db.statements[0]) == ["a" * 64]


def test_emarged_rows_and_withdrawn_parents_are_both_skipped(replaced):
    asked = replaced({"b" * 64})
    db = _FakeSession(emarged=["a" * 64])

    inserted, _updated, skipped = reconciliation_entry_repository.upsert_finacle(
        db, [_row("a" * 64), _row("b" * 64), _row("c" * 64)]
    )

    assert skipped == 2 and inserted == 1
    # An émargé row never even reaches the guard — it is already immutable.
    assert asked["asked"] == ["b" * 64, "c" * 64]
    assert inserted_hashes(db.statements[0]) == ["c" * 64]
