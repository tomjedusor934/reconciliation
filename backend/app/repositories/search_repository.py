"""Identifier resolution for the deep (transversal) search.

Pure lookups: every method answers "where does this value appear?" and returns
raw identifiers. Walking the graph and assembling the payload is the service's
job.

Two rules govern this file:

* **Exact first.** Every method here compares with ``=`` on an INDEXED column.
  The broad ILIKE variants are separated out (``*_broad``) because they are
  sequential scans of multi-million-row tables and must never run implicitly.
* **Both entry tables, always.** A movement that has been reconciled no longer
  exists in ``reco.reconciliation_entry`` — ``move_matched_to_emargement``
  DELETEs it — so any read that ignores ``reconciliation_entry_emargement``
  silently returns nothing for reconciled data.
"""
import logging
from typing import Any, Dict, List, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.movement_lot import KEY_TYPES

logger = logging.getLogger(__name__)

# Entry columns a value can be matched against, with their table alias. Only
# ``reco_id`` is indexed (ix_reconciliation_entry_reco_id / ix_emargement_reco_id);
# the others are declared here for the broad pass and are scans.
# These names are code constants interpolated into SQL; the searched value is
# always a bound parameter, never interpolated.
_ENTRY_EXACT_FIELDS = ("reco_id", "external_ref", "ref_no", "remarks_1", "transaction_id")

_ENTRY_TABLES = (
    ("live", "reco.reconciliation_entry"),
    ("emargement", "reco.reconciliation_entry_emargement"),
)


class SearchRepository:
    # ------------------------------------------------------------------
    # Payment number → reco_id / lot (the three indexed paths)
    # ------------------------------------------------------------------

    def reco_ids_by_po_id(self, db: Session, *, value: str) -> List[str]:
        """Path A: entry_payment_status.po_id → reco_id (ix_..._po_id).

        For the batch-booking flow that reco_id IS the lot uuid, which makes this
        the cheapest payment-number → lot edge in the system.
        """
        rows = db.execute(
            text(
                "SELECT DISTINCT reco_id FROM reco.entry_payment_status WHERE po_id = :v"
            ),
            {"v": value},
        ).fetchall()
        return [r[0] for r in rows]

    def lot_ids_by_key_value(self, db: Session, *, value: str) -> List[Dict[str, Any]]:
        """Path B: movement_lot_key → member → lot.

        ``key_type`` is constrained with ANY(...) on purpose: the index is
        (key_type, key_value) and Postgres has no index skip scan, so a bare
        ``key_value = :v`` would degrade to a sequential scan. Pinning the
        leading column keeps an index scan AND covers PACS008 / MSGID lookups.
        """
        rows = db.execute(
            text(
                """
                SELECT DISTINCT m.lot_id, k.key_type
                FROM reco.movement_lot_key k
                JOIN reco.movement_lot_member m ON m.id = k.member_id
                WHERE k.key_type = ANY(:key_types) AND k.key_value = :v
                """
            ),
            {"key_types": list(KEY_TYPES), "v": value},
        ).fetchall()
        return [{"lot_id": r.lot_id, "key_type": r.key_type} for r in rows]

    def bulk_reco_id_by_po_id(self, db: Session, *, value: str) -> List[str]:
        """Path C: payment_bulk_key.po_id (PK) → the bulk's MessageID."""
        rows = db.execute(
            text("SELECT reco_id FROM reco.payment_bulk_key WHERE po_id = :v"),
            {"v": value},
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Direct hits on the other identifier holders
    # ------------------------------------------------------------------

    def lot_exists(self, db: Session, *, value: str) -> bool:
        row = db.execute(
            text("SELECT 1 FROM reco.movement_lot WHERE id = :v"), {"v": value}
        ).scalar()
        return bool(row)

    def reco_ids_from_entries(self, db: Session, *, value: str) -> List[Dict[str, Any]]:
        """Entries of BOTH tables carrying the value on any identifier field.

        Returns the reco_id plus which field matched, so the UI can explain why a
        result showed up. Only the reco_id branch is indexed — the others are the
        expensive part of the exact pass and are bounded by the caller's LIMIT.
        """
        conditions = " OR ".join(f"{f} = :v" for f in _ENTRY_EXACT_FIELDS)
        matched_field = " ".join(
            f"WHEN {f} = :v THEN '{f}'" for f in _ENTRY_EXACT_FIELDS
        )
        union = " UNION ALL ".join(
            f"""
            SELECT '{source}' AS source, reco_id,
                   CASE {matched_field} END AS matched_field
            FROM {table} WHERE {conditions}
            """
            for source, table in _ENTRY_TABLES
        )
        rows = db.execute(
            text(f"SELECT DISTINCT source, reco_id, matched_field FROM ({union}) e"),
            {"v": value},
        ).fetchall()
        return [
            {"source": r.source, "reco_id": r.reco_id, "field": r.matched_field}
            for r in rows
        ]

    def reco_ids_from_entries_broad(
        self, db: Session, *, value: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Broad fallback: ILIKE '%value%' over the same fields.

        Sequential scan of both entry tables — only ever called when the exact
        pass found nothing AND the user explicitly asked for it.
        """
        conditions = " OR ".join(f"{f} ILIKE :v" for f in _ENTRY_EXACT_FIELDS)
        matched_field = " ".join(
            f"WHEN {f} ILIKE :v THEN '{f}'" for f in _ENTRY_EXACT_FIELDS
        )
        union = " UNION ALL ".join(
            f"""
            SELECT '{source}' AS source, reco_id,
                   CASE {matched_field} END AS matched_field
            FROM {table} WHERE {conditions}
            """
            for source, table in _ENTRY_TABLES
        )
        rows = db.execute(
            text(
                f"SELECT DISTINCT source, reco_id, matched_field "
                f"FROM ({union}) e WHERE reco_id IS NOT NULL LIMIT :lim"
            ),
            {"v": f"%{value}%", "lim": limit},
        ).fetchall()
        return [
            {"source": r.source, "reco_id": r.reco_id, "field": r.matched_field}
            for r in rows
        ]

    def lot_ids_by_key_value_broad(
        self, db: Session, *, value: str, limit: int = 200
    ) -> List[Dict[str, Any]]:
        """Broad variant of path B — the ILIKE defeats ix_movement_lot_key_value."""
        rows = db.execute(
            text(
                """
                SELECT DISTINCT m.lot_id, k.key_type
                FROM reco.movement_lot_key k
                JOIN reco.movement_lot_member m ON m.id = k.member_id
                WHERE k.key_value ILIKE :v
                LIMIT :lim
                """
            ),
            {"v": f"%{value}%", "lim": limit},
        ).fetchall()
        return [{"lot_id": r.lot_id, "key_type": r.key_type} for r in rows]

    # ------------------------------------------------------------------
    # Expansion
    # ------------------------------------------------------------------

    def payments_for_reco_ids(
        self, db: Session, *, reco_ids: Sequence[str], limit: int = 500
    ) -> List[Any]:
        """The payments of these reconciliation groups (ix_..._reco_id)."""
        if not reco_ids:
            return []
        return db.execute(
            text(
                """
                SELECT reco_id, po_id, status, amount, payment_timestamp
                FROM reco.entry_payment_status
                WHERE reco_id = ANY(:ids)
                ORDER BY reco_id, po_id
                LIMIT :lim
                """
            ),
            {"ids": list(reco_ids), "lim": limit},
        ).fetchall()

    def merge_chain(self, db: Session, *, lot_ids: Sequence[str]) -> List[str]:
        """Lots related to these by a merge, followed BOTH ways.

        Historical only: the retired union-find clustering could absorb one lot
        into another, relinking members and live PENDING entries to the survivor
        without rewriting entry_payment_status.reco_id, and already-émargé
        entries kept the absorbed lot's uuid. So a payment still resolves to an
        absorbed lot while its members live in the survivor — both must be
        surfaced. Buckets never merge, so no new chain can appear. The visited
        set guards against a cycle.
        """
        if not lot_ids:
            return []
        seen = set(lot_ids)
        frontier = list(lot_ids)
        while frontier:
            rows = db.execute(
                text(
                    """
                    SELECT id, merged_into_lot_id FROM reco.movement_lot
                    WHERE merged_into_lot_id = ANY(:ids) OR id = ANY(:ids)
                    """
                ),
                {"ids": frontier},
            ).fetchall()
            nxt = set()
            for r in rows:
                for candidate in (r.id, r.merged_into_lot_id):
                    if candidate and candidate not in seen:
                        seen.add(candidate)
                        nxt.add(candidate)
            frontier = list(nxt)
        return sorted(seen)


search_repository = SearchRepository()
