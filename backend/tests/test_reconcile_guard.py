"""Concurrency guard around the reconciliation engine (DB-free).

Covers the three layers of the fix for the 2026-07-16 deadlock, where two
concurrent run_auto walked the same rows through different indexes and locked
them in opposite orders:

  * app.db.locks — the advisory-lock context manager, exercised against a fake
    engine (no Postgres);
  * ReconciliationService.run_auto / EmargementService.sweep_matched — the guard
    and the rollback-on-failure, exercised with a MagicMock Session;
  * the /tasks endpoints — the 200/skipped contract.

app.main is never imported (it connects to the DB at import time). Importing
app.db.locks is safe: create_engine does not connect, and the engine is lazy.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import NullPool

from app.api.v1 import deps
from app.api.v1.endpoints import tasks as tasks_endpoint
from app.db import locks
from app.db.locks import ADVISORY_LOCK_CLASS, LockKey, try_advisory_lock
from app.services.archive_service import emargement_service
from app.services.reconciliation_service import (
    ReconciliationAlreadyRunning,
    reconciliation_service,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    """Records every statement, so tests can assert on the SQL actually sent."""

    def __init__(self, *, acquired=True, unlock_raises=False):
        self.calls = []          # list of (sql_text, params)
        self.closed = False
        self._acquired = acquired
        self._unlock_raises = unlock_raises

    def execute(self, sql, params=None):
        text = str(sql)
        self.calls.append((text, params))
        if "pg_try_advisory_lock" in text:
            return _FakeResult(self._acquired)
        if "pg_advisory_unlock" in text:
            if self._unlock_raises:
                raise RuntimeError("connection went away")
            return _FakeResult(True)
        raise AssertionError(f"unexpected statement: {text}")

    def close(self):
        self.closed = True


class _FakeEngine:
    def __init__(self, conn):
        self._conn = conn

    def connect(self):
        return self._conn


@pytest.fixture()
def fake_conn(monkeypatch):
    """Install a fake lock engine; yields the connection the CM will use."""
    conn = _FakeConn()

    def _install(**kwargs):
        for k, v in kwargs.items():
            setattr(conn, f"_{k}", v)
        monkeypatch.setattr(locks, "_get_lock_engine", lambda: _FakeEngine(conn))
        return conn

    _install()
    return conn


def _sql_sent(conn):
    return " | ".join(text for text, _ in conn.calls)


# ---------------------------------------------------------------------------
# A. The context manager
# ---------------------------------------------------------------------------

def test_lock_engine_is_nullpool_and_autocommit(monkeypatch):
    """Pins the two properties the whole guard's safety rests on.

    NullPool → close() really closes → Postgres drops the lock even if the unlock
    or the process dies. A pooled connection would return to the pool still
    holding it, blocking every later run for good.
    AUTOCOMMIT → no "idle in transaction" pinning a snapshot (and blocking VACUUM
    on reconciliation_entry) for the whole of run_auto.

    Does not connect: create_engine is lazy.
    """
    monkeypatch.setattr(locks, "_lock_engine", None)
    captured = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(locks, "create_engine", fake_create_engine)
    locks._get_lock_engine()

    assert captured["poolclass"] is NullPool
    assert captured["isolation_level"] == "AUTOCOMMIT"


def test_lock_engine_is_a_singleton(monkeypatch):
    monkeypatch.setattr(locks, "_lock_engine", None)
    calls = []
    monkeypatch.setattr(
        locks, "create_engine", lambda url, **kw: calls.append(url) or object()
    )
    first = locks._get_lock_engine()
    second = locks._get_lock_engine()
    assert first is second
    assert len(calls) == 1


def test_acquired_yields_true_then_unlocks_and_closes(fake_conn):
    with try_advisory_lock(LockKey.RECONCILIATION_ENTRY_WRITER) as acquired:
        assert acquired is True

    sql = _sql_sent(fake_conn)
    assert "pg_try_advisory_lock" in sql
    assert "pg_advisory_unlock" in sql
    assert fake_conn.closed is True


def test_uses_the_two_int_key_form(fake_conn):
    """Pins the key convention: the 2-int space is disjoint from the bigint one
    (pg_locks.objsubid 2 vs 1), and reads back without decoding a bigint."""
    with try_advisory_lock(LockKey.RECONCILIATION_ENTRY_WRITER):
        pass

    _, params = fake_conn.calls[0]
    assert params == {"cls": ADVISORY_LOCK_CLASS, "key": 1}


def test_not_acquired_yields_false_and_never_unlocks(fake_conn):
    """Unlocking a lock we never held returns false and logs a server-side
    WARNING for nothing — the unlock must be guarded by `acquired`."""
    fake_conn._acquired = False

    with try_advisory_lock(LockKey.RECONCILIATION_ENTRY_WRITER) as acquired:
        assert acquired is False

    assert "pg_advisory_unlock" not in _sql_sent(fake_conn)
    assert fake_conn.closed is True


def test_closes_and_unlocks_when_body_raises(fake_conn):
    with pytest.raises(RuntimeError, match="boom"):
        with try_advisory_lock(LockKey.RECONCILIATION_ENTRY_WRITER):
            raise RuntimeError("boom")

    assert "pg_advisory_unlock" in _sql_sent(fake_conn)
    assert fake_conn.closed is True


def test_unlock_failure_is_swallowed_but_connection_still_closed(fake_conn):
    """A failed unlock must not escape: closing the connection ends the Postgres
    session, which releases the lock anyway."""
    fake_conn._unlock_raises = True

    with try_advisory_lock(LockKey.RECONCILIATION_ENTRY_WRITER) as acquired:
        assert acquired is True

    assert fake_conn.closed is True


def test_unlock_failure_does_not_mask_body_exception(fake_conn):
    fake_conn._unlock_raises = True

    with pytest.raises(ValueError, match="original"):
        with try_advisory_lock(LockKey.RECONCILIATION_ENTRY_WRITER):
            raise ValueError("original")

    assert fake_conn.closed is True


# ---------------------------------------------------------------------------
# B. The services
# ---------------------------------------------------------------------------

@pytest.fixture()
def lock_busy(monkeypatch):
    """Make try_advisory_lock always report the lock as taken."""
    from contextlib import contextmanager

    @contextmanager
    def _busy(key):
        yield False

    monkeypatch.setattr("app.services.reconciliation_service.try_advisory_lock", _busy)
    monkeypatch.setattr("app.services.archive_service.try_advisory_lock", _busy)


@pytest.fixture()
def lock_free(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _free(key):
        yield True

    monkeypatch.setattr("app.services.reconciliation_service.try_advisory_lock", _free)
    monkeypatch.setattr("app.services.archive_service.try_advisory_lock", _free)


def test_run_auto_raises_when_lock_busy_and_creates_no_run(lock_busy, monkeypatch):
    """Pins "lock before creating the run": a skipped run must leave no phantom
    finished_at=NULL row in reco.reconciliation_run."""
    created = []
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_run_repository.create",
        lambda db, **kw: created.append(kw) or MagicMock(),
    )

    with pytest.raises(ReconciliationAlreadyRunning):
        reconciliation_service.run_auto(MagicMock(), triggered_by="airflow")

    assert created == []


def test_run_auto_rolls_back_and_finalizes_on_error(lock_free, monkeypatch):
    """Non-regression for the 2026-07-16 cascade: without the rollback, the
    bookkeeping in the finally hit InFailedSqlTransaction, masked the real error
    and left run 57 with finished_at NULL for good."""
    db = MagicMock()
    run = MagicMock(id=57)
    updates = {}

    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_run_repository.create",
        lambda _db, **kw: run,
    )
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_run_repository.update",
        lambda _db, run, **fields: updates.update(fields) or run,
    )
    monkeypatch.setattr(
        reconciliation_service, "resolve_pending_references", lambda _db: 0
    )
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_entry_repository"
        ".find_balanced_groups",
        MagicMock(side_effect=RuntimeError("deadlock detected")),
    )

    # The original error surfaces — not an InFailedSqlTransaction.
    with pytest.raises(RuntimeError, match="deadlock detected"):
        reconciliation_service.run_auto(db, triggered_by="airflow")

    db.rollback.assert_called_once()
    assert updates["finished_at"] is not None
    assert "duration_ms" in updates


def test_run_auto_finalize_failure_does_not_mask_original_error(lock_free, monkeypatch):
    """An exception raised in a finally replaces the propagating one — the
    bookkeeping must never erase the root cause."""
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_run_repository.create",
        lambda _db, **kw: MagicMock(id=57),
    )
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_run_repository.update",
        MagicMock(side_effect=RuntimeError("bookkeeping blew up")),
    )
    monkeypatch.setattr(
        reconciliation_service, "resolve_pending_references", lambda _db: 0
    )
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_entry_repository"
        ".find_balanced_groups",
        MagicMock(side_effect=RuntimeError("the real cause")),
    )

    with pytest.raises(RuntimeError, match="the real cause"):
        reconciliation_service.run_auto(MagicMock(), triggered_by="airflow")


def test_run_auto_returns_run_when_lock_free(lock_free, monkeypatch):
    run = MagicMock(id=58)
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_run_repository.create",
        lambda _db, **kw: run,
    )
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_run_repository.update",
        lambda _db, run, **fields: run,
    )
    monkeypatch.setattr(
        reconciliation_service, "resolve_pending_references", lambda _db: 0
    )
    monkeypatch.setattr(
        "app.services.reconciliation_service.reconciliation_entry_repository"
        ".find_balanced_groups",
        lambda _db: [],
    )

    assert reconciliation_service.run_auto(MagicMock()) is run


def test_sweep_matched_raises_when_lock_busy_and_deletes_nothing(lock_busy):
    db = MagicMock()

    with pytest.raises(ReconciliationAlreadyRunning):
        emargement_service.sweep_matched(db)

    db.execute.assert_not_called()
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# C. Endpoint contracts
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(tasks_endpoint.router, prefix="/tasks")
    app.dependency_overrides[deps.get_db] = lambda: None
    app.dependency_overrides[deps.verify_internal_token] = lambda: None
    return TestClient(app)


def test_reconcile_returns_run_payload(client, monkeypatch):
    run = MagicMock(id=58, groups_created=3, entries_matched=12)
    monkeypatch.setattr(reconciliation_service, "run_auto", lambda db, **kw: run)

    resp = client.post("/tasks/reconcile")

    assert resp.status_code == 200
    assert resp.json()["data"]["run_id"] == 58


def test_reconcile_returns_skipped_when_already_running(client, monkeypatch):
    """Locks in the 200/skipped decision against a future "let's harmonise on
    409": a 409 would fail the Airflow task → retry → the run is still going →
    409 again → red DAG over a benign no-op."""
    def _busy(db, **kw):
        raise ReconciliationAlreadyRunning("a reconciliation run is already in progress")

    monkeypatch.setattr(reconciliation_service, "run_auto", _busy)

    resp = client.post("/tasks/reconcile")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"skipped": True, "reason": "already_running"}


def test_emargement_returns_skipped_when_reconcile_running(client, monkeypatch):
    def _busy(db):
        raise ReconciliationAlreadyRunning("a reconciliation run is in progress")

    monkeypatch.setattr(emargement_service, "sweep_matched", _busy)

    resp = client.post("/tasks/emargement")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["data"] == {"skipped": True, "reason": "already_running"}


def test_emargement_returns_moved_count(client, monkeypatch):
    monkeypatch.setattr(emargement_service, "sweep_matched", lambda db: 42)

    resp = client.post("/tasks/emargement")

    assert resp.status_code == 200
    assert resp.json()["data"] == {"moved": 42}
