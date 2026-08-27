"""Split service — the real movements replaced by claim-group ghost entries.

Owns the split batch transaction pushed by the ingest_finacle_bb DAG, and the
UI read that walks a ghost back to the movement(s) it stands for.

The batch is atomic for a reason that is not cosmetic: registering a group's
parents and withdrawing their real movements are two halves of one fact.
Between them the database says the movements AND their ghosts both count, which
is exactly the double count the whole design exists to avoid. So either both
land, or neither.

Ghost identities are anchored on each group's CANONICAL parent — the oldest
``movement_split`` row of the group, resolved AFTER the batch's parents are
upserted. A brand-new group anchors on the run's own oldest parent; an existing
group keeps its stored anchor, so re-emitting it upserts the very same ghost
rows however the run happened to see the group.
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


def _claim_of(group: Any) -> Tuple[str, str]:
    """The claim key, uppercased — one folding rule, shared with the DAG
    (SQL Server's collation is case-insensitive, see ``BucketKey``)."""
    return (
        (group.claim_key_type or "").strip().upper(),
        (group.claim_key_value or "").strip().upper(),
    )


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
        groups: List[Any],
        run_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """One atomic transaction: register every group's parents → resolve each
        group's canonical → materialise the group ghosts → withdraw the real
        movements → reap the ghosts the groups no longer produce → commit.

        Every hash is recomputed here from identity fields, exactly like
        ``lot_service.apply_lot_batch`` does for a member — so a parent
        addresses the very row the finacle push created for the same movement,
        a ghost entry and its lot member land on the same source_hash, and
        nothing has to travel over the wire pre-hashed.
        """
        try:
            parent_rows: Dict[str, dict] = {}
            for group in groups:
                claim_type, claim_value = _claim_of(group)
                children = list(group.children or [])
                child_amount = sum((c.amount for c in children), Decimal("0"))
                for parent in group.parents or []:
                    value_date = _to_utc(parent.value_date)
                    operation_date = _to_utc(parent.operation_date) or value_date
                    source_hash = lot_service.member_to_source_hash(
                        flow_id=flow.id,
                        external_ref=parent.external_ref,
                        account=parent.account,
                        value_date=parent.value_date,
                        operation_date=parent.operation_date,
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
                        # The GROUP's ghosts — identical on every parent of the
                        # group; only the group has children, not the movement.
                        "child_count": len(children),
                        "payment_count": parent.payment_count or 0,
                        "child_amount": child_amount,
                        "payment_amount": parent.payment_amount or Decimal("0"),
                        "shared_key_movements": parent.shared_key_movements or 1,
                        "claim_key_type": claim_type,
                        "claim_key_value": claim_value,
                    }

            inserted, updated = movement_split_repository.upsert_parents(
                db, list(parent_rows.values())
            )

            # Canonicals AFTER the upsert: a new group anchors on the batch's
            # own oldest parent, an existing one keeps its stored anchor.
            claims = [_claim_of(g) for g in groups]
            canonicals = movement_split_repository.resolve_group_canonicals(
                db, flow_source_id=source.id, claims=claims
            )

            ghost_rows: Dict[str, dict] = {}
            for group in groups:
                claim = _claim_of(group)
                canon = canonicals.get(claim)
                if canon is None:
                    # Only reachable on a group pushed with no parents at all —
                    # nothing to anchor its ghosts on, nothing to withdraw.
                    logger.warning(
                        "[split] group %s:%s has no registered parent — "
                        "%d ghost(s) skipped", claim[0], claim[1],
                        len(group.children or []),
                    )
                    continue
                canon_value = _to_utc(canon.value_date)
                canon_operation = _to_utc(canon.operation_date) or canon_value
                for child in group.children or []:
                    child_hash = lot_service.member_to_source_hash(
                        flow_id=flow.id,
                        external_ref=child.external_ref,
                        account=canon.account,
                        value_date=canon.value_date,
                        operation_date=canon.operation_date,
                    )
                    ghost_rows[child_hash] = {
                        "flow_id": flow.id,
                        "ingestion_run_id": run_id,
                        "reco_id": child.lot_id,
                        "account": canon.account,
                        "currency": group.currency,
                        "amount": child.amount,
                        "direction": child.direction,
                        "value_date": canon_value,
                        "operation_date": canon_operation,
                        "event_type": group.event_type,
                        "external_ref": child.external_ref,
                        "transaction_particulars": canon.transaction_particulars,
                        "ref_no": canon.ref_no,
                        "remarks_1": canon.remarks_1,
                        # Deliberately NOT a movement's raw datamart row: a ghost
                        # is an app-side construct, and what a reader needs is
                        # which bucket of which claim group it stands for.
                        "payload_raw": {
                            "split_of": canon.external_ref,
                            "claim_key": f"{claim[0]}:{claim[1]}",
                            "bucket_kind": child.bucket_kind,
                            "bucket_pacs008": child.bucket_pacs008,
                            "bucket_msgid": child.bucket_msgid,
                            "bucket_po": child.bucket_po,
                            "payment_count": child.payment_count,
                        },
                        "source_hash": child_hash,
                        "split_parent_hash": canon.source_hash,
                        "status": "PENDING",
                    }

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
                    "they double count against their group's ghosts until "
                    "arbitrated (e.g. %s)",
                    len(emarged), sorted(emarged)[:3],
                )
            reaped = movement_split_repository.reap_stale_group_children(
                db,
                flow_source_id=source.id,
                claims=claims,
                expected_hashes=list(ghost_rows),
            )

            # Observability, not an invariant: Σ(ghosts) ≠ Σ(parents) is a fact
            # the second reconciliation will tag, not an error to reject.
            totals = movement_split_repository.group_parent_totals(
                db, flow_source_id=source.id, claims=claims
            )
            for group in groups:
                claim = _claim_of(group)
                stored = totals.get(claim)
                if stored is None:
                    continue
                child_total = sum((c.amount for c in group.children or []), Decimal("0"))
                delta = stored.parent_total - child_total
                if delta:
                    logger.info(
                        "[split] group %s:%s does not add up: %d parent(s) book %s, "
                        "%d ghost(s) carry %s (delta %s) — lots will be tagged "
                        "parent_mismatch at the next reconcile",
                        claim[0], claim[1], stored.parent_count, stored.parent_total,
                        len(group.children or []), child_total, delta,
                    )

            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError(f"split batch violates integrity: {exc.orig}") from exc
        except Exception:
            db.rollback()
            raise

        return {
            "groups": len(groups),
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
        """One parent, its claim group, and the group's ghosts.

        The ghosts hang off the group's CANONICAL parent, so whichever parent
        of the group the caller starts from, the same children and the same
        group reconciliation come back.
        """
        parent = movement_split_repository.get_parent(db, source_hash=source_hash)
        if parent is None:
            return None

        if parent.claim_key_value:
            siblings = movement_split_repository.list_group_parents(
                db,
                flow_source_id=parent.flow_source_id,
                claim_key_type=parent.claim_key_type,
                claim_key_value=parent.claim_key_value,
            )
        else:
            # Pre-claim-group row (or a purge miss): the parent is its own group.
            siblings = [parent]
        canonical_hash = siblings[0].source_hash if siblings else parent.source_hash

        children = movement_split_repository.list_children(db, parent_hash=canonical_hash)
        parent_total = sum((s.amount for s in siblings), Decimal("0"))
        children_total = sum((c.amount for c in children), Decimal("0"))
        payment_amount = max(
            (s.payment_amount for s in siblings if s.payment_amount is not None),
            key=abs,
            default=parent.payment_amount,
        )

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
                "claim_key_type": parent.claim_key_type or None,
                "claim_key_value": parent.claim_key_value or None,
                "parent_emarged": parent.parent_emarged,
            },
            "group": {
                "claim_key_type": parent.claim_key_type or "",
                "claim_key_value": parent.claim_key_value or "",
                "canonical_source_hash": canonical_hash,
                "parents": [
                    {
                        "source_hash": s.source_hash,
                        "movement_type": s.movement_type,
                        "external_ref": s.external_ref,
                        "amount": s.amount,
                        "currency": s.currency,
                        "value_date": s.value_date,
                        "parent_emarged": bool(s.parent_emarged),
                    }
                    for s in siblings
                ],
                # Recomputed from the ghosts that actually exist rather than
                # read off stored totals: a reaped or manually deleted ghost
                # must show up in the delta, not stay hidden.
                "parent_total": parent_total,
                "children_total": children_total,
                "delta": parent_total - children_total,
                "payment_amount": payment_amount,
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
        }


split_service = SplitService()
