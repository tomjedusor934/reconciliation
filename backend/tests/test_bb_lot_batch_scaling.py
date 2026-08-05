"""The lot-batch write path must stay proportional to the BATCH, not the source.

A full BB run pushes hundreds of ``/tasks/finacle-bb/lots/batch`` calls, so any
per-batch step that re-reads what earlier batches wrote makes the run quadratic
(a 595k-member run spent hours there). These tests lock the places where that
regression would come back: the two rollups (currency, synthetic_only) and the
unbounded key INSERT.

The cross-lot-key guard that used to live here is gone with the union-find: a
bucket id is a uuid5 of its identity, so a key cannot end up pointing at two
lots by accident and there is nothing to verify after the write.

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
# the rollups
# ---------------------------------------------------------------------------

def test_synthetic_only_rollup_is_scoped_to_the_batchs_lots():
    """Same trap as the currency rollup: a GROUP BY over every member of the
    source, run once per batch, is quadratic."""
    db = _FakeSession()
    movement_lot_repository.sync_synthetic_only(db, lot_ids=["lot-a", "lot-b"])

    statement, params = db.calls[0]
    sql = _sql(statement)
    assert "BOOL_AND(split_parent_hash IS NOT NULL)" in sql
    assert "WHERE lot_id = ANY(:lot_ids)" in sql
    assert params["lot_ids"] == ["lot-a", "lot-b"]


def test_rollups_on_no_lots_run_no_query():
    db = _FakeSession()
    movement_lot_repository.sync_synthetic_only(db, lot_ids=[])
    movement_lot_repository.sync_lot_currencies(db, lot_ids=[])
    assert db.calls == []


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
    def __init__(self, lot_id, external_ref, keys, currency="EUR", split_parent=None):
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
        self.split_parent_external_ref = split_parent
        self.payment_count = None
        self.keys = keys


class _Lot:
    def __init__(self, lot_id, bucket_kind="PAIR"):
        self.lot_id = lot_id
        self.bucket_kind = bucket_kind
        self.bucket_pacs008 = "PACS1"
        self.bucket_msgid = "MSGA"
        self.bucket_po = ""
        self.bucket_ref = ""

    def model_dump(self):
        return {
            "lot_id": self.lot_id,
            "bucket_kind": self.bucket_kind,
            "bucket_pacs008": self.bucket_pacs008,
            "bucket_msgid": self.bucket_msgid,
            "bucket_po": self.bucket_po,
            "bucket_ref": self.bucket_ref,
        }


@pytest.fixture()
def captured(monkeypatch):
    """Stub every repository call apply_lot_batch makes, recording the args.

    ``stored_currencies`` stands in for what the lots already carry in DB; tests
    override it to drive the currency-rollup filter.
    """
    seen = {"stored_currencies": {}}

    monkeypatch.setattr(
        movement_lot_repository, "create_lots", lambda db, **kw: len(kw["buckets"])
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
        seen["member_rows"] = rows
        return len(rows), 0, {r["source_hash"]: i for i, r in enumerate(rows)}

    monkeypatch.setattr(movement_lot_repository, "upsert_members", _upsert)

    def _insert_keys(db, rows):
        seen["key_rows"] = rows
        return len(rows)

    monkeypatch.setattr(movement_lot_repository, "insert_keys", _insert_keys)

    def _sync(db, *, lot_ids):
        seen["currency_lot_ids"] = set(lot_ids)

    monkeypatch.setattr(movement_lot_repository, "sync_lot_currencies", _sync)

    def _sync_synthetic(db, *, lot_ids):
        seen["synthetic_lot_ids"] = set(lot_ids)

    monkeypatch.setattr(movement_lot_repository, "sync_synthetic_only", _sync_synthetic)
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
        members=[
            _Member(settled, "TX1", [_Key("PO", "po-1")], currency="EUR"),
            _Member(moving, "TX2", [_Key("PACS008", "pacs-1")], currency="USD"),
        ],
    )

    assert captured["currency_lot_ids"] == {moving}
    # The synthetic rollup, however, must see every lot the batch touched: a
    # previously all-ghost bucket that just received a real movement has to lose
    # the flag.
    assert captured["synthetic_lot_ids"] == {settled, moving}


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
        members=[_Member(lot_id, "TX1", [_Key("PO", "po-1")], currency="GBP")],
    )

    assert captured["currency_lot_ids"] == {lot_id}


def test_member_keys_are_all_written(captured):
    """A member's whole key set reaches movement_lot_key — it is what the deep
    search and the key drawer navigate."""
    db = _FakeSession()
    lot_id = "33333333-3333-4333-8333-333333333333"

    lot_service.apply_lot_batch(
        db,
        flow=_Flow(),
        source=_Source(),
        lots=[_Lot(lot_id)],
        members=[
            _Member(
                lot_id,
                "TX1",
                [_Key("PO", "po-1"), _Key("MSGID", "msg-1"), _Key("PACS008", "pacs-1")],
            )
        ],
    )

    assert sorted((r["key_type"], r["key_value"]) for r in captured["key_rows"]) == [
        ("MSGID", "msg-1"),
        ("PACS008", "pacs-1"),
        ("PO", "po-1"),
    ]


def test_ghost_members_carry_a_derived_parent_hash(captured):
    """The DAG cannot hash anything, so it sends the parent's external_ref and
    the service derives the hash with the SAME formula split_service used when
    it registered the parent — otherwise a ghost would point at nothing."""
    db = _FakeSession()
    lot_id = "55555555-5555-4555-8555-555555555555"

    lot_service.apply_lot_batch(
        db,
        flow=_Flow(),
        source=_Source(),
        lots=[_Lot(lot_id)],
        members=[
            _Member(lot_id, "TX1~aaaa", [_Key("PO", "po-1")], split_parent="TX1"),
            _Member(lot_id, "TX2", [_Key("PO", "po-2")]),  # a real movement
        ],
    )

    rows = captured["member_rows"]
    ghost = next(r for r in rows if r["external_ref"] == "TX1~aaaa")
    real = next(r for r in rows if r["external_ref"] == "TX2")
    assert real["split_parent_hash"] is None
    assert ghost["split_parent_hash"] == lot_service.member_to_source_hash(
        flow_id=_Flow.id, external_ref="TX1", account="0010130015001",
        value_date=NOW, operation_date=NOW,
    )
    # It is the PARENT's hash, not the ghost's own.
    assert ghost["split_parent_hash"] != ghost["source_hash"]
