"""Data access for the bulk-booking key map (po_id -> bulk reco_id).

Write methods NEVER commit — the caller owns the transaction, mirroring
payment_status_repository / movement_lot_repository.
"""
import logging
from typing import Dict, Sequence, Tuple

from sqlalchemy import literal_column, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.payment_bulk_key import PaymentBulkKey

logger = logging.getLogger(__name__)


class PaymentBulkKeyRepository:
    def get_map(self, db: Session) -> Dict[str, str]:
        """The whole {po_id -> reco_id} map. Only bulk members are stored (the 1-1
        instant majority never lands here), so it stays small enough to hand to the
        DAG in one go — same idea as the BB lot key map."""
        return {
            po_id: reco_id
            for po_id, reco_id in db.query(
                PaymentBulkKey.po_id, PaymentBulkKey.reco_id
            ).all()
        }

    def upsert_many(self, db: Session, rows: Sequence[dict]) -> Tuple[int, int]:
        """Insert/refresh po_id -> (reco_id, init_module, member_count). Rows whose
        reco_id is unchanged are NOT rewritten (a bulk is re-detected on every run
        it has movements in). Returns (inserted, updated)."""
        if not rows:
            return 0, 0
        deduped = {r["po_id"]: r for r in rows}
        table = PaymentBulkKey.__table__
        stmt = pg_insert(table).values(list(deduped.values()))
        stmt = stmt.on_conflict_do_update(
            index_elements=["po_id"],
            set_={
                "reco_id": stmt.excluded.reco_id,
                "init_module": stmt.excluded.init_module,
                "member_count": stmt.excluded.member_count,
                "updated_at": text("now()"),
            },
            where=(
                table.c.reco_id.is_distinct_from(stmt.excluded.reco_id)
                | table.c.member_count.is_distinct_from(stmt.excluded.member_count)
            ),
        ).returning(literal_column("(xmax = 0)"))
        inserted = updated = 0
        for (is_insert,) in db.execute(stmt).fetchall():
            if is_insert:
                inserted += 1
            else:
                updated += 1
        return inserted, updated


payment_bulk_key_repository = PaymentBulkKeyRepository()
