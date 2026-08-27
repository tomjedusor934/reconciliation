"""The job registry that keeps the reattribution off the request path.

What matters here is not the threading but the contract the UI depends on:
a job is visible the moment it starts, it reports its phase while running, it
ends up either with a result or with a readable error, and it never leaks the
session it worked with.
"""
import threading
import time

import pytest

from app.services import rcp_job_service as module
from app.services.rcp_job_service import (
    STATUS_DONE,
    STATUS_ERROR,
    STATUS_RUNNING,
    RcpJobService,
)


class _FakeRuns:
    """Stands in for the rcp_run table — records what would be persisted."""

    def __init__(self):
        self.opened = []
        self.closed = []
        self.pruned = 0

    def open_run(self, **kwargs):
        self.opened.append(kwargs)

    def close_run(self, **kwargs):
        self.closed.append(kwargs)

    def prune(self, **kwargs):
        self.pruned += 1


@pytest.fixture
def service(monkeypatch):
    """A registry whose worker threads get a fake session and a fake table."""
    closed = []

    class _FakeSession:
        def close(self):
            closed.append(True)

    monkeypatch.setattr(module, "SessionLocal", lambda: _FakeSession())
    runs = _FakeRuns()
    monkeypatch.setattr(module, "rcp_run_repository", runs)
    svc = RcpJobService()
    svc.closed = closed
    svc.runs = runs
    return svc


def _wait(job, timeout=5.0):
    deadline = time.time() + timeout
    while job.status == STATUS_RUNNING and time.time() < deadline:
        time.sleep(0.01)
    return job


def test_a_job_is_readable_immediately_and_carries_its_result(service):
    job = service.start("analyze", lambda db, progress: {"proposals": [1, 2, 3]})

    assert service.get(job.id) is job          # visible before it finishes
    _wait(job)
    assert job.status == STATUS_DONE
    assert job.result == {"proposals": [1, 2, 3]}
    assert job.finished_at is not None
    assert service.closed, "la session du thread doit être fermée"


def test_progress_is_visible_while_the_job_runs(service):
    started, release = threading.Event(), threading.Event()

    def work(db, progress):
        progress("interrogation du datamart", 2, 10)
        started.set()
        release.wait(2)
        return {}

    job = service.start("analyze", work)
    started.wait(2)

    snapshot = job.public()
    assert snapshot["status"] == STATUS_RUNNING
    assert snapshot["phase"] == "interrogation du datamart"
    assert (snapshot["done"], snapshot["total"]) == (2, 10)
    assert snapshot["result"] is None           # kept small while running
    release.set()
    _wait(job)


def test_a_crash_becomes_a_readable_error_not_a_lost_job(service):
    def work(db, progress):
        raise ValueError("datamart injoignable")

    job = _wait(service.start("analyze", work))

    assert job.status == STATUS_ERROR
    assert "datamart injoignable" in job.error
    assert job.result is None
    assert service.closed, "la session doit être fermée même en cas d'échec"


def test_a_run_is_recorded_before_it_starts_and_updated_when_it_ends(service):
    """An operator has no reason to watch a batch: the run must be findable
    even if the browser never comes back."""
    job = service.start(
        "analyze", lambda db, progress: {"proposals": [{"status": "PROPOSED"}]},
        user_id=7, label="SP_LINK_REPORT.xlsx + 8 fichiers",
    )

    assert service.runs.opened[0]["run_id"] == job.id
    assert service.runs.opened[0]["status"] == STATUS_RUNNING
    assert service.runs.opened[0]["user_id"] == 7
    assert "SP_LINK_REPORT" in service.runs.opened[0]["label"]

    _wait(job)
    closed = service.runs.closed[0]
    assert closed["status"] == STATUS_DONE
    assert closed["result"] == {"proposals": [{"status": "PROPOSED"}]}
    assert closed["finished_at"] is not None
    assert service.runs.pruned == 1


def test_a_failed_run_persists_its_error_and_the_phase_it_died_on(service):
    def work(db, progress):
        progress("interrogation du datamart", 0, 10)
        raise RuntimeError("boom")

    _wait(service.start("analyze", work))

    closed = service.runs.closed[0]
    assert closed["status"] == STATUS_ERROR
    assert "boom" in closed["error"]
    assert closed["phase"] == "échec"
    assert closed["result"] is None


def test_an_unknown_job_is_simply_absent(service):
    assert service.get("nope") is None


def test_finished_jobs_are_capped_but_a_running_one_is_never_dropped(service, monkeypatch):
    monkeypatch.setattr(module, "MAX_JOBS", 3)
    release = threading.Event()
    running = service.start("commit", lambda db, progress: release.wait(3))
    for _ in range(6):
        _wait(service.start("analyze", lambda db, progress: {}))

    ids = {j.id for j in service.list_jobs()}
    assert running.id in ids
    assert len(ids) <= 4          # the cap plus the untouchable running one
    release.set()
    _wait(running)


# ── the datamart lookup: a temp-table join, not an IN list ──────────

class _FakeCursor:
    def __init__(self, rows=None, fail_on=None):
        self.statements = []
        self.batches = []
        self._rows = rows or []
        self._fail_on = fail_on

    def execute(self, sql, *args):
        self.statements.append(" ".join(sql.split()))
        if self._fail_on and self._fail_on in sql:
            raise RuntimeError("CREATE TABLE permission denied in database 'tempdb'")

    def executemany(self, sql, params):
        self.statements.append(" ".join(sql.split()))
        self.batches.append(len(params))

    def fetchall(self):
        return self._rows

    def close(self):
        pass


class _FakeRemote:
    def __init__(self, cursor):
        self.connection = type("C", (), {"cursor": lambda _self: cursor})()
        self.cursor = cursor


def test_the_payment_lookup_loads_a_temp_table_and_joins_once():
    """14 534 PaymentNumbers as fifteen 1 000-term IN predicates is what made the
    run sit on 'interrogation du datamart' for minutes. One temp table, one
    join — the same shape the ingestion DAG has always used."""
    from app.services.rcp_link_service import PO_INSERT_BATCH, RcpLinkService

    rows = [("000008957379", "PACS-A", "MSG-A")]
    cursor = _FakeCursor(rows=rows)
    values = [f"{i:012d}" for i in range(PO_INSERT_BATCH + 5)]

    out = RcpLinkService()._payments_via_temp_table(_FakeRemote(cursor), values, None)

    assert out == rows
    sql = " || ".join(cursor.statements)
    assert "CREATE TABLE #reco_rcp_po" in sql
    assert "INNER JOIN #reco_rcp_po t ON p.PaymentNumber = t.po" in sql
    assert sql.count("IN (") == 0, "aucune liste IN ne doit subsister"
    # Chunked load, then the index — built AFTER, and never unique: the datamart
    # collation is case-insensitive and would abort on a PRIMARY KEY.
    assert cursor.batches == [PO_INSERT_BATCH, 5]
    assert "CREATE CLUSTERED INDEX" in sql
    assert "PRIMARY KEY" not in sql and "UNIQUE" not in sql
    # Dropped on both ends: the connection goes back to a pool and #temp is
    # session-scoped, so a leftover would break the next run.
    assert cursor.statements[0].startswith("IF OBJECT_ID('tempdb..#reco_rcp_po')")
    assert cursor.statements[-1].startswith("IF OBJECT_ID('tempdb..#reco_rcp_po')")


def test_a_refused_temp_table_falls_back_to_batched_in_lists(monkeypatch):
    """If the datamart account may not create a temp table, the run degrades to
    the slow path instead of failing outright."""
    from app.services import rcp_link_service as svc_module
    from app.services.rcp_link_service import RcpLinkService

    service = RcpLinkService()
    cursor = _FakeCursor(fail_on="CREATE TABLE")
    remote = _FakeRemote(cursor)
    used = {}

    class _Engine:
        def connect(self):
            class _Ctx:
                def __enter__(_s):
                    return remote

                def __exit__(_s, *a):
                    return False
            return _Ctx()

    monkeypatch.setattr(
        svc_module.source_connection_service, "get_connection",
        lambda db, cid: object(),
    )
    monkeypatch.setattr(
        svc_module.source_connection_service, "get_engine", lambda conn: _Engine()
    )
    def fake_in_list(remote, values, progress):
        used["values"] = list(values)
        return [("PO1", "PACS-A", "MSG-A")]

    monkeypatch.setattr(service, "_payments_via_in_list", fake_in_list)

    payments, error = service.resolve_payments(None, 1, ["PO1", "PO1", " "])

    assert error == ""
    assert payments == {"PO1": [("PACS-A", "MSG-A")]}
    assert used["values"] == ["PO1"]      # dédupliqué et nettoyé avant l'appel


# ── the persisted history ───────────────────────────────────────────

def test_run_counters_are_derived_per_kind():
    """The history list must be readable without opening a 2.6 MB payload."""
    from app.repositories.rcp_run_repository import summarize

    analyze = summarize("analyze", {"proposals": [
        {"status": "PROPOSED"}, {"status": "TO_RECOMMIT"},
        {"status": "EMARGED"}, {"status": "ENTRY_NOT_FOUND"},
    ]})
    assert (analyze["movements"], analyze["actionable"]) == (4, 2)

    commit = summarize("commit", {"results": [1, 2, 3], "applied": 2, "failed": 1})
    assert (commit["movements"], commit["applied"], commit["failed"]) == (3, 2, 1)

    # A run that died before producing anything still lists cleanly.
    assert summarize("analyze", None)["movements"] == 0


def test_a_listed_run_never_carries_its_report():
    """Loading the history must not mean loading every payload."""
    from types import SimpleNamespace

    from app.repositories.rcp_run_repository import rcp_run_repository

    row = SimpleNamespace(
        id="r1", kind="analyze", status="done", phase="terminé", error="",
        label="SP_LINK_REPORT.xlsx", movements=1627, actionable=504, applied=0,
        failed=0, started_at=None, finished_at=None, result={"proposals": [1, 2]},
    )

    listed = rcp_run_repository.to_public(row)
    opened = rcp_run_repository.to_public(row, with_result=True)

    assert listed["result"] is None
    assert listed["movements"] == 1627
    assert opened["result"] == {"proposals": [1, 2]}
    # Logs are live commentary — never persisted, so never replayed.
    assert listed["logs"] == [] and opened["logs"] == []
