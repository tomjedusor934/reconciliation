"""Data access for movement lots (Finacle Batch Booking True).

Write methods NEVER commit — ``lot_service.apply_lot_batch`` owns the
transaction so a whole DAG batch (lots + members + keys) is atomic.

A lot is a (PACS008 × MSGID) bucket whose id is a uuid5 of its identity, so the
DAG can name it without asking the backend anything. That retired three pieces
of machinery this module used to carry: ``get_key_map`` (reading every key back
each run — the scaling wall), ``apply_merge`` (absorbing one lot into another
when a new movement bridged two clusters) and ``find_cross_lot_conflicts`` (the
guard that caught clustering against a stale key map). None of them have an
equivalent here: the same bucket always yields the same uuid.

Raw-SQL notes: entry statuses are compared against the enum NAMES
('PENDING', 'MATCHED', ...) — that is what ORM-written rows store.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import case, literal_column, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.movement_lot import (
    LOT_STATUS_ACTIVE,
    MovementLot,
    MovementLotKey,
    MovementLotMember,
)

logger = logging.getLogger(__name__)

# std.Payment.Status value for a payment still pending settlement. The lot view
# reads its payments straight from reco.entry_payment_status, which is keyed by
# reco_id — and for the batch-booking flow reco_id IS the lot uuid
# (movement_lot.id). So the lot's PDNG payments are exactly the rows with
# ep.reco_id = lot.id; the (reco_id, po_id) uniqueness means each payment counts
# once. The DAG (reco_payment_status) resolves every payment of a reconciliation
# group once (handling NDGB=MessageID and SCT/SDD/I/BLK pacs008), so summing
# entry_payment_status.amount over the PDNG rows yields the real pending total.
PENDING_PAYMENT_STATUS = "PDNG"

# Key rows per INSERT statement. psycopg2 interpolates parameters client-side, so
# a statement's size is bounded by nothing but the row count — and one SP bulk
# member can carry thousands of keys (see reco_datamart_bb._single_lot_report).
# At 10 000 members per batch an unbounded statement would be tens of MB of SQL
# built in Python and parsed in one shot.
KEY_INSERT_CHUNK = 10000


class MovementLotRepository:
    # ------------------------------------------------------------------
    # DAG-facing writes (no commit — the service owns the transaction)
    # ------------------------------------------------------------------

    def create_lots(
        self, db: Session, *, flow_id: int, flow_source_id: int, buckets: Sequence[dict]
    ) -> int:
        """Idempotent bucket creation (a re-pushed batch after a crash is a no-op).

        ``buckets`` carries ``lot_id`` plus the identity the uuid5 was derived
        from, so the row records what it stands for and not just an opaque id.

        Stored VERBATIM — no normalisation here. The DAG folds the identity to
        upper case before deriving the uuid5 (see ``BucketKey`` in
        reco_datamart_bb), so touching the components backend-side could only put
        them out of step with the id they produced. This is a faithful recorder.
        """
        if not buckets:
            return 0
        rows = {
            b["lot_id"]: {
                "id": b["lot_id"],
                "flow_id": flow_id,
                "flow_source_id": flow_source_id,
                "currency": "EUR",
                "status": LOT_STATUS_ACTIVE,
                "merge_conflict": False,
                "bucket_kind": b["bucket_kind"],
                "bucket_pacs008": b.get("bucket_pacs008") or "",
                "bucket_msgid": b.get("bucket_msgid") or "",
                "bucket_po": b.get("bucket_po") or "",
                "bucket_ref": b.get("bucket_ref") or "",
            }
            for b in buckets
        }
        stmt = pg_insert(MovementLot.__table__).values(list(rows.values()))
        stmt = stmt.on_conflict_do_nothing(index_elements=["id"])
        result = db.execute(stmt)
        return result.rowcount or 0

    def upsert_members(self, db: Session, rows: Sequence[dict]) -> Tuple[int, int, Dict[str, int]]:
        """Insert/refresh members keyed on source_hash.

        A real movement's amount and dates are its identity and stay immutable
        after insert (same rule as the entry upsert). A GHOST's is not: its
        amount is the sum of its bucket's payments, which grows as std.Payment
        fills in — so ``amount``/``direction`` are refreshed for rows carrying a
        ``split_parent_hash``, and left alone for everything else.
        Returns (inserted, updated, {source_hash: member_id}).
        """
        if not rows:
            return 0, 0, {}
        stmt = pg_insert(MovementLotMember.__table__).values(list(rows))
        ghost = stmt.excluded.split_parent_hash.isnot(None)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_hash"],
            set_={
                "lot_id": stmt.excluded.lot_id,
                "movement_type": stmt.excluded.movement_type,
                "transaction_particulars": stmt.excluded.transaction_particulars,
                "ref_no": stmt.excluded.ref_no,
                "remarks_1": stmt.excluded.remarks_1,
                "split_parent_hash": stmt.excluded.split_parent_hash,
                "payment_count": stmt.excluded.payment_count,
                "amount": case(
                    (ghost, stmt.excluded.amount),
                    else_=MovementLotMember.__table__.c.amount,
                ),
                "direction": case(
                    (ghost, stmt.excluded.direction),
                    else_=MovementLotMember.__table__.c.direction,
                ),
                "updated_at": text("now()"),
            },
        ).returning(
            literal_column("(xmax = 0)"),
            MovementLotMember.__table__.c.id,
            MovementLotMember.__table__.c.source_hash,
        )
        inserted = updated = 0
        ids_by_hash: Dict[str, int] = {}
        for is_insert, member_id, source_hash in db.execute(stmt).fetchall():
            ids_by_hash[source_hash] = member_id
            if is_insert:
                inserted += 1
            else:
                updated += 1
        return inserted, updated, ids_by_hash

    def insert_keys(self, db: Session, rows: Sequence[dict]) -> int:
        """Keys only ever accrue (late std.Payment data adds MSGID/PACS008 keys
        to an already-known member). Chunked — see KEY_INSERT_CHUNK."""
        if not rows:
            return 0
        deduped = {(r["member_id"], r["key_type"], r["key_value"]): r for r in rows}
        values = list(deduped.values())
        inserted = 0
        for i in range(0, len(values), KEY_INSERT_CHUNK):
            stmt = (
                pg_insert(MovementLotKey.__table__)
                .values(values[i : i + KEY_INSERT_CHUNK])
                .on_conflict_do_nothing(
                    index_elements=["member_id", "key_type", "key_value"]
                )
            )
            inserted += db.execute(stmt).rowcount or 0
        return inserted

    def sync_lot_currencies(self, db: Session, *, lot_ids: Sequence[str]) -> None:
        """Lot currency = its first member's (informational rollup)."""
        if not lot_ids:
            return
        db.execute(
            text(
                """
                UPDATE reco.movement_lot l
                SET currency = fm.currency
                FROM (
                    SELECT DISTINCT ON (lot_id) lot_id, currency
                    FROM reco.movement_lot_member
                    WHERE lot_id = ANY(:lot_ids)
                    ORDER BY lot_id, id
                ) fm
                WHERE fm.lot_id = l.id AND l.currency IS DISTINCT FROM fm.currency
                """
            ),
            {"lot_ids": list(lot_ids)},
        )

    def sync_synthetic_only(self, db: Session, *, lot_ids: Sequence[str]) -> None:
        """Flag the buckets whose every member is a ghost.

        Both sides of such a bucket are derived from the same std.Payment
        amounts, so it nets to zero by construction and its 'matched' state
        proves nothing on its own — the evidence is the parent-level
        conservation in ``movement_split``. The UI surfaces the distinction; the
        matching engine deliberately does not care.
        """
        if not lot_ids:
            return
        db.execute(
            text(
                """
                UPDATE reco.movement_lot l
                SET synthetic_only = agg.all_ghosts
                FROM (
                    SELECT lot_id, BOOL_AND(split_parent_hash IS NOT NULL) AS all_ghosts
                    FROM reco.movement_lot_member
                    WHERE lot_id = ANY(:lot_ids)
                    GROUP BY lot_id
                ) agg
                WHERE agg.lot_id = l.id AND l.synthetic_only IS DISTINCT FROM agg.all_ghosts
                """
            ),
            {"lot_ids": list(lot_ids)},
        )

    def lot_currencies(self, db: Session, *, lot_ids: Sequence[str]) -> Dict[str, str]:
        """{lot_id: currency} for the ids that exist — existence check and stored
        currency in one query (apply_lot_batch needs both)."""
        if not lot_ids:
            return {}
        rows = (
            db.query(MovementLot.id, MovementLot.currency)
            .filter(MovementLot.id.in_(list(lot_ids)))
            .all()
        )
        return {r[0]: r[1] for r in rows}

    def lots_exist(self, db: Session, *, lot_ids: Sequence[str]) -> set:
        if not lot_ids:
            return set()
        rows = db.query(MovementLot.id).filter(MovementLot.id.in_(list(lot_ids))).all()
        return {r[0] for r in rows}

    # ------------------------------------------------------------------
    # UI-facing reads (aggregates computed at read time)
    # ------------------------------------------------------------------

    _LOT_SELECT = f"""
        SELECT l.id, l.flow_id, l.flow_source_id, l.currency, l.status AS lot_status,
               l.merged_into_lot_id, l.merged_at, l.merge_conflict, l.created_at, l.updated_at,
               l.bucket_kind, l.bucket_pacs008, l.bucket_msgid, l.bucket_po, l.bucket_ref,
               l.synthetic_only, l.parent_mismatch,
               COALESCE(agg.member_count, 0) AS member_count,
               COALESCE(agg.total_debit, 0) AS total_debit,
               COALESCE(agg.total_credit, 0) AS total_credit,
               COALESCE(agg.net_amount, 0) AS net_amount,
               COALESCE(agg.currencies, ARRAY[]::varchar[]) AS currencies,
               COALESCE(agg.unbalanced_currencies, 0) AS unbalanced_currencies,
               agg.first_value_date, agg.last_value_date,
               COALESCE(st.pending_count, 0) AS pending_count,
               COALESCE(st.matched_count, 0) AS matched_count,
               COALESCE(st.excluded_count, 0) AS excluded_count,
               COALESCE(pp.pending_payment_amount, 0) AS pending_payment_amount,
               COALESCE(pp.pending_payment_count, 0) AS pending_payment_count
        FROM reco.movement_lot l
        LEFT JOIN LATERAL (
            SELECT COUNT(*) AS member_count,
                   COALESCE(SUM(CASE WHEN m.amount < 0 THEN m.amount ELSE 0 END), 0) AS total_debit,
                   COALESCE(SUM(CASE WHEN m.amount > 0 THEN m.amount ELSE 0 END), 0) AS total_credit,
                   COALESCE(SUM(m.amount), 0) AS net_amount,
                   ARRAY_AGG(DISTINCT m.currency) AS currencies,
                   MIN(m.value_date) AS first_value_date,
                   MAX(m.value_date) AS last_value_date,
                   (SELECT COUNT(*) FROM (
                        SELECT 1 FROM reco.movement_lot_member m2
                        WHERE m2.lot_id = l.id
                        GROUP BY m2.currency HAVING SUM(m2.amount) <> 0
                    ) unb) AS unbalanced_currencies
            FROM reco.movement_lot_member m
            WHERE m.lot_id = l.id
        ) agg ON TRUE
        LEFT JOIN LATERAL (
            SELECT COUNT(*) FILTER (WHERE le.status = 'PENDING') AS pending_count,
                   COUNT(*) FILTER (WHERE ee.status IN ('MATCHED', 'FORCED')) AS matched_count,
                   COUNT(*) FILTER (WHERE ee.status = 'EXCLUDED' OR le.status = 'EXCLUDED') AS excluded_count
            FROM reco.movement_lot_member m
            LEFT JOIN reco.reconciliation_entry le ON le.source_hash = m.source_hash
            LEFT JOIN reco.reconciliation_entry_emargement ee ON ee.source_hash = m.source_hash
            WHERE m.lot_id = l.id
        ) st ON TRUE
        LEFT JOIN LATERAL (
            -- Pending payment total for the lot. entry_payment_status is keyed
            -- by reco_id, which for the batch-booking flow IS the lot uuid
            -- (movement_lot.id == the members' reco_id). The (reco_id, po_id)
            -- uniqueness means each PDNG payment already counts once — no member
            -- join and no DISTINCT needed.
            SELECT COALESCE(SUM(ep.amount), 0) AS pending_payment_amount,
                   COUNT(*)                    AS pending_payment_count
            FROM reco.entry_payment_status ep
            WHERE ep.reco_id = l.id
              AND ep.status  = '{PENDING_PAYMENT_STATUS}'
        ) pp ON TRUE
    """

    def _lot_filters(
        self,
        *,
        flow_id: Optional[int],
        status: Optional[str],
        balanced: Optional[bool],
        date_from: Optional[datetime],
        date_to: Optional[datetime],
        search: Optional[str],
        lot_id: Optional[str] = None,
        bucket_kind: Optional[str] = None,
        synthetic_only: Optional[bool] = None,
        payment_gap: Optional[bool] = None,
        parent_mismatch: Optional[bool] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """(outer WHERE clause over the aggregated subquery, bind params)."""
        clauses: List[str] = []
        params: Dict[str, Any] = {}
        if lot_id is not None:
            clauses.append("id = :lot_id")
            params["lot_id"] = lot_id
        if flow_id is not None:
            clauses.append("flow_id = :flow_id")
            params["flow_id"] = flow_id
        if bucket_kind:
            clauses.append("bucket_kind = :bucket_kind")
            params["bucket_kind"] = bucket_kind
        if synthetic_only is not None:
            clauses.append("synthetic_only = :synthetic_only")
            params["synthetic_only"] = synthetic_only
        if parent_mismatch is not None:
            clauses.append("parent_mismatch = :parent_mismatch")
            params["parent_mismatch"] = parent_mismatch
        if payment_gap is not None:
            # Lots holding a ghost whose movement's booked amount disagrees with
            # what std.Payment accounts for. Scoped through the lot's own members
            # (index on movement_lot_member.lot_id, then a PK probe per ghost),
            # same shape as the key EXISTS below.
            exists = (
                "EXISTS (SELECT 1 FROM reco.movement_lot_member gm"
                " JOIN reco.movement_split gs ON gs.source_hash = gm.split_parent_hash"
                " WHERE gm.lot_id = lot_agg.id AND gs.amount <> gs.payment_amount)"
            )
            clauses.append(exists if payment_gap else f"NOT {exists}")
        if status == "merged":
            clauses.append("lot_status = 'merged'")
        elif status == "matched":
            clauses.append("lot_status = 'active' AND pending_count = 0 AND matched_count > 0")
        elif status == "pending":
            clauses.append("lot_status = 'active' AND (pending_count > 0 OR matched_count = 0)")
        if balanced is True:
            clauses.append("member_count > 0 AND unbalanced_currencies = 0")
        elif balanced is False:
            clauses.append("(member_count = 0 OR unbalanced_currencies > 0)")
        # Lots whose member activity window intersects [date_from, date_to]
        # (date_to inclusive: strictly before the next day).
        if date_from is not None:
            clauses.append("(last_value_date IS NULL OR last_value_date >= CAST(:date_from AS date))")
            params["date_from"] = date_from
        if date_to is not None:
            clauses.append("(first_value_date IS NULL OR first_value_date < (CAST(:date_to AS date) + 1))")
            params["date_to"] = date_to
        if search:
            clauses.append(
                "(id ILIKE :search_like OR EXISTS ("
                " SELECT 1 FROM reco.movement_lot_key k"
                " JOIN reco.movement_lot_member km ON km.id = k.member_id"
                " WHERE km.lot_id = lot_agg.id AND k.key_value ILIKE :search_like))"
            )
            params["search_like"] = f"%{search.strip()}%"
        where = (" WHERE " + " AND ".join(f"({c})" for c in clauses)) if clauses else ""
        return where, params

    def list_lots(
        self,
        db: Session,
        *,
        flow_id: Optional[int] = None,
        status: Optional[str] = None,
        balanced: Optional[bool] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        search: Optional[str] = None,
        bucket_kind: Optional[str] = None,
        synthetic_only: Optional[bool] = None,
        payment_gap: Optional[bool] = None,
        parent_mismatch: Optional[bool] = None,
        skip: int = 0,
        limit: int = 50,
    ) -> Tuple[List[Any], int]:
        where, params = self._lot_filters(
            flow_id=flow_id, status=status, balanced=balanced,
            date_from=date_from, date_to=date_to, search=search,
            bucket_kind=bucket_kind, synthetic_only=synthetic_only,
            payment_gap=payment_gap, parent_mismatch=parent_mismatch,
        )
        base = f"SELECT * FROM ({self._LOT_SELECT}) AS lot_agg{where}"
        total = db.execute(
            text(f"SELECT COUNT(*) FROM ({base}) AS c"), params
        ).scalar() or 0
        rows = db.execute(
            text(
                base
                + " ORDER BY last_value_date DESC NULLS LAST, created_at DESC"
                + " LIMIT :limit OFFSET :skip"
            ),
            {**params, "limit": limit, "skip": skip},
        ).fetchall()
        return rows, total

    def get_lot_summary(self, db: Session, *, lot_id: str) -> Optional[Any]:
        where, params = self._lot_filters(
            flow_id=None, status=None, balanced=None,
            date_from=None, date_to=None, search=None, lot_id=lot_id,
        )
        rows = db.execute(
            text(f"SELECT * FROM ({self._LOT_SELECT}) AS lot_agg{where}"), params
        ).fetchall()
        return rows[0] if rows else None

    _MEMBER_SELECT = """
        SELECT m.id, m.lot_id, m.source_hash, m.movement_type, m.external_ref,
               m.account, m.currency, m.amount, m.direction,
               m.value_date, m.operation_date,
               m.transaction_particulars, m.ref_no, m.remarks_1,
               m.split_parent_hash, m.payment_count,
               sp.external_ref AS split_parent_external_ref,
               sp.amount AS split_parent_amount,
               COALESCE(le.status::text, ee.status::text) AS entry_status,
               COALESCE(le.id, ee.id) AS entry_id,
               COALESCE(le.match_group_id, ee.match_group_id) AS match_group_id
        FROM reco.movement_lot_member m
        LEFT JOIN reco.reconciliation_entry le ON le.source_hash = m.source_hash
        LEFT JOIN reco.reconciliation_entry_emargement ee ON ee.source_hash = m.source_hash
        -- Ghosts only (split_parent_hash IS NULL for a real movement, so this
        -- costs a PK probe per ghost and nothing at all for the rest).
        LEFT JOIN reco.movement_split sp ON sp.source_hash = m.split_parent_hash
        WHERE m.lot_id = :lot_id
    """

    def list_members_with_status(self, db: Session, *, lot_id: str) -> List[Any]:
        """Members + their entry's status/id/match_group resolved through the
        live table first, the émargement table second (source_hash join)."""
        return db.execute(
            text(self._MEMBER_SELECT + " ORDER BY m.value_date, m.id"),
            {"lot_id": lot_id},
        ).fetchall()

    def list_members_page(
        self,
        db: Session,
        *,
        lot_id: str,
        skip: int = 0,
        limit: int = 100,
        movement_type: Optional[str] = None,
        direction: Optional[str] = None,
        entry_status: Optional[str] = None,
        search: Optional[str] = None,
        key_type: Optional[str] = None,
        key_value: Optional[str] = None,
    ) -> Tuple[List[Any], int]:
        """One page of members + total count. The key filter backs the
        aggregate-graph interaction (click a key node → its members)."""
        clauses: List[str] = []
        params: Dict[str, Any] = {"lot_id": lot_id}
        if movement_type:
            clauses.append("m.movement_type = :mtype")
            params["mtype"] = movement_type
        if direction:
            clauses.append("m.direction = :mdir")
            params["mdir"] = direction
        if entry_status:
            clauses.append("COALESCE(le.status::text, ee.status::text) = :estatus")
            params["estatus"] = entry_status.upper()
        if search:
            clauses.append(
                "(m.external_ref ILIKE :search_like OR m.ref_no ILIKE :search_like"
                " OR m.remarks_1 ILIKE :search_like"
                " OR m.transaction_particulars ILIKE :search_like)"
            )
            params["search_like"] = f"%{search.strip()}%"
        if key_type and key_value:
            clauses.append(
                "EXISTS (SELECT 1 FROM reco.movement_lot_key kf"
                " WHERE kf.member_id = m.id AND kf.key_type = :f_key_type"
                " AND kf.key_value = :f_key_value)"
            )
            params["f_key_type"] = key_type
            params["f_key_value"] = key_value
        base = self._MEMBER_SELECT + "".join(f" AND ({c})" for c in clauses)
        total = db.execute(
            text(f"SELECT COUNT(*) FROM ({base}) AS c"), params
        ).scalar() or 0
        rows = db.execute(
            text(base + " ORDER BY m.value_date, m.id LIMIT :limit OFFSET :skip"),
            {**params, "limit": limit, "skip": skip},
        ).fetchall()
        return rows, total

    def list_hub_keys(
        self, db: Session, *, lot_id: str, limit: int = 60
    ) -> Tuple[List[Any], int]:
        """Connective keys of a lot (carried by ≥ 2 members), busiest first —
        the skeleton of the aggregate graph. Returns (rows, hub_total)."""
        base = """
            SELECT k.key_type, k.key_value, COUNT(DISTINCT k.member_id) AS member_count
            FROM reco.movement_lot_key k
            JOIN reco.movement_lot_member m ON m.id = k.member_id
            WHERE m.lot_id = :lot_id
            GROUP BY k.key_type, k.key_value
            HAVING COUNT(DISTINCT k.member_id) >= 2
        """
        total = db.execute(
            text(f"SELECT COUNT(*) FROM ({base}) AS h"), {"lot_id": lot_id}
        ).scalar() or 0
        rows = db.execute(
            text(base + " ORDER BY member_count DESC, k.key_type, k.key_value LIMIT :limit"),
            {"lot_id": lot_id, "limit": limit},
        ).fetchall()
        return rows, total

    def aggregate_members_by_hub_keys(
        self, db: Session, *, lot_id: str, hub_ids: Sequence[str], limit: int = 150
    ) -> Tuple[List[Any], int]:
        """Members grouped by (movement_type, set of hub keys carried) — one
        aggregate-graph node per row. ``hub_ids`` are 'TYPE:value' strings
        (':' never appears in a key type); members carrying no hub key
        collapse into one 'isolated' group per movement_type (empty hub_sig).
        Returns (rows, group_total)."""
        base = """
            WITH member_hubs AS (
                SELECT m.id, m.movement_type, m.amount,
                       COALESCE(le.status::text, ee.status::text) AS entry_status,
                       COALESCE(
                           ARRAY_AGG(DISTINCT k.key_type || ':' || k.key_value)
                               FILTER (WHERE k.member_id IS NOT NULL),
                           ARRAY[]::text[]
                       ) AS hub_sig
                FROM reco.movement_lot_member m
                LEFT JOIN reco.reconciliation_entry le ON le.source_hash = m.source_hash
                LEFT JOIN reco.reconciliation_entry_emargement ee ON ee.source_hash = m.source_hash
                LEFT JOIN reco.movement_lot_key k
                    ON k.member_id = m.id
                    AND (k.key_type || ':' || k.key_value) = ANY(:hub_ids)
                WHERE m.lot_id = :lot_id
                GROUP BY m.id, m.movement_type, m.amount, le.status, ee.status
            ),
            groups AS (
                SELECT movement_type, hub_sig,
                       COUNT(*) AS member_count,
                       SUM(amount) AS total_amount,
                       COUNT(*) FILTER (WHERE entry_status = 'PENDING') AS pending_count,
                       COUNT(*) FILTER (WHERE entry_status IN ('MATCHED', 'FORCED')) AS matched_count,
                       COUNT(*) FILTER (WHERE entry_status = 'EXCLUDED') AS excluded_count
                FROM member_hubs
                GROUP BY movement_type, hub_sig
            )
            SELECT * FROM groups
        """
        params: Dict[str, Any] = {"lot_id": lot_id, "hub_ids": list(hub_ids)}
        total = db.execute(
            text(f"SELECT COUNT(*) FROM ({base}) AS g"), params
        ).scalar() or 0
        rows = db.execute(
            text(base + " ORDER BY member_count DESC, movement_type LIMIT :limit"),
            {**params, "limit": limit},
        ).fetchall()
        return rows, total

    def count_members_by_type(self, db: Session, *, lot_id: str) -> List[Any]:
        return db.execute(
            text(
                """
                SELECT movement_type, COUNT(*) AS member_count
                FROM reco.movement_lot_member
                WHERE lot_id = :lot_id
                GROUP BY movement_type
                ORDER BY movement_type
                """
            ),
            {"lot_id": lot_id},
        ).fetchall()

    def list_keys_for_lot(self, db: Session, *, lot_id: str) -> List[Any]:
        return db.execute(
            text(
                """
                SELECT k.id, k.member_id, k.key_type, k.key_value
                FROM reco.movement_lot_key k
                JOIN reco.movement_lot_member m ON m.id = k.member_id
                WHERE m.lot_id = :lot_id
                ORDER BY k.key_type, k.key_value, k.member_id
                """
            ),
            {"lot_id": lot_id},
        ).fetchall()

    # ------------------------------------------------------------------
    # Mega-aggregate graph (3 link nodes + movement nodes by type+direction)
    # ------------------------------------------------------------------

    def summarize_key_types(self, db: Session, *, lot_id: str) -> List[Any]:
        """One row per key type present in the lot: how many distinct values it
        carries (the 'number of links') and the total member↔key link count."""
        return db.execute(
            text(
                """
                SELECT k.key_type,
                       COUNT(DISTINCT k.key_value) AS distinct_count,
                       COUNT(*) AS member_link_count
                FROM reco.movement_lot_key k
                JOIN reco.movement_lot_member m ON m.id = k.member_id
                WHERE m.lot_id = :lot_id
                GROUP BY k.key_type
                ORDER BY k.key_type
                """
            ),
            {"lot_id": lot_id},
        ).fetchall()

    def aggregate_members_by_type_direction(
        self,
        db: Session,
        *,
        lot_id: str,
        key_type: Optional[str] = None,
        key_value: Optional[str] = None,
    ) -> List[Any]:
        """Movement nodes: members grouped by (movement_type, direction) with
        summed signed amount + status counts. When (key_type, key_value) is
        given, restrict to members carrying that exact key — the 'related nodes'
        surfaced when a value is picked in the drawer."""
        clause = ""
        params: Dict[str, Any] = {"lot_id": lot_id}
        if key_type and key_value:
            clause = (
                " AND EXISTS (SELECT 1 FROM reco.movement_lot_key kf"
                " WHERE kf.member_id = m.id AND kf.key_type = :key_type"
                " AND kf.key_value = :key_value)"
            )
            params["key_type"] = key_type
            params["key_value"] = key_value
        return db.execute(
            text(
                f"""
                WITH member_dir AS (
                    SELECT m.movement_type,
                           COALESCE(m.direction,
                                    CASE WHEN m.amount < 0 THEN 'debit' ELSE 'credit' END
                           ) AS direction,
                           m.amount,
                           le.status::text AS le_status,
                           ee.status::text AS ee_status
                    FROM reco.movement_lot_member m
                    LEFT JOIN reco.reconciliation_entry le ON le.source_hash = m.source_hash
                    LEFT JOIN reco.reconciliation_entry_emargement ee ON ee.source_hash = m.source_hash
                    WHERE m.lot_id = :lot_id{clause}
                )
                SELECT movement_type, direction,
                       COUNT(*) AS member_count,
                       COALESCE(SUM(amount), 0) AS total_amount,
                       COUNT(*) FILTER (WHERE le_status = 'PENDING') AS pending_count,
                       COUNT(*) FILTER (WHERE ee_status IN ('MATCHED', 'FORCED')) AS matched_count,
                       COUNT(*) FILTER (WHERE ee_status = 'EXCLUDED' OR le_status = 'EXCLUDED') AS excluded_count,
                       -- Payments are keyed by reco_id (lot level), not per member,
                       -- so a per-(type,direction) pending amount is no longer
                       -- attributable; the lot-wide total is shown by LotPendingNode.
                       0::numeric AS pending_payment_amount
                FROM member_dir
                GROUP BY movement_type, direction
                ORDER BY member_count DESC, movement_type, direction
                """
            ),
            params,
        ).fetchall()

    def list_graph_edges(self, db: Session, *, lot_id: str) -> List[Any]:
        """Distinct (movement_type, direction, key_type) triples — which movement
        node connects to which of the 3 link nodes."""
        return db.execute(
            text(
                """
                SELECT DISTINCT m.movement_type,
                       COALESCE(m.direction,
                                CASE WHEN m.amount < 0 THEN 'debit' ELSE 'credit' END
                       ) AS direction,
                       k.key_type
                FROM reco.movement_lot_member m
                JOIN reco.movement_lot_key k ON k.member_id = m.id
                WHERE m.lot_id = :lot_id
                """
            ),
            {"lot_id": lot_id},
        ).fetchall()

    def list_key_values(
        self,
        db: Session,
        *,
        lot_id: str,
        key_type: str,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Tuple[List[Any], int]:
        """Paginated distinct values of one key type (+ member count each) —
        the drawer list opened when a link node is clicked."""
        clauses = ["m.lot_id = :lot_id", "k.key_type = :key_type"]
        params: Dict[str, Any] = {"lot_id": lot_id, "key_type": key_type}
        if search:
            clauses.append("k.key_value ILIKE :search_like")
            params["search_like"] = f"%{search.strip()}%"
        base = f"""
            SELECT k.key_value, COUNT(DISTINCT k.member_id) AS member_count
            FROM reco.movement_lot_key k
            JOIN reco.movement_lot_member m ON m.id = k.member_id
            WHERE {' AND '.join(clauses)}
            GROUP BY k.key_value
        """
        total = db.execute(
            text(f"SELECT COUNT(*) FROM ({base}) AS c"), params
        ).scalar() or 0
        rows = db.execute(
            text(base + " ORDER BY member_count DESC, k.key_value LIMIT :limit OFFSET :skip"),
            {**params, "limit": limit, "skip": skip},
        ).fetchall()
        return rows, total


movement_lot_repository = MovementLotRepository()
