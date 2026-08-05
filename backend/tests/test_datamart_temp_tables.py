"""Lookup temp tables must never carry a UNIQUE constraint.

Python de-duplicates its lookup inputs case-SENSITIVELY (a set of str) while SQL
Server's default collation (SQL_Latin1_General_CP1_CI_AS) compares
case-INSENSITIVELY. Two values distinct in Python are the same key to SQL Server,
so a PRIMARY KEY turns an ordinary data quirk into an aborted run — which is what
happened in prod on 2026-08-04:

    Violation of PRIMARY KEY constraint 'PK__#reco_bb__…'.
    Cannot insert duplicate key … (CNS-MAL-202607-o4ATT8ZS-0GoL1Y)

Uniqueness buys nothing here: the index exists to make the join a seek, and a
duplicated temp row only repeats join output, which every caller collapses into a
dict. These tests drive the loaders with a fake cursor — no MSSQL, no pyodbc.
"""
import sys
from pathlib import Path

import pytest

DAGS_DIR = Path(__file__).resolve().parents[2] / "shared" / "dags"
if not DAGS_DIR.is_dir():
    pytest.skip(
        "shared/dags not mounted in this environment (backend-only container)",
        allow_module_level=True,
    )
sys.path.insert(0, str(DAGS_DIR))

import reco_datamart  # noqa: E402
import reco_datamart_bb  # noqa: E402


class _FakeCursor:
    """Records SQL and enforces the constraint SQL Server would enforce."""

    def __init__(self, *, collation_insensitive=True):
        self.statements = []
        self.rows_by_table = {}
        self.fast_executemany = False
        self._insensitive = collation_insensitive
        self._unique = set()  # tables declared with a UNIQUE/PRIMARY KEY

    def _fold(self, value):
        return value.lower() if self._insensitive else value

    def execute(self, sql, *args):
        self.statements.append(sql)
        low = sql.lower()
        if "create table" in low and ("primary key" in low or "unique" in low):
            # Mirror the production failure rather than passing silently.
            self._unique.add(sql)
        if "create table" in low:
            self.rows_by_table.setdefault(_table_of(sql), [])

    def executemany(self, sql, params):
        self.statements.append(sql)
        table = _table_of(sql)
        bucket = self.rows_by_table.setdefault(table, [])
        for (value,) in params:
            if self._unique and self._fold(value) in {self._fold(v) for v in bucket}:
                raise RuntimeError(
                    f"Violation of PRIMARY KEY constraint. Duplicate key ({value})"
                )
            bucket.append(value)

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _table_of(sql: str) -> str:
    for token in sql.replace("(", " ").split():
        if token.startswith("#"):
            return token
    return "?"


# Same shape as the value that aborted the 2026-08-04 backfill.
CASE_VARIANTS = ["CNS-MAL-202607-o4ATT8ZS-0GoL1Y", "CNS-MAL-202607-O4ATT8ZS-0GOL1Y"]


def test_bb_temp_join_survives_case_only_duplicates():
    cursor = _FakeCursor()
    reco_datamart_bb._temp_join(
        _FakeConn(cursor),
        CASE_VARIANTS,
        temp_name="#reco_bb_msg",
        join_sql="SELECT 1",
    )
    ddl = next(s for s in cursor.statements if "CREATE TABLE" in s)
    assert "PRIMARY KEY" not in ddl and "UNIQUE" not in ddl
    # The index is still there — dropping it would turn the join into a scan.
    assert any("CREATE CLUSTERED INDEX" in s for s in cursor.statements)
    # Both variants reach the table: the case distinction is meaningful to the
    # Python-side lookup that follows.
    assert sorted(cursor.rows_by_table["#reco_bb_msg"]) == sorted(CASE_VARIANTS)


def test_bb_temp_join_builds_the_index_after_the_load():
    """Indexing an empty table then inserting is slower, and on a 660k-row
    backfill that difference is not academic."""
    cursor = _FakeCursor()
    reco_datamart_bb._temp_join(
        _FakeConn(cursor), CASE_VARIANTS, temp_name="#reco_bb_msg", join_sql="SELECT 1"
    )
    order = [
        i for i, s in enumerate(cursor.statements)
        if "INSERT INTO" in s or "CREATE CLUSTERED INDEX" in s
    ]
    last_insert = max(i for i in order if "INSERT INTO" in cursor.statements[i])
    index_at = next(i for i in order if "CREATE CLUSTERED INDEX" in cursor.statements[i])
    assert index_at > last_insert


def test_the_fake_cursor_would_actually_catch_a_primary_key():
    """Guards the guard: with a UNIQUE table the fake must reproduce the prod
    error, otherwise the tests above prove nothing."""
    cursor = _FakeCursor()
    cursor.execute("CREATE TABLE #t (k VARCHAR(128) PRIMARY KEY)")
    with pytest.raises(RuntimeError, match="Duplicate key"):
        cursor.executemany("INSERT INTO #t (k) VALUES (?)", [(v,) for v in CASE_VARIANTS])


@pytest.mark.parametrize(
    "resolver, kwargs",
    [
        (reco_datamart.resolve_bulk_returns, {}),
        (reco_datamart.resolve_return_recos, {}),
        (reco_datamart.resolve_reversals, {}),
    ],
)
def test_legacy_resolvers_survive_case_only_duplicates(resolver, kwargs):
    """The same loaders on the non-BB path — #reco_bmsg in particular holds the
    very MessageID population that collided."""
    cursor = _FakeCursor()
    resolver(_FakeConn(cursor), CASE_VARIANTS, **kwargs)
    for sql in cursor.statements:
        if "CREATE TABLE" in sql:
            assert "PRIMARY KEY" not in sql and "UNIQUE" not in sql, sql


def test_bulk_groups_loader_survives_case_only_duplicates():
    cursor = _FakeCursor()
    reco_datamart.resolve_bulk_groups(_FakeConn(cursor), CASE_VARIANTS, CASE_VARIANTS)
    creates = [s for s in cursor.statements if "CREATE TABLE" in s]
    assert creates and all(
        "PRIMARY KEY" not in s and "UNIQUE" not in s for s in creates
    )
    assert sum(1 for s in cursor.statements if "CREATE CLUSTERED INDEX" in s) == len(creates)
