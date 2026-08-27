"""Every hand-built statement must COMPILE against the Postgres dialect.

A statement is only built when it runs, so a malformed one hides until
production: `.returning(text("(xmax = 0)"))` shipped a batch-booking run that
ingested 660k rows and then died on the split push, because a TextClause has no
label and the PG dialect cannot put it in a RETURNING list. Compiling is enough
to catch that whole class of error, and it needs no database.

Service-level tests stub the repository, so they cannot cover this — that is
exactly why it got through.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

from app.repositories.movement_lot_repository import movement_lot_repository
from app.repositories.movement_split_repository import movement_split_repository
from app.repositories.reconciliation_entry_repository import reconciliation_entry_repository

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


class _CompilingSession:
    """Compiles what it is handed instead of executing it, then returns nothing.

    Compilation is where a bad column expression blows up, so reaching the empty
    result means the statement is well-formed.
    """

    def __init__(self):
        self.compiled = []

    def execute(self, statement, params=None):
        self.compiled.append(str(statement.compile(dialect=postgresql.dialect())))
        return _EmptyResult()

    def query(self, *args, **kwargs):
        # Some repositories pre-filter with the ORM before building the raw
        # statement; an empty ORM result just lets them reach it.
        return _EmptyQuery()

    def commit(self):
        pass

    def rollback(self):
        pass


class _EmptyQuery:
    def filter(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return []

    def first(self):
        return None


class _EmptyResult:
    rowcount = 0

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def scalar(self):
        return None


@pytest.fixture()
def db():
    return _CompilingSession()


def _member_row(**overrides):
    row = {
        "lot_id": "11111111-1111-4111-8111-111111111111",
        "source_hash": "a" * 64,
        "movement_type": "SCTXB",
        "external_ref": "S1",
        "account": "0010130015001",
        "currency": "EUR",
        "amount": Decimal("-100.00"),
        "direction": "debit",
        "value_date": NOW,
        "operation_date": NOW,
        "transaction_particulars": "SCTXB/O/x",
        "ref_no": None,
        "remarks_1": "PACS1",
        "split_parent_hash": None,
        "payment_count": None,
    }
    row.update(overrides)
    return row


def _parent_row(**overrides):
    row = {
        "source_hash": "b" * 64,
        "flow_id": 1,
        "flow_source_id": 2,
        "movement_type": "SCTXB",
        "external_ref": "S1",
        "account": "0010130015001",
        "currency": "EUR",
        "amount": Decimal("-1000.00"),
        "direction": "debit",
        "value_date": NOW,
        "operation_date": NOW,
        "transaction_particulars": "SCTXB/O/x",
        "ref_no": None,
        "remarks_1": "PACS1",
        "payload_raw": {"MovementID": 1},
        "child_count": 2,
        "payment_count": 3,
        "child_amount": Decimal("-1000.00"),
        "payment_amount": Decimal("-990.00"),
        "shared_key_movements": 1,
        "claim_key_type": "PACS008",
        "claim_key_value": "PACS1",
    }
    row.update(overrides)
    return row


def _ghost_row(**overrides):
    row = {
        "flow_id": 1,
        "ingestion_run_id": 9,
        "reco_id": "11111111-1111-4111-8111-111111111111",
        "account": "0010130015001",
        "currency": "EUR",
        "amount": Decimal("-700.00"),
        "direction": "debit",
        "value_date": NOW,
        "operation_date": NOW,
        "event_type": "TR",
        "external_ref": "S1~aaaaaaaaaa",
        "transaction_particulars": "SCTXB/O/x",
        "ref_no": None,
        "remarks_1": "PACS1",
        "transaction_id": "TX-1",
        "payload_raw": {"split_of": "S1"},
        "source_hash": "c" * 64,
        "split_parent_hash": "b" * 64,
        "status": "PENDING",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# splits
# ---------------------------------------------------------------------------

def test_upsert_parents_compiles(db):
    """The exact statement that failed in production."""
    movement_split_repository.upsert_parents(db, [_parent_row()])
    sql = db.compiled[0]
    assert "INSERT INTO reco.movement_split" in sql
    assert "ON CONFLICT (source_hash) DO UPDATE" in sql
    assert "RETURNING (xmax = 0)" in sql


def test_writes_are_chunked_so_one_statement_stays_bounded():
    """psycopg2 interpolates client-side, so nothing but the row count bounds a
    statement — and a parent row carries a whole datamart movement in
    payload_raw. An unbounded INSERT is how the split push timed out."""
    from app.repositories.movement_split_repository import _INSERT_CHUNK

    db = _CompilingSession()
    n = _INSERT_CHUNK * 2 + 5
    movement_split_repository.upsert_parents(
        db, [_parent_row(source_hash=f"{i:064d}") for i in range(n)]
    )
    assert len(db.compiled) == 3

    db = _CompilingSession()
    movement_split_repository.upsert_ghost_entries(
        db, [_ghost_row(source_hash=f"{i:064d}") for i in range(n)]
    )
    inserts = [s for s in db.compiled if "INSERT INTO reco.reconciliation_entry" in s]
    assert len(inserts) == 3


def test_upsert_ghost_entries_compiles(db):
    movement_split_repository.upsert_ghost_entries(db, [_ghost_row()])
    insert = next(s for s in db.compiled if "INSERT INTO reco.reconciliation_entry" in s)
    assert "ON CONFLICT (source_hash) DO UPDATE" in insert
    assert "RETURNING (xmax = 0)" in insert
    # A ghost's amount IS refreshed — it grows as std.Payment fills in.
    assert "amount" in insert.split("DO UPDATE")[1]


def test_withdraw_and_reap_compile(db):
    movement_split_repository.withdraw_parent_movements(db, parent_hashes=["b" * 64])
    movement_split_repository.reap_stale_group_children(
        db, flow_source_id=2, claims=[("MSGID", "PTEL-X")], expected_hashes=["c" * 64]
    )
    movement_split_repository.flag_emarged(db, parent_hashes=["b" * 64])
    assert len(db.compiled) >= 3


def test_claim_group_statements_compile(db):
    movement_split_repository.resolve_group_canonicals(
        db, flow_source_id=2, claims=[("MSGID", "PTEL-X"), ("PACS008", "PACS1")]
    )
    movement_split_repository.group_parent_totals(
        db, flow_source_id=2, claims=[("MSGID", "PTEL-X")]
    )
    movement_split_repository.list_group_parents(
        db, flow_source_id=2, claim_key_type="MSGID", claim_key_value="PTEL-X"
    )
    assert len(db.compiled) == 3
    assert all("movement_split" in s for s in db.compiled)


def test_parents_still_replaced_compiles(db):
    """The re-ingestion guard: it runs on every finacle batch, so a malformed
    statement would stop the ingestion itself."""
    movement_split_repository.parents_still_replaced(db, source_hashes=["b" * 64])
    assert len(db.compiled) == 1
    statement = db.compiled[0]
    assert "movement_split" in statement
    # The group has ghosts if EITHER table carries one — an émargé ghost must
    # not let its parent come back.
    assert "reconciliation_entry_emargement" in statement
    assert "claim_key_value" in statement


def test_refresh_parent_mismatch_compiles(db):
    """The second reconciliation: the CTE chain that measures each claim group
    and the two UPDATEs that tag/clear the lots."""
    movement_split_repository.refresh_parent_mismatch(db)
    assert len(db.compiled) == 2
    assert "parent_mismatch" in db.compiled[0]
    assert "reconciliation_entry_emargement" in db.compiled[0]
    assert "parent_mismatch = false" in db.compiled[1]


def test_split_reads_compile(db):
    movement_split_repository.get_parent(db, source_hash="b" * 64)
    movement_split_repository.list_children(db, parent_hash="b" * 64)
    movement_split_repository.parents_for_hashes(db, source_hashes=["b" * 64])
    assert len(db.compiled) == 3


# ---------------------------------------------------------------------------
# buckets
# ---------------------------------------------------------------------------

def test_create_lots_compiles(db):
    movement_lot_repository.create_lots(
        db,
        flow_id=1,
        flow_source_id=2,
        buckets=[
            {
                "lot_id": "11111111-1111-4111-8111-111111111111",
                "bucket_kind": "PAIR",
                "bucket_pacs008": "PACS1",
                "bucket_msgid": "MSGA",
                "bucket_po": "",
                "bucket_ref": "",
            }
        ],
    )
    sql = db.compiled[0]
    assert "INSERT INTO reco.movement_lot" in sql
    assert "bucket_kind" in sql


def test_upsert_members_compiles_with_the_ghost_amount_case(db):
    """The CASE that keeps a real movement's amount frozen while letting a
    ghost's move — a construct SQLAlchemy will happily build and only reject at
    compile time if it is malformed."""
    movement_lot_repository.upsert_members(
        db, [_member_row(), _member_row(source_hash="d" * 64, split_parent_hash="b" * 64)]
    )
    sql = db.compiled[0]
    assert "ON CONFLICT (source_hash) DO UPDATE" in sql
    assert "CASE WHEN" in sql
    assert "RETURNING" in sql


def test_key_and_rollup_statements_compile(db):
    movement_lot_repository.insert_keys(
        db, [{"member_id": 1, "key_type": "PACS008", "key_value": "PACS1"}]
    )
    movement_lot_repository.sync_lot_currencies(db, lot_ids=["lot-a"])
    movement_lot_repository.sync_synthetic_only(db, lot_ids=["lot-a"])
    assert len(db.compiled) == 3


def test_lot_reads_compile(db):
    movement_lot_repository.list_lots(
        db, flow_id=1, bucket_kind="PAIR", synthetic_only=True, parent_mismatch=True
    )
    movement_lot_repository.get_lot_summary(db, lot_id="lot-a")
    movement_lot_repository.list_members_page(db, lot_id="lot-a", key_type="PACS008", key_value="P1")
    movement_lot_repository.list_members_with_status(db, lot_id="lot-a")
    assert all("SELECT" in s for s in db.compiled)
    assert any("parent_mismatch" in s for s in db.compiled)


# ---------------------------------------------------------------------------
# entries
# ---------------------------------------------------------------------------

def test_upsert_finacle_compiles_with_the_ghost_amount_case(db):
    """Same CASE on the entry side. upsert_finacle commits internally, which the
    fake session absorbs — only the compile matters here."""
    reconciliation_entry_repository.upsert_finacle(
        db,
        [
            {
                "flow_id": 1,
                "ingestion_run_id": 9,
                "reco_id": "lot-a",
                "account": "0010130015001",
                "currency": "EUR",
                "amount": Decimal("-100.00"),
                "direction": "debit",
                "value_date": NOW,
                "operation_date": NOW,
                "event_type": "TR",
                "external_ref": "S1",
                "transaction_particulars": "SCTXB/O/x",
                "ref_no": None,
                "remarks_1": "PACS1",
                "transaction_id": "TX-1",
                "payload_raw": {},
                "source_hash": "e" * 64,
                "split_parent_hash": None,
                "status": "pending",
            }
        ],
    )
    insert = next(s for s in db.compiled if "INSERT INTO reco.reconciliation_entry" in s)
    assert "CASE WHEN" in insert
    assert "RETURNING (xmax = 0)" in insert


def test_rcp_bucket_filter_compiles_as_a_row_value_in():
    """The RCP reattribution looks a target lot up by its whole bucket identity.

    A row-value IN is dialect-specific — SQLite renders it, Postgres renders it,
    a typo in the column tuple does not surface until it runs. Compiling proves
    the five columns line up with the five-component key.
    """
    from app.models.movement_lot import MovementLot
    from app.services.rcp_link_parser import BUCKET_PAIR, BUCKET_PO, BucketKey
    from app.services.rcp_link_service import RcpLinkService

    statement = select(MovementLot).where(
        RcpLinkService.bucket_filter(
            7,
            [
                BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSG1"),
                BucketKey(BUCKET_PO, po="000008957379"),
            ],
        )
    )
    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "movement_lot.flow_source_id = 7" in compiled
    assert "(reco.movement_lot.bucket_kind, reco.movement_lot.bucket_pacs008" in compiled
    assert "('PAIR', 'PACS1', 'MSG1', '', '')" in compiled
    assert "('PO', '', '', '000008957379', '')" in compiled


def test_rcp_orphan_reads_compile():
    """The two reads of the 'non rattachés' section.

    The orphan sweep filters on ``reco_id IS NULL OR reco_id = 'Not Supported'``
    over an enum-typed status, and the movement lookup ORs one ILIKE per named
    TransactionID across a join — both are built at call time, so a bad column
    or a mistyped join surfaces only when an operator clicks Analyser.
    """
    from sqlalchemy import or_

    from app.models.flow import FlowSource
    from app.models.ingestion_run import IngestionRun
    from app.models.movement_lot import MovementLot, MovementLotMember
    from app.models.reconciliation_entry import EntryStatus, ReconciliationEntry
    from app.services.rcp_orphan_service import UNRESOLVED_RECO_ID

    orphans = (
        select(ReconciliationEntry, FlowSource)
        .join(IngestionRun, ReconciliationEntry.ingestion_run_id == IngestionRun.id)
        .join(FlowSource, IngestionRun.flow_source_id == FlowSource.id)
        .where(ReconciliationEntry.flow_id == 16)
        .where(ReconciliationEntry.status == EntryStatus.PENDING)
        .where(
            or_(
                ReconciliationEntry.reco_id.is_(None),
                ReconciliationEntry.reco_id == UNRESOLVED_RECO_ID,
            )
        )
        .limit(5000)
    )
    compiled = str(
        orphans.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "reco_id IS NULL" in compiled
    assert "'Not Supported'" in compiled

    named = (
        select(MovementLotMember.lot_id, MovementLotMember.external_ref)
        .join(MovementLot, MovementLot.id == MovementLotMember.lot_id)
        .where(MovementLot.flow_id == 16)
        .where(
            or_(
                MovementLotMember.external_ref.ilike("PF0008529%"),
                MovementLotMember.external_ref.ilike("PF0008528%"),
            )
        )
    )
    compiled = str(
        named.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )
    assert "ILIKE" in compiled.upper() and "PF0008529%" in compiled


def test_entry_ids_filter_compiles_on_both_tables():
    """The `ids` filter a match basket uses to re-read its rows.

    Compiled against both entry tables because the basket queries with no
    status, which routes to the live AND the émargement table — a clause that
    named a column only one of them has would blow up there and nowhere else.
    An empty list must mean "nothing", not "no filter", so the None check has to
    stay a `is not None`.
    """
    from sqlalchemy.orm import Query

    from app.models.reconciliation_entry import (
        EntryStatus,
        ReconciliationEntry,
        ReconciliationEntryEmargement,
    )

    for model in (ReconciliationEntry, ReconciliationEntryEmargement):
        q = reconciliation_entry_repository._apply_entry_filters(
            Query(model), model, ids=[1, 2, 3]
        )
        compiled = str(
            q.statement.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert ".id IN (1, 2, 3)" in compiled

        # No ids → no clause at all (the filter must not degrade the other paths).
        q_none = reconciliation_entry_repository._apply_entry_filters(
            Query(model), model, ids=None, status=EntryStatus.PENDING
        )
        assert ".id IN" not in str(
            q_none.statement.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )

        # Empty list → an IN () that matches nothing, never a full scan.
        q_empty = reconciliation_entry_repository._apply_entry_filters(
            Query(model), model, ids=[]
        )
        assert "WHERE" in str(q_empty.statement.compile(dialect=postgresql.dialect()))
