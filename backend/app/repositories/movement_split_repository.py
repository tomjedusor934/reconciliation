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

from sqlalchemy import func, literal_column, text
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
    "shared_key_movements", "claim_key_type", "claim_key_value",
)

# The canonical parent of a claim group: its OLDEST row, ties broken by
# external_ref. Ghost hashes are anchored on its account/dates and ghost
# entries carry its source_hash as split_parent_hash — one definition, used by
# every reader (resolve_group_canonicals, refresh_parent_mismatch, get_split).
_CANONICAL_ORDER = "value_date, external_ref NULLS LAST, source_hash"


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
                    # A push without a run (an operator replaying a manual
                    # reattribution) must not erase the run that created the row.
                    "ingestion_run_id": func.coalesce(
                        stmt.excluded.ingestion_run_id,
                        ReconciliationEntry.__table__.c.ingestion_run_id,
                    ),
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

    def parents_still_replaced(
        self, db: Session, *, source_hashes: Sequence[str]
    ) -> Set[str]:
        """Of ``source_hashes``, those that are split parents STILL replaced by
        ghosts — the movements an ingestion must not re-insert.

        WHY. ``withdraw_parent_movements`` DELETEs the real movement; nothing in
        ``upsert_finacle`` knew about it, so the next run re-inserted the very
        row the ghosts stand in for and the flow counted both. The DAG's own
        splits never hit this (a split movement contributes no entry at all, see
        ``plan_movement``); a reattribution committed by hand does, on every run
        that follows it.

        The test is GROUP-wide, not per parent: ghosts hang off the group's
        canonical, so a non-canonical parent has none of its own and would come
        back alone. Legacy rows written before claim groups (empty
        ``claim_key_value``) are their own group.

        Émargé ghosts count as much as live ones — a split that has been fully
        reconciled must not resurrect its parent. Conversely, a group with no
        ghost left at all is NOT protected: delete the ghosts and the next
        ingestion brings the movement back, which is the only undo there is.
        """
        hashes = list(dict.fromkeys(h for h in source_hashes if h))
        if not hashes:
            return set()
        rows = db.execute(
            text(
                """
                SELECT ms.source_hash
                FROM reco.movement_split ms
                WHERE ms.source_hash = ANY(:hashes)
                  AND EXISTS (
                      SELECT 1
                      FROM reco.movement_split sib
                      WHERE (
                              (ms.claim_key_value <> ''
                               AND sib.flow_source_id = ms.flow_source_id
                               AND sib.claim_key_type = ms.claim_key_type
                               AND sib.claim_key_value = ms.claim_key_value)
                           OR (ms.claim_key_value = ''
                               AND sib.source_hash = ms.source_hash)
                            )
                        AND (
                              EXISTS (SELECT 1 FROM reco.reconciliation_entry e
                                       WHERE e.split_parent_hash = sib.source_hash)
                           OR EXISTS (SELECT 1 FROM reco.reconciliation_entry_emargement m
                                       WHERE m.split_parent_hash = sib.source_hash)
                            )
                  )
                """
            ),
            {"hashes": hashes},
        ).fetchall()
        return {row[0] for row in rows}

    def resolve_group_canonicals(
        self, db: Session, *, flow_source_id: int, claims: Sequence[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], Any]:
        """{(claim_key_type, claim_key_value): canonical parent row}.

        The canonical parent anchors a group's ghost identities (see
        ``_CANONICAL_ORDER``). Read over ALL of ``movement_split`` — including
        rows this very transaction just upserted — so a group that already
        exists keeps its anchor when a later run pushes new parents.
        """
        claims = list(dict.fromkeys(claims))
        if not claims:
            return {}
        rows = db.execute(
            text(
                f"""
                SELECT DISTINCT ON (claim_key_type, claim_key_value)
                       claim_key_type, claim_key_value, source_hash, external_ref,
                       account, currency, value_date, operation_date,
                       transaction_particulars, ref_no, remarks_1
                FROM reco.movement_split
                WHERE flow_source_id = :fsid
                  AND (claim_key_type, claim_key_value) IN (
                      SELECT t, v FROM unnest(CAST(:types AS text[]),
                                              CAST(:values AS text[])) AS c(t, v)
                  )
                ORDER BY claim_key_type, claim_key_value, {_CANONICAL_ORDER}
                """
            ),
            {
                "fsid": flow_source_id,
                "types": [kt for kt, _v in claims],
                "values": [kv for _t, kv in claims],
            },
        ).fetchall()
        return {(r.claim_key_type, r.claim_key_value): r for r in rows}

    def group_parent_totals(
        self, db: Session, *, flow_source_id: int, claims: Sequence[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], Any]:
        """{claim: Σ amount over EVERY known parent of the group} — the batch's
        parents plus the ones earlier runs registered (observability: the group
        delta logged by apply_split_batch)."""
        claims = list(dict.fromkeys(claims))
        if not claims:
            return {}
        rows = db.execute(
            text(
                """
                SELECT claim_key_type, claim_key_value,
                       SUM(amount) AS parent_total, COUNT(*) AS parent_count
                FROM reco.movement_split
                WHERE flow_source_id = :fsid
                  AND (claim_key_type, claim_key_value) IN (
                      SELECT t, v FROM unnest(CAST(:types AS text[]),
                                              CAST(:values AS text[])) AS c(t, v)
                  )
                GROUP BY claim_key_type, claim_key_value
                """
            ),
            {
                "fsid": flow_source_id,
                "types": [kt for kt, _v in claims],
                "values": [kv for _t, kv in claims],
            },
        ).fetchall()
        return {(r.claim_key_type, r.claim_key_value): r for r in rows}

    def reap_stale_group_children(
        self,
        db: Session,
        *,
        flow_source_id: int,
        claims: Sequence[Tuple[str, str]],
        expected_hashes: Sequence[str],
    ) -> int:
        """Delete PENDING ghosts the pushed claim groups no longer produce.

        A stale ghost belongs to a bucket that disappeared (a payment moved to
        another MessageID, std.Payment corrected itself) — or was named by the
        retired per-parent scheme. Reaping is GROUP-wide: any PENDING entry
        whose ``split_parent_hash`` is one of the groups' parents — canonical or
        not — and whose hash the payload does not mention goes, so a canonical
        change can never strand the old ghosts.

        Scoped to the groups in the batch: a group absent from this push is
        untouched. Matched ghosts are left alone (émargement is never rewritten).
        """
        claims = list(dict.fromkeys(claims))
        if not claims:
            return 0
        stale = db.execute(
            text(
                """
                DELETE FROM reco.reconciliation_entry e
                WHERE e.status = 'PENDING'
                  AND e.split_parent_hash IN (
                      SELECT s.source_hash FROM reco.movement_split s
                      WHERE s.flow_source_id = :fsid
                        AND (s.claim_key_type, s.claim_key_value) IN (
                            SELECT t, v FROM unnest(CAST(:types AS text[]),
                                                    CAST(:values AS text[])) AS c(t, v)
                        )
                  )
                  AND NOT (e.source_hash = ANY(:expected))
                RETURNING e.source_hash
                """
            ),
            {
                "fsid": flow_source_id,
                "types": [kt for kt, _v in claims],
                "values": [kv for _t, kv in claims],
                "expected": list(expected_hashes),
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

    def refresh_parent_mismatch(self, db: Session) -> int:
        """Second reconciliation: recompute ``movement_lot.parent_mismatch``.

        Per claim group, Σ(parents.amount) is compared to Σ(the ghosts that
        actually exist, live and émargé) — the ghosts hang off the group's
        canonical parent. Any difference (a charge, FX, an over-fetched label
        MessageID, a reaped ghost) tags every lot carrying one of the group's
        ghosts: matched or not, the lot cannot be fully validated while its
        parent side does not add up.

        Full recompute, two set-based statements: lots with ghost members take
        their group's verdict, lots without any ghost member are cleared.
        Returns how many lots changed.
        """
        changed = db.execute(
            text(
                f"""
                WITH canon AS (
                    SELECT DISTINCT ON (flow_source_id, claim_key_type, claim_key_value)
                           flow_source_id, claim_key_type, claim_key_value, source_hash
                    FROM reco.movement_split
                    WHERE claim_key_value <> ''
                    ORDER BY flow_source_id, claim_key_type, claim_key_value,
                             {_CANONICAL_ORDER}
                ),
                grp AS (
                    SELECT flow_source_id, claim_key_type, claim_key_value,
                           SUM(amount) AS parent_total
                    FROM reco.movement_split
                    WHERE claim_key_value <> ''
                    GROUP BY 1, 2, 3
                ),
                child AS (
                    SELECT split_parent_hash, SUM(amount) AS child_total
                    FROM (
                        SELECT split_parent_hash, amount
                        FROM reco.reconciliation_entry
                        WHERE split_parent_hash IS NOT NULL
                        UNION ALL
                        SELECT split_parent_hash, amount
                        FROM reco.reconciliation_entry_emargement
                        WHERE split_parent_hash IS NOT NULL
                    ) u
                    GROUP BY 1
                ),
                mismatched AS (
                    SELECT c.source_hash
                    FROM canon c
                    JOIN grp g USING (flow_source_id, claim_key_type, claim_key_value)
                    LEFT JOIN child ch ON ch.split_parent_hash = c.source_hash
                    WHERE g.parent_total <> COALESCE(ch.child_total, 0)
                ),
                lot_flags AS (
                    SELECT m.lot_id, BOOL_OR(mm.source_hash IS NOT NULL) AS mismatch
                    FROM reco.movement_lot_member m
                    LEFT JOIN mismatched mm ON mm.source_hash = m.split_parent_hash
                    WHERE m.split_parent_hash IS NOT NULL
                    GROUP BY m.lot_id
                )
                UPDATE reco.movement_lot l
                SET parent_mismatch = f.mismatch, updated_at = now()
                FROM lot_flags f
                WHERE l.id = f.lot_id
                  AND l.parent_mismatch IS DISTINCT FROM f.mismatch
                """
            )
        ).rowcount or 0
        cleared = db.execute(
            text(
                """
                UPDATE reco.movement_lot l
                SET parent_mismatch = false, updated_at = now()
                WHERE l.parent_mismatch = true
                  AND NOT EXISTS (
                      SELECT 1 FROM reco.movement_lot_member m
                      WHERE m.lot_id = l.id AND m.split_parent_hash IS NOT NULL
                  )
                """
            )
        ).rowcount or 0
        total = changed + cleared
        if total:
            logger.info("[movement_split] parent_mismatch refreshed on %d lot(s)", total)
        return total

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
                       s.claim_key_type, s.claim_key_value, s.parent_emarged,
                       s.created_at, s.updated_at
                FROM reco.movement_split s
                WHERE s.source_hash = :h
                """
            ),
            {"h": source_hash},
        ).fetchone()

    def list_group_parents(
        self, db: Session, *, flow_source_id: int, claim_key_type: str, claim_key_value: str
    ) -> List[Any]:
        """Every parent of one claim group, canonical FIRST (see
        ``_CANONICAL_ORDER``) — feeds the group block of GET /splits."""
        return db.execute(
            text(
                f"""
                SELECT s.source_hash, s.movement_type, s.external_ref, s.amount,
                       s.currency, s.direction, s.value_date, s.payment_amount,
                       s.parent_emarged
                FROM reco.movement_split s
                WHERE s.flow_source_id = :fsid
                  AND s.claim_key_type = :kt AND s.claim_key_value = :kv
                ORDER BY {_CANONICAL_ORDER}
                """
            ),
            {"fsid": flow_source_id, "kt": claim_key_type, "kv": claim_key_value},
        ).fetchall()

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
                           value_date, external_ref, event_type,
                           status::text AS status, match_group_id
                    FROM reco.reconciliation_entry
                    WHERE split_parent_hash = :h
                    UNION ALL
                    SELECT id, source_hash, reco_id, amount, currency, direction,
                           value_date, external_ref, event_type,
                           status::text AS status, match_group_id
                    FROM reco.reconciliation_entry_emargement
                    WHERE split_parent_hash = :h
                )
                SELECT g.id AS entry_id, g.source_hash, g.reco_id AS lot_id, g.amount,
                       g.currency, g.direction, g.value_date, g.external_ref,
                       g.event_type, g.status AS entry_status, g.match_group_id,
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
                       shared_key_movements, claim_key_type, claim_key_value,
                       parent_emarged
                FROM reco.movement_split
                WHERE source_hash = ANY(:hashes)
                """
            ),
            {"hashes": list(dict.fromkeys(source_hashes))},
        ).fetchall()
        return {row.source_hash: row for row in rows}


movement_split_repository = MovementSplitRepository()
