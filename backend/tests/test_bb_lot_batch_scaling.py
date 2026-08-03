"""The lot-batch write path must stay proportional to the BATCH, not the source.

A full BB run pushes hundreds of ``/tasks/finacle-bb/lots/batch`` calls, so any
per-batch step that re-reads what earlier batches wrote makes the run quadratic
(a 595k-member run spent hours there). These tests lock the three places where
that regression would come back: the cross-lot-key guard, the currency rollup,
and the unbounded key INSERT.

DB-free: a fake Session records the statements instead of executing them
(app.main is never imported — it connects to Postgres at import time).
"""
from datetime import datetime, timezone

import pytest

from app.repositories.movement_lot_repository import (
    KEY_INSERT_CHUNK,
    movement_lot_repository,
)
from app.services.lot_service import lot_service

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


class _FakeResult:
    def __init__(self, rows=(), rowcount=0):
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows


class _FakeSession:
    """Records every execute(); returns empty results."""

    def __init__(self, rows=()):
        self.calls = []
        self._rows = rows

    def execute(self, statement, params=None):
        self.calls.append((statement, params))
        return _FakeResult(self._rows, rowcount=len(self.calls))

    def commit(self):
        pass

    def rollback(self):
        pass


def _sql(statement) -> str:
    return " ".join(str(statement).split())


# ---------------------------------------------------------------------------
# find_cross_lot_conflicts
# ---------------------------------------------------------------------------

def test_scoped_guard_is_driven_by_the_batch_keys():
    """The batch's keys drive the join, so it is an index lookup on
    ix_movement_lot_key_value instead of a full scan of the source."""
    db = _FakeSession()
    movement_lot_repository.find_cross_lot_conflicts(
        db,
        flow_source_id=7,
        keys=[("PO", "b"), ("PACS008", "a"), ("PO", "b")],
    )

    statement, params = db.calls[0]
    sql = _sql(statement)
    assert "batch_keys" in sql
    assert "JOIN reco.movement_lot_key k ON k.key_type = bk.kt" in sql
    # Deduplicated and paired positionally.
    assert params["key_types"] == ["PACS008", "PO"]
    assert params["key_values"] == ["a", "b"]
    assert params["fsid"] == 7


def test_scoped_guard_with_no_keys_runs_no_query():
    """An all-duplicates batch writes no key: nothing to check, nothing to scan."""
    db = _FakeSession()
    assert movement_lot_repository.find_cross_lot_conflicts(
        db, flow_source_id=7, keys=[]
    ) == []
    assert db.calls == []


def test_unscoped_guard_keeps_the_whole_source_form():
    """keys=None stays available as a one-off audit over the entire source."""
    db = _FakeSession()
    movement_lot_repository.find_cross_lot_conflicts(db, flow_source_id=7)

    statement, params = db.calls[0]
    sql = _sql(statement)
    assert "batch_keys" not in sql
    assert "FROM reco.movement_lot_key k" in sql
    assert "key_types" not in params


# ---------------------------------------------------------------------------
# insert_keys
# ---------------------------------------------------------------------------

def test_insert_keys_chunks_and_sums_rowcounts():
    """One SP bulk member can carry thousands of keys; the statement must stay
    bounded (psycopg2 interpolates client-side, so nothing else bounds it)."""
    rows = [
        {"member_id": i, "key_type": "PO", "key_value": f"po-{i}"}
        for i in range(KEY_INSERT_CHUNK * 2 + 5)
    ]
    db = _FakeSession()

    total = movement_lot_repository.insert_keys(db, rows)

    assert len(db.calls) == 3
    # _FakeSession returns rowcount = call index (1, 2, 3).
    assert total == 6


def test_insert_keys_dedupes_before_chunking():
    rows = [{"member_id": 1, "key_type": "PO", "key_value": "x"}] * 3
    db = _FakeSession()

    movement_lot_repository.insert_keys(db, rows)

    assert len(db.calls) == 1


def test_insert_keys_on_empty_rows_runs_no_query():
    db = _FakeSession()
    assert movement_lot_repository.insert_keys(db, []) == 0
    assert db.calls == []


# ---------------------------------------------------------------------------
# apply_lot_batch wiring
# ---------------------------------------------------------------------------

class _Flow:
    id = 1
    code = "float_account_outward"


class _Source:
    id = 42
    code = "finacle_db"


class _Key:
    def __init__(self, key_type, key_value):
        self.key_type = key_type
        self.key_value = key_value


class _Member:
    def __init__(self, lot_id, external_ref, keys, currency="EUR"):
        self.lot_id = lot_id
        self.movement_type = "SCTXB"
        self.external_ref = external_ref
        self.account = "0010130015001"
        self.currency = currency
        self.amount = "10.00"
        self.direction = "credit"
        self.value_date = NOW
        self.operation_date = NOW
        self.transaction_particulars = None
        self.ref_no = None
        self.remarks_1 = None
        self.keys = keys


class _Lot:
    def __init__(self, lot_id):
        self.lot_id = lot_id


@pytest.fixture()
def captured(monkeypatch):
    """Stub every repository call apply_lot_batch makes, recording the args.

    ``stored_currencies`` stands in for what the lots already carry in DB; tests
    override it to drive the currency-rollup filter.
    """
    seen = {"stored_currencies": {}}

    monkeypatch.setattr(
        movement_lot_repository, "create_lots", lambda db, **kw: len(kw["lot_ids"])
    )
    monkeypatch.setattr(
        movement_lot_repository,
        "lot_currencies",
        lambda db, **kw: {
            lot_id: seen["stored_currencies"].get(lot_id, "EUR")
            for lot_id in kw["lot_ids"]
        },
    )

    def _upsert(db, rows):
        # member_id = position, enough to build the key rows.
        return len(rows), 0, {r["source_hash"]: i for i, r in enumerate(rows)}

    monkeypatch.setattr(movement_lot_repository, "upsert_members", _upsert)

    def _insert_keys(db, rows):
        seen["key_rows"] = rows
        return len(rows)

    monkeypatch.setattr(movement_lot_repository, "insert_keys", _insert_keys)

    def _sync(db, *, lot_ids):
        seen["currency_lot_ids"] = set(lot_ids)

    monkeypatch.setattr(movement_lot_repository, "sync_lot_currencies", _sync)

    def _guard(db, *, flow_source_id, keys=None, limit=5):
        seen["guard_keys"] = keys
        return []

    monkeypatch.setattr(movement_lot_repository, "find_cross_lot_conflicts", _guard)
    return seen


def test_currency_rollup_skips_lots_that_already_match(captured):
    """The mega-lot case: a batch drops members into a lot that already carries
    their currency. Re-deriving it would DISTINCT ON over all 263k of its
    members — on every batch it appears in — and change nothing."""
    settled = "11111111-1111-4111-8111-111111111111"
    moving = "22222222-2222-4222-8222-222222222222"
    db = _FakeSession()
    captured["stored_currencies"] = {settled: "EUR", moving: "EUR"}

    lot_service.apply_lot_batch(
        db,
        flow=_Flow(),
        source=_Source(),
        lots=[],
        merges=[],
        members=[
            _Member(settled, "TX1", [_Key("PO", "po-1")], currency="EUR"),
            _Member(moving, "TX2", [_Key("PACS008", "pacs-1")], currency="USD"),
        ],
    )

    assert captured["currency_lot_ids"] == {moving}
    # The guard still sees both members' keys.
    assert sorted(captured["guard_keys"]) == [("PACS008", "pacs-1"), ("PO", "po-1")]


def test_currency_rollup_still_runs_for_a_lot_declared_in_an_earlier_batch(captured):
    """Lots are all declared in the FIRST POST but their members spread across
    every batch, so the filter cannot key off ``lots`` — a lot still sitting on
    the placeholder currency must be rolled up whenever its members turn up."""
    lot_id = "44444444-4444-4444-8444-444444444444"
    db = _FakeSession()
    captured["stored_currencies"] = {lot_id: "EUR"}  # create_lots placeholder

    lot_service.apply_lot_batch(
        db,
        flow=_Flow(),
        source=_Source(),
        lots=[],  # declared by an earlier batch
        merges=[],
        members=[_Member(lot_id, "TX1", [_Key("PO", "po-1")], currency="GBP")],
    )

    assert captured["currency_lot_ids"] == {lot_id}


def test_guard_sees_every_key_of_a_reclustered_member(captured):
    """A member moving lots carries its WHOLE key set, not just the new key —
    that is what makes scoping the guard to the batch's keys safe."""
    db = _FakeSession()
    lot_id = "33333333-3333-4333-8333-333333333333"

    lot_service.apply_lot_batch(
        db,
        flow=_Flow(),
        source=_Source(),
        lots=[_Lot(lot_id)],
        merges=[],
        members=[
            _Member(
                lot_id,
                "TX1",
                [_Key("PO", "po-1"), _Key("MSGID", "msg-1"), _Key("PACS008", "pacs-1")],
            )
        ],
    )

    assert sorted(captured["guard_keys"]) == [
        ("MSGID", "msg-1"),
        ("PACS008", "pacs-1"),
        ("PO", "po-1"),
    ]
