"""Data access for split parents — real movements replaced by ghost entries.

Write methods NEVER commit — ``split_service.apply_split_batch`` owns the
transaction, so registering a parent, withdrawing the real movement and reaping
its stale ghosts either all happen or none do. A half-applied batch is exactly
the state that double counts (parent AND ghosts live at once), which is why it
is not allowed to exist between commits.

Raw-SQL notes: entry statuses are compared against the enum NAMES
('PENDING', 'MATCHED', ...) — that is what ORM-written rows store.
"""
import logging
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from sqlalchemy import literal_column, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.movement_split import MovementSplit
from app.models.reconciliation_entry import EntryStatus, ReconciliationEntry

logger = logging.getLogger(__name__)

# Rows per INSERT statement. psycopg2 interpolates parameters client-side, so a
# statement's size is bounded by nothing but the row count — and a parent row
# carries the whole datamart movement in payload_raw. Same reasoning as
# KEY_INSERT_CHUNK in movement_lot_repository.
_INSERT_CHUNK = 1000

# Columns refreshed when a parent is pushed again: everything the DAG recomputes
# from the datamart. The identity (source_hash) and created_at are not in it.
_PARENT_REFRESH = (
    "flow_id", "flow_source_id", "movement_type", "external_ref", "account",
    "currency", "amount", "direction", "value_date", "operation_date",
    "transaction_particulars", "ref_no", "remarks_1", "payload_raw",
    "child_count", "payment_count", "child_amount", "payment_amount",
    "shared_key_movements",
)


def _chunks(rows: List[dict], size: int = _INSERT_CHUNK) -> Iterator[List[dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


class MovementSplitRepository:
    # ------------------------------------------------------------------
    # DAG-facing writes (no commit — the service owns the transaction)
    # ------------------------------------------------------------------

    def upsert_parents(self, db: Session, rows: Sequence[dict]) -> Tuple[int, int]:
        """Insert/refresh split parents keyed on source_hash → (inserted, updated).

        ``parent_emarged`` is deliberately NOT refreshed here: it records a
        conflict resolved (or not) out of band, and a re-push must not clear it.
        """
        if not rows:
            return 0, 0
        inserted = updated = 0
        for chunk in _chunks(list(rows)):
            stmt = pg_insert(MovementSplit.__table__).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["source_hash"],
                set_={
                    **{col: getattr(stmt.excluded, col) for col in _PARENT_REFRESH},
                    "updated_at": text("now()"),
                },
            # literal_column, NOT text: on SQLAlchemy 1.4 a TextClause has no
            # label and the PG dialect cannot compile it into a RETURNING list.
            ).returning(literal_column("(xmax = 0)"))
            for (is_insert,) in db.execute(stmt).fetchall():
                if is_insert:
                    inserted += 1
                else:
                    updated += 1
        return inserted, updated

    def upsert_ghost_entries(self, db: Session, rows: Sequence[dict]) -> Tuple[int, int, int]:
        """Materialise the ghosts as live PENDING entries → (inserted, updated, skipped).

        Ghosts are written here rather than through the generic finacle entry
        push so that creating them, registering their parent and withdrawing the
        real movement all sit in ONE transaction — the in-between state, where
        both the movement and its ghosts count, is the double count this design
        exists to prevent.

        Unlike a real movement's, a ghost's ``amount`` IS refreshed: it is the
        sum of its bucket's payments and grows as std.Payment fills in. Ghosts
        that already reached the émargement table are immutable → skipped.
        """
        if not rows:
            return 0, 0, 0
        hashes = [r["source_hash"] for r in rows]
        emarged = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT source_hash FROM reco.reconciliation_entry_emargement "
                    "WHERE source_hash = ANY(:hashes)"
                ),
                {"hashes": hashes},
            ).fetchall()
        }
        live = [r for r in rows if r["source_hash"] not in emarged]
        skipped = len(rows) - len(live)
        if not live:
            return 0, 0, skipped

        inserted = updated = 0
        for chunk in _chunks(live):
            stmt = pg_insert(ReconciliationEntry.__table__).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["source_hash"],
                set_={
                    "reco_id": stmt.excluded.reco_id,
                    "amount": stmt.excluded.amount,
                    "direction": stmt.excluded.direction,
                    "split_parent_hash": stmt.excluded.split_parent_hash,
                    "payload_raw": stmt.excluded.payload_raw,
                    "ingestion_run_id": stmt.excluded.ingestion_run_id,
                },
                where=ReconciliationEntry.__table__.c.status == EntryStatus.PENDING,
            ).returning(literal_column("(xmax = 0)"))
            flags = [bool(r[0]) for r in db.execute(stmt).fetchall()]
            inserted += sum(1 for f in flags if f)
            updated += len(flags) - sum(1 for f in flags if f)
        return inserted, updated, skipped

    def withdraw_parent_movements(
        self, db: Session, *, parent_hashes: Sequence[str]
    ) -> Tuple[int, Set[str]]:
        """Remove the real movements from the reconciliation, their ghosts stand in.

        Returns (entries_withdrawn, hashes_already_emarged). A parent that
        already reached the émargement table is NOT withdrawn — émargé history is
        never rewritten — and its hash comes back so the caller can flag it. Its
        ghosts then double count against it until an operator arbitrates, which
        is the whole point of surfacing the flag.

        The parent's ``movement_lot_member`` row is dropped too: it was written
        by an earlier run, when the movement still fitted in a single bucket, and
        would otherwise leave the lot aggregates counting the full amount on top
        of the ghosts.
        """
        if not parent_hashes:
            return 0, set()
        hashes = list(dict.fromkeys(parent_hashes))

        emarged = {
            row[0]
            for row in db.execute(
                text(
                    "SELECT source_hash FROM reco.reconciliation_entry_emargement "
                    "WHERE source_hash = ANY(:hashes)"
                ),
                {"hashes": hashes},
            ).fetchall()
        }
        removable = [h for h in hashes if h not in emarged]
        if not removable:
            return 0, emarged

        withdrawn = db.execute(
            text(
                "DELETE FROM reco.reconciliation_entry "
                "WHERE source_hash = ANY(:hashes) AND status = 'PENDING'"
            ),
            {"hashes": removable},
        ).rowcount or 0
        db.execute(
            text("DELETE FROM reco.movement_lot_member WHERE source_hash = ANY(:hashes)"),
            {"hashes": removable},
        )
        return withdrawn, emarged

    def reap_stale_children(
        self, db: Session, *, expected: Sequence[Tuple[str, str]]
    ) -> int:
        """Delete PENDING ghosts their parent no longer produces.

        ``expected`` is the full (parent_hash, child_hash) set the batch just
        pushed. A ghost outside it belongs to a bucket that disappeared — a
        payment moved to another MessageID, or std.Payment corrected itself —
        and would otherwise sit PENDING forever carrying a stale amount.

        Scoped to the parents in the batch: a parent absent from this push is
        untouched, so a partial run never reaps ghosts it simply did not mention.
        Matched ghosts are left alone (they live in the émargement table).
        """
        if not expected:
            return 0
        parents = [p for p, _c in expected]
        children = [c for _p, c in expected]
        stale = db.execute(
            text(
                """
                WITH kept AS (
                    SELECT DISTINCT p, c
                    FROM unnest(CAST(:parents AS text[]), CAST(:children AS text[])) AS t(p, c)
                )
                DELETE FROM reco.reconciliation_entry e
                WHERE e.split_parent_hash = ANY(:parent_list)
                  AND e.status = 'PENDING'
                  AND NOT EXISTS (
                      SELECT 1 FROM kept k
                      WHERE k.p = e.split_parent_hash AND k.c = e.source_hash
                  )
                RETURNING e.source_hash
                """
            ),
            {
                "parents": parents,
                "children": children,
                "parent_list": list(dict.fromkeys(parents)),
            },
        ).fetchall()
        if not stale:
            return 0
        stale_hashes = [row[0] for row in stale]
        db.execute(
            text("DELETE FROM reco.movement_lot_member WHERE source_hash = ANY(:hashes)"),
            {"hashes": stale_hashes},
        )
        logger.info("[movement_split] reaped %d stale ghost entrie(s)", len(stale_hashes))
        return len(stale_hashes)

    def flag_emarged(self, db: Session, *, parent_hashes: Sequence[str]) -> None:
        if not parent_hashes:
            return
        db.execute(
            text(
                "UPDATE reco.movement_split SET parent_emarged = true, updated_at = now() "
                "WHERE source_hash = ANY(:hashes) AND parent_emarged = false"
            ),
            {"hashes": list(parent_hashes)},
        )

    # ------------------------------------------------------------------
    # UI-facing reads
    # ------------------------------------------------------------------

    def get_parent(self, db: Session, *, source_hash: str) -> Optional[Any]:
        return db.execute(
            text(
                """
                SELECT s.source_hash, s.flow_id, s.flow_source_id, s.movement_type,
                       s.external_ref, s.account, s.currency, s.amount, s.direction,
                       s.value_date, s.operation_date, s.transaction_particulars,
                       s.ref_no, s.remarks_1, s.child_count, s.payment_count,
                       s.child_amount, s.payment_amount, s.shared_key_movements,
                       s.parent_emarged,
                       s.created_at, s.updated_at
                FROM reco.movement_split s
                WHERE s.source_hash = :h
                """
            ),
            {"h": source_hash},
        ).fetchone()

    def list_children(self, db: Session, *, parent_hash: str) -> List[Any]:
        """The ghosts of one parent, live and émargé alike, with their bucket.

        The filter is repeated inside each UNION branch on purpose: both tables
        are large and only an index lookup on split_parent_hash is acceptable.
        """
        return db.execute(
            text(
                """
                WITH ghosts AS (
                    SELECT id, source_hash, reco_id, amount, currency, direction,
                           value_date, external_ref, status::text AS status, match_group_id
                    FROM reco.reconciliation_entry
                    WHERE split_parent_hash = :h
                    UNION ALL
                    SELECT id, source_hash, reco_id, amount, currency, direction,
                           value_date, external_ref, status::text AS status, match_group_id
                    FROM reco.reconciliation_entry_emargement
                    WHERE split_parent_hash = :h
                )
                SELECT g.id AS entry_id, g.source_hash, g.reco_id AS lot_id, g.amount,
                       g.currency, g.direction, g.value_date, g.external_ref,
                       g.status AS entry_status, g.match_group_id,
                       l.bucket_kind, l.bucket_pacs008, l.bucket_msgid,
                       l.bucket_po, l.bucket_ref, l.synthetic_only,
                       m.payment_count
                FROM ghosts g
                LEFT JOIN reco.movement_lot l ON l.id = g.reco_id
                LEFT JOIN reco.movement_lot_member m ON m.source_hash = g.source_hash
                ORDER BY ABS(g.amount) DESC, g.source_hash
                """
            ),
            {"h": parent_hash},
        ).fetchall()

    def parents_for_hashes(self, db: Session, *, source_hashes: Sequence[str]) -> Dict[str, Any]:
        """{parent source_hash: row} — feeds the ghost cards of a lot in one query."""
        if not source_hashes:
            return {}
        rows = db.execute(
            text(
                """
                SELECT source_hash, external_ref, amount, currency, direction,
                       movement_type, value_date, child_count, payment_amount,
                       shared_key_movements, parent_emarged
                FROM reco.movement_split
                WHERE source_hash = ANY(:hashes)
                """
            ),
            {"hashes": list(dict.fromkeys(source_hashes))},
        ).fetchall()
        return {row.source_hash: row for row in rows}


movement_split_repository = MovementSplitRepository()
