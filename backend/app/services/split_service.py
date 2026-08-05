"""Split service — the real movements replaced by ghost entries.

Owns the split batch transaction pushed by the ingest_finacle_bb DAG, and the
UI read that walks a ghost back to the movement it came from.

The batch is atomic for a reason that is not cosmetic: registering a parent and
withdrawing its real movement are two halves of one fact. Between them the
database says the movement AND its ghosts both count, which is exactly the
double count the whole design exists to avoid. So either both land, or neither.
"""
import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.flow import Flow, FlowSource
from app.repositories.movement_split_repository import movement_split_repository
from app.services.lot_service import _to_utc, lot_service

logger = logging.getLogger(__name__)


class SplitService:
    # ------------------------------------------------------------------
    # DAG-facing writes
    # ------------------------------------------------------------------

    def apply_split_batch(
        self,
        db: Session,
        *,
        flow: Flow,
        source: FlowSource,
        parents: List[Any],
        run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """One atomic transaction: register parents → materialise their ghosts →
        withdraw the real movements → reap the ghosts a parent no longer
        produces → commit.

        Every hash is recomputed here from identity fields, exactly like
        ``lot_service.apply_lot_batch`` does for a member — so a parent addresses
        the very row the finacle push created for the same movement, a ghost
        entry and its lot member land on the same source_hash, and nothing has to
        travel over the wire pre-hashed.
        """
        try:
            parent_rows: Dict[str, dict] = {}
            ghost_rows: Dict[str, dict] = {}
            expected: List[Tuple[str, str]] = []
            for parent in parents:
                value_date = _to_utc(parent.value_date)
                operation_date = _to_utc(parent.operation_date) or value_date
                source_hash = lot_service.member_to_source_hash(
                    flow_id=flow.id,
                    external_ref=parent.external_ref,
                    account=parent.account,
                    value_date=parent.value_date,
                    operation_date=parent.operation_date,
                )
                children = list(parent.children or [])
                child_amount = sum((c.amount for c in children), Decimal("0"))
                # Ghosts only ever share out the movement's own amount, so this
                # is now a genuine invariant, not a tolerance: a violation means
                # the two sides disagree about what a split IS. Worth a loud log
                # rather than a silently wrong parent row.
                if child_amount != parent.amount:
                    logger.warning(
                        "[split] parent %s: children sum to %s but movement is %s "
                        "(difference %s carried by nothing — the allocation is broken)",
                        parent.external_ref, child_amount, parent.amount,
                        parent.amount - child_amount,
                    )
                parent_rows[source_hash] = {
                    "source_hash": source_hash,
                    "flow_id": flow.id,
                    "flow_source_id": source.id,
                    "movement_type": parent.movement_type,
                    "external_ref": parent.external_ref,
                    "account": parent.account,
                    "currency": parent.currency,
                    "amount": parent.amount,
                    "direction": parent.direction,
                    "value_date": value_date,
                    "operation_date": operation_date,
                    "transaction_particulars": parent.transaction_particulars,
                    "ref_no": parent.ref_no,
                    "remarks_1": parent.remarks_1,
                    "payload_raw": parent.payload_raw,
                    "child_count": len(children),
                    "payment_count": parent.payment_count or 0,
                    "child_amount": child_amount,
                    "payment_amount": parent.payment_amount or Decimal("0"),
                    "shared_key_movements": parent.shared_key_movements or 1,
                }

                for child in children:
                    child_hash = lot_service.member_to_source_hash(
                        flow_id=flow.id,
                        external_ref=child.external_ref,
                        account=parent.account,
                        value_date=parent.value_date,
                        operation_date=parent.operation_date,
                    )
                    expected.append((source_hash, child_hash))
                    ghost_rows[child_hash] = {
                        "flow_id": flow.id,
                        "ingestion_run_id": run_id,
                        "reco_id": child.lot_id,
                        "account": parent.account,
                        "currency": parent.currency,
                        "amount": child.amount,
                        "direction": child.direction or parent.direction,
                        "value_date": value_date,
                        "operation_date": operation_date,
                        "event_type": parent.event_type,
                        "external_ref": child.external_ref,
                        "transaction_particulars": parent.transaction_particulars,
                        "ref_no": parent.ref_no,
                        "remarks_1": parent.remarks_1,
                        "transaction_id": parent.transaction_id,
                        # Deliberately NOT the parent's raw datamart row: a ghost
                        # is an app-side construct, and what a reader needs is
                        # which slice of which movement it stands for.
                        "payload_raw": {
                            "split_of": parent.external_ref,
                            "bucket_kind": child.bucket_kind,
                            "bucket_pacs008": child.bucket_pacs008,
                            "bucket_msgid": child.bucket_msgid,
                            "bucket_po": child.bucket_po,
                            "payment_count": child.payment_count,
                        },
                        "source_hash": child_hash,
                        "split_parent_hash": source_hash,
                        "status": "PENDING",
                    }

            inserted, updated = movement_split_repository.upsert_parents(
                db, list(parent_rows.values())
            )
            ghosts_inserted, ghosts_updated, ghosts_skipped = (
                movement_split_repository.upsert_ghost_entries(db, list(ghost_rows.values()))
            )
            withdrawn, emarged = movement_split_repository.withdraw_parent_movements(
                db, parent_hashes=list(parent_rows)
            )
            if emarged:
                movement_split_repository.flag_emarged(db, parent_hashes=sorted(emarged))
                logger.warning(
                    "[split] %d parent movement(s) already émargé — NOT withdrawn, "
                    "their ghosts double count until arbitrated (e.g. %s)",
                    len(emarged), sorted(emarged)[:3],
                )
            reaped = movement_split_repository.reap_stale_children(db, expected=expected)

            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError(f"split batch violates integrity: {exc.orig}") from exc
        except Exception:
            db.rollback()
            raise

        return {
            "parents_inserted": inserted,
            "parents_updated": updated,
            "ghosts_inserted": ghosts_inserted,
            "ghosts_updated": ghosts_updated,
            "ghosts_skipped": ghosts_skipped,
            "movements_withdrawn": withdrawn,
            "parents_emarged": len(emarged),
            "ghosts_reaped": reaped,
        }

    # ------------------------------------------------------------------
    # UI-facing reads
    # ------------------------------------------------------------------

    def get_split(self, db: Session, *, source_hash: str) -> Optional[Dict[str, Any]]:
        """The real movement, its ghosts, and whether the amount is conserved."""
        parent = movement_split_repository.get_parent(db, source_hash=source_hash)
        if parent is None:
            return None
        children = movement_split_repository.list_children(db, parent_hash=source_hash)
        child_amount = sum((c.amount for c in children), Decimal("0"))
        return {
            "parent": {
                "source_hash": parent.source_hash,
                "flow_id": parent.flow_id,
                "movement_type": parent.movement_type,
                "external_ref": parent.external_ref,
                "account": parent.account,
                "currency": parent.currency,
                "amount": parent.amount,
                "direction": parent.direction,
                "value_date": parent.value_date,
                "operation_date": parent.operation_date,
                "transaction_particulars": parent.transaction_particulars,
                "ref_no": parent.ref_no,
                "remarks_1": parent.remarks_1,
                "payment_count": parent.payment_count,
                "shared_key_movements": parent.shared_key_movements,
                "parent_emarged": parent.parent_emarged,
            },
            "children": [
                {
                    "entry_id": c.entry_id,
                    "source_hash": c.source_hash,
                    "lot_id": c.lot_id,
                    "amount": c.amount,
                    "currency": c.currency,
                    "direction": c.direction,
                    "value_date": c.value_date,
                    "external_ref": c.external_ref,
                    "entry_status": c.entry_status.lower() if c.entry_status else None,
                    "match_group_id": c.match_group_id,
                    "payment_count": c.payment_count,
                    "bucket_kind": c.bucket_kind,
                    "bucket_pacs008": c.bucket_pacs008 or None,
                    "bucket_msgid": c.bucket_msgid or None,
                    "bucket_po": c.bucket_po or None,
                    "bucket_ref": c.bucket_ref or None,
                    "synthetic_only": bool(c.synthetic_only),
                }
                for c in children
            ],
            "conservation": {
                # children_amount is recomputed from the ghosts that actually
                # exist rather than read off the parent: a reaped or manually
                # deleted ghost must show up as a hole here, not stay hidden
                # behind the stored total. It equals parent_amount on a healthy
                # split — the ghosts only share out the movement's own money.
                "parent_amount": parent.amount,
                "children_amount": child_amount,
                "missing_amount": parent.amount - child_amount,
                # A different question entirely: does std.Payment agree with what
                # finacle booked? Non-zero is a data-quality signal, not a hole.
                "payment_amount": parent.payment_amount,
                "payment_gap": parent.amount - parent.payment_amount,
                "child_count": len(children),
            },
        }


split_service = SplitService()
