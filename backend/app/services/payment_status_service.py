"""Per-reco payment statuses (std.Payment sync from the DAG).

Rows are keyed by ``reco_id`` — the DAG sends it directly (the reconciliation
group / lot uuid the movement belongs to), so no source_hash resolution is
needed. Movements with a NULL / "Not Supported" reco_id are skipped (they are
not a real reconciliation group).
"""
import logging
from typing import Any, Dict, List, Sequence

from sqlalchemy.orm import Session

from app.models.flow import Flow
from app.models.reconciliation_entry import UNRESOLVED_RECO_ID
from app.repositories.payment_status_repository import payment_status_repository

logger = logging.getLogger(__name__)


class PaymentStatusService:
    def apply_status_batch(
        self, db: Session, *, flow: Flow, rows: Sequence[Any]
    ) -> Dict[str, int]:
        """Upsert one batch of (reco_id, po_id, status, amount) rows.

        Rows without a usable reco_id (empty or the "Not Supported" sentinel)
        are counted ``skipped`` — they carry no reconciliation group to key on.
        """
        to_upsert: List[dict] = []
        skipped = 0
        for r in rows:
            reco_id = (r.reco_id or "").strip()
            if not reco_id or reco_id == UNRESOLVED_RECO_ID:
                skipped += 1
                continue
            to_upsert.append(
                {
                    "reco_id": reco_id,
                    "po_id": r.po_id,
                    "status": r.status,
                    "amount": getattr(r, "amount", None),
                }
            )
        inserted, updated, unchanged = payment_status_repository.upsert_statuses(db, to_upsert)
        db.commit()
        if skipped:
            logger.info(
                "[payment_status] flow %s: %d row(s) skipped (no usable reco_id)",
                flow.code, skipped,
            )
        return {
            "inserted": inserted, "updated": updated,
            "unchanged": unchanged, "skipped": skipped,
        }

    def list_movements(
        self, db: Session, *, flow: Flow, scope: str = "pending",
        skip: int = 0, limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        rows = payment_status_repository.list_movements(
            db, flow_id=flow.id, scope=scope, skip=skip, limit=limit
        )
        return [
            {
                "reco_id": r.reco_id,
                "external_ref": r.external_ref,
                "account": r.account,
                "value_date": r.value_date,
                "operation_date": r.operation_date,
                "transaction_particulars": r.transaction_particulars,
                "ref_no": r.ref_no,
                "remarks_1": r.remarks_1,
                "initiating_channel": r.initiating_channel,
            }
            for r in rows
        ]

    def aggregate_for_reco_ids(
        self, db: Session, *, reco_ids: Sequence[str]
    ) -> Dict[str, Dict[str, int]]:
        return payment_status_repository.aggregate_for_reco_ids(db, reco_ids=reco_ids)

    def distinct_statuses(self, db: Session) -> List[str]:
        return payment_status_repository.distinct_statuses(db)


payment_status_service = PaymentStatusService()
