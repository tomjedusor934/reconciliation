"""Data access for per-reco payment statuses (std.Payment sync).

Write methods NEVER commit — the service owns the transaction, mirroring
movement_lot_repository. Rows are keyed by ``reco_id`` (see EntryPaymentStatus).
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import literal_column, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.models.payment_status import EntryPaymentStatus

logger = logging.getLogger(__name__)


@dataclass
class PaymentAggregate:
    """The payments of ONE reconciliation group, summarized for the operational
    view: how many payments per status, and the span of their acceptance
    timestamps (both bounds equal when the group has a single timestamp)."""

    statuses: Dict[str, int] = field(default_factory=dict)
    payment_timestamp_min: Optional[datetime] = None
    payment_timestamp_max: Optional[datetime] = None

    def merge_span(self, low: Optional[datetime], high: Optional[datetime]) -> None:
        """Widen the span with one status bucket's MIN/MAX (NULLs ignored)."""
        if low is not None and (self.payment_timestamp_min is None or low < self.payment_timestamp_min):
            self.payment_timestamp_min = low
        if high is not None and (self.payment_timestamp_max is None or high > self.payment_timestamp_max):
            self.payment_timestamp_max = high


class PaymentStatusRepository:
    # Columns refreshed from std.Payment on every sync. A row is only rewritten
    # when at least one of them actually moved (WHERE on the conflict clause —
    # every run re-pushes all pending recos, most rows haven't changed). Any new
    # synced column must be listed here, or its updates would be swallowed.
    _SYNCED_COLUMNS = ("status", "amount", "payment_timestamp")

    def upsert_statuses(self, db: Session, rows: Sequence[dict]) -> Tuple[int, int, int]:
        """Insert/refresh (reco_id, po_id) -> (status, amount, payment_timestamp).
        Returns (inserted, updated, unchanged)."""
        if not rows:
            return 0, 0, 0
        deduped = {(r["reco_id"], r["po_id"]): r for r in rows}
        table = EntryPaymentStatus.__table__
        stmt = pg_insert(table).values(list(deduped.values()))
        changed = or_(
            *(
                table.c[col].is_distinct_from(stmt.excluded[col])
                for col in self._SYNCED_COLUMNS
            )
        )
        stmt = stmt.on_conflict_do_update(
            constraint="uq_entry_payment_status_reco_po",
            set_={
                **{col: stmt.excluded[col] for col in self._SYNCED_COLUMNS},
                "updated_at": text("now()"),
            },
            where=changed,
        ).returning(literal_column("(xmax = 0)"))
        inserted = updated = 0
        for (is_insert,) in db.execute(stmt).fetchall():
            if is_insert:
                inserted += 1
            else:
                updated += 1
        return inserted, updated, len(deduped) - inserted - updated

    _MOVEMENT_COLUMNS = """
        reco_id, external_ref, account, value_date, operation_date,
        transaction_particulars, ref_no, remarks_1,
        payload_raw->>'Initiating_channel' AS initiating_channel
    """

    def list_movements(
        self, db: Session, *, flow_id: int, scope: str = "pending",
        skip: int = 0, limit: int = 1000,
    ) -> List[Any]:
        """One page of the flow's movements for the payment-status DAG task:
        the reco_id (payment-status key) + the fields PO derivation reads.
        ``pending`` = live PENDING entries only (the sync perimeter);
        ``all`` = live + émargement (first-launch full sync)."""
        if scope == "all":
            base = f"""
                SELECT {self._MOVEMENT_COLUMNS}, id FROM reco.reconciliation_entry
                WHERE flow_id = :flow_id AND external_ref IS NOT NULL
                UNION ALL
                SELECT {self._MOVEMENT_COLUMNS}, id FROM reco.reconciliation_entry_emargement
                WHERE flow_id = :flow_id AND external_ref IS NOT NULL
                ORDER BY id LIMIT :limit OFFSET :skip
            """
        else:
            base = f"""
                SELECT {self._MOVEMENT_COLUMNS}, id FROM reco.reconciliation_entry
                WHERE flow_id = :flow_id AND status = 'PENDING'
                  AND external_ref IS NOT NULL
                ORDER BY id LIMIT :limit OFFSET :skip
            """
        return db.execute(
            text(base), {"flow_id": flow_id, "limit": limit, "skip": skip}
        ).fetchall()

    def aggregate_for_reco_ids(
        self, db: Session, *, reco_ids: Sequence[str]
    ) -> Dict[str, PaymentAggregate]:
        """reco_id -> payment aggregate for one page of the operational view.

        One pass: the status histogram and the acceptance-timestamp span come
        from the same scan (a reco carries N payments, hence N timestamps — the
        view shows the span).
        """
        if not reco_ids:
            return {}
        rows = db.execute(
            text(
                """
                SELECT reco_id, COALESCE(status, '?') AS status, COUNT(*) AS n,
                       MIN(payment_timestamp) AS pts_min, MAX(payment_timestamp) AS pts_max
                FROM reco.entry_payment_status
                WHERE reco_id = ANY(:ids)
                GROUP BY reco_id, COALESCE(status, '?')
                """
            ),
            {"ids": list(reco_ids)},
        ).fetchall()
        result: Dict[str, PaymentAggregate] = {}
        for r in rows:
            agg = result.get(r.reco_id)
            if agg is None:
                agg = result[r.reco_id] = PaymentAggregate()
            agg.statuses[r.status] = r.n
            agg.merge_span(r.pts_min, r.pts_max)
        return result

    def distinct_statuses(self, db: Session) -> List[str]:
        """All distinct payment-status values present ('?' = NULL/unknown),
        busiest first — the operational filter's option list."""
        rows = db.execute(
            text(
                """
                SELECT COALESCE(status, '?') AS status, COUNT(*) AS n
                FROM reco.entry_payment_status
                GROUP BY COALESCE(status, '?')
                ORDER BY n DESC, status
                """
            )
        ).fetchall()
        return [r.status for r in rows]


payment_status_repository = PaymentStatusRepository()
