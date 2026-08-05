"""Movement-lot service (Finacle Batch Booking True).

Owns the lot batch transaction pushed by the ingest_finacle_bb DAG and the
UI-facing reads. The critical contract here is ``member_to_source_hash``: a
member row must hash EXACTLY like its entry does through the finacle push path
(``tasks._to_parsed`` → ``ParsedEntry.compute_finacle_hash``) so members join
``reconciliation_entry`` / ``_emargement`` rows 1:1 — locked by
``tests/test_lot_hash_parity.py``.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.flow import Flow, FlowSource
from app.models.movement_lot import BUCKET_KINDS, KEY_TYPES, LOT_STATUS_MERGED
from app.repositories.movement_lot_repository import movement_lot_repository
from app.services.parsers.base_parser import ParsedEntry

logger = logging.getLogger(__name__)

# Above this many members, GET /lots/{id} stops inlining members/keys and the
# UI must use /graph (aggregate view) + /members (paginated) instead.
LOT_DETAIL_INLINE_CAP = 400


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Naive datetimes are treated as UTC — MUST mirror ``tasks._to_utc`` so
    member hashes match entry hashes byte for byte."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class LotService:
    # ------------------------------------------------------------------
    # Hash parity + derived status (pure helpers)
    # ------------------------------------------------------------------

    @staticmethod
    def member_to_source_hash(
        *,
        flow_id: int,
        external_ref: Optional[str],
        account: Optional[str],
        value_date: datetime,
        operation_date: Optional[datetime] = None,
    ) -> str:
        """Finacle identity of a member — same formula and same naive→UTC
        normalization as the entry push path."""
        value_date = _to_utc(value_date)
        entry = ParsedEntry(
            reco_id=None,
            account=account,
            currency="EUR",
            amount=Decimal("0"),
            value_date=value_date,
            operation_date=_to_utc(operation_date) or value_date,
            external_ref=external_ref,
        )
        return entry.compute_finacle_hash(flow_id)

    @staticmethod
    def derive_lot_status(
        *, lot_status: str, member_count: int, pending_count: int, matched_count: int
    ) -> str:
        if lot_status == LOT_STATUS_MERGED:
            return "merged"
        if member_count > 0 and pending_count == 0 and matched_count > 0:
            return "matched"
        return "pending"

    # ------------------------------------------------------------------
    # DAG-facing writes
    # ------------------------------------------------------------------

    def apply_lot_batch(
        self,
        db: Session,
        *,
        flow: Flow,
        source: FlowSource,
        lots: List[Any],
        members: List[Any],
    ) -> Dict[str, Any]:
        """One atomic transaction: create buckets → upsert members (hash computed
        here) → accrue keys → roll up currency and synthetic_only → commit.

        Everything here must stay proportional to the BATCH, never to the source:
        a full run is hundreds of these calls, so any per-batch step that scans
        what previous batches wrote turns the run quadratic.

        There is no merge step and no cross-lot guard any more: a bucket id is a
        uuid5 of its identity, so two batches naming the same bucket land on the
        same row by construction and nothing can drift between runs."""
        try:
            for lot in lots:
                if lot.bucket_kind not in BUCKET_KINDS:
                    raise ValueError(f"unknown bucket_kind {lot.bucket_kind!r}")
            lots_created = movement_lot_repository.create_lots(
                db,
                flow_id=flow.id,
                flow_source_id=source.id,
                buckets=[lot.model_dump() for lot in lots],
            )

            member_rows: Dict[str, dict] = {}
            key_specs: Dict[str, List[Tuple[str, str]]] = {}
            for member in members:
                for key in member.keys:
                    if key.key_type not in KEY_TYPES:
                        raise ValueError(f"unknown key_type {key.key_type!r}")
                value_date = _to_utc(member.value_date)
                operation_date = _to_utc(member.operation_date) or value_date
                source_hash = self.member_to_source_hash(
                    flow_id=flow.id,
                    external_ref=member.external_ref,
                    account=member.account,
                    value_date=member.value_date,
                    operation_date=member.operation_date,
                )
                # A ghost shares its parent's account and dates — same movement,
                # sliced — so the parent's hash comes out of the same formula
                # with the parent's external_ref. Matches what split_service
                # computed when it registered the parent.
                split_parent_hash = (
                    self.member_to_source_hash(
                        flow_id=flow.id,
                        external_ref=member.split_parent_external_ref,
                        account=member.account,
                        value_date=member.value_date,
                        operation_date=member.operation_date,
                    )
                    if member.split_parent_external_ref
                    else None
                )
                # Overlap re-streams can repeat a movement within one batch —
                # later occurrence wins, mirroring the entry upsert.
                member_rows[source_hash] = {
                    "lot_id": member.lot_id,
                    "source_hash": source_hash,
                    "movement_type": member.movement_type,
                    "external_ref": member.external_ref,
                    "account": member.account,
                    "currency": member.currency,
                    "amount": member.amount,
                    "direction": member.direction,
                    "value_date": value_date,
                    "operation_date": operation_date,
                    "transaction_particulars": member.transaction_particulars,
                    "ref_no": member.ref_no,
                    "remarks_1": member.remarks_1,
                    "split_parent_hash": split_parent_hash,
                    "payment_count": member.payment_count,
                }
                key_specs[source_hash] = [(k.key_type, k.key_value) for k in member.keys]

            target_lot_ids = {row["lot_id"] for row in member_rows.values()}
            stored_currencies = movement_lot_repository.lot_currencies(
                db, lot_ids=list(target_lot_ids)
            )
            missing = target_lot_ids - set(stored_currencies)
            if missing:
                raise ValueError(f"members reference unknown lot(s): {sorted(missing)[:5]}")

            inserted, updated, ids_by_hash = movement_lot_repository.upsert_members(
                db, list(member_rows.values())
            )
            key_rows = [
                {"member_id": ids_by_hash[h], "key_type": kt, "key_value": kv}
                for h, pairs in key_specs.items()
                for kt, kv in pairs
                if h in ids_by_hash
            ]
            keys_added = movement_lot_repository.insert_keys(db, key_rows)
            # The rollup re-derives a lot's currency with a DISTINCT ON over ALL
            # its members, so handing it every lot the batch touches re-scans the
            # source's biggest lot on every single batch — the same quadratic trap
            # as the guard below. A lot whose stored currency already matches
            # every member this batch brings cannot come out of that UPDATE with a
            # different value, so it is skipped; a lot the batch could move (new,
            # or genuinely mixed-currency) is still passed through untouched.
            currency_candidates = {
                row["lot_id"]
                for row in member_rows.values()
                if stored_currencies.get(row["lot_id"]) != row["currency"]
            }
            movement_lot_repository.sync_lot_currencies(
                db, lot_ids=list(currency_candidates)
            )
            # Scoped to the lots this batch touched, for the same reason.
            movement_lot_repository.sync_synthetic_only(
                db, lot_ids=list(target_lot_ids)
            )

            db.commit()
        except IntegrityError as exc:
            db.rollback()
            raise ValueError(f"lot batch violates integrity: {exc.orig}") from exc
        except Exception:
            db.rollback()
            raise

        return {
            "lots_created": lots_created,
            "members_inserted": inserted,
            "members_updated": updated,
            "keys_added": keys_added,
        }

    # ------------------------------------------------------------------
    # UI-facing reads
    # ------------------------------------------------------------------

    def _row_to_summary(self, row: Any) -> Dict[str, Any]:
        member_count = row.member_count or 0
        return {
            "lot_id": row.id,
            "flow_id": row.flow_id,
            "flow_source_id": row.flow_source_id,
            "currency": row.currency,
            "currencies": list(row.currencies or []),
            "status": self.derive_lot_status(
                lot_status=row.lot_status,
                member_count=member_count,
                pending_count=row.pending_count or 0,
                matched_count=row.matched_count or 0,
            ),
            "is_balanced": member_count > 0 and (row.unbalanced_currencies or 0) == 0,
            "member_count": member_count,
            "pending_count": row.pending_count or 0,
            "matched_count": row.matched_count or 0,
            "excluded_count": row.excluded_count or 0,
            "total_debit": row.total_debit,
            "total_credit": row.total_credit,
            "net_amount": row.net_amount,
            "pending_payment_amount": row.pending_payment_amount,
            "pending_payment_count": row.pending_payment_count or 0,
            "first_value_date": row.first_value_date,
            "last_value_date": row.last_value_date,
            "merge_conflict": row.merge_conflict,
            "merged_into_lot_id": row.merged_into_lot_id,
            "bucket_kind": row.bucket_kind,
            "bucket_pacs008": row.bucket_pacs008 or None,
            "bucket_msgid": row.bucket_msgid or None,
            "bucket_po": row.bucket_po or None,
            "bucket_ref": row.bucket_ref or None,
            "synthetic_only": bool(row.synthetic_only),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

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
        skip: int = 0,
        limit: int = 50,
    ) -> Tuple[List[Dict[str, Any]], int]:
        rows, total = movement_lot_repository.list_lots(
            db,
            flow_id=flow_id, status=status, balanced=balanced,
            date_from=date_from, date_to=date_to, search=search,
            bucket_kind=bucket_kind, synthetic_only=synthetic_only,
            payment_gap=payment_gap,
            skip=skip, limit=limit,
        )
        return [self._row_to_summary(r) for r in rows], total

    def get_lot_summaries(self, db: Session, *, lot_ids: Sequence[str]) -> List[Dict[str, Any]]:
        """Summaries of these lots, unknown ids skipped.

        Lighter than ``get_lot_detail`` (no members, no keys) — for callers that
        only need to present the lots, such as the deep search.
        """
        out: List[Dict[str, Any]] = []
        for lot_id in lot_ids:
            row = movement_lot_repository.get_lot_summary(db, lot_id=lot_id)
            if row is not None:
                out.append(self._row_to_summary(row))
        return out

    @staticmethod
    def _member_row_to_dict(m: Any) -> Dict[str, Any]:
        return {
            "id": m.id,
            "source_hash": m.source_hash,
            "movement_type": m.movement_type,
            "external_ref": m.external_ref,
            "account": m.account,
            "currency": m.currency,
            "amount": m.amount,
            "direction": m.direction,
            "value_date": m.value_date,
            "operation_date": m.operation_date,
            "transaction_particulars": m.transaction_particulars,
            "ref_no": m.ref_no,
            "remarks_1": m.remarks_1,
            "split_parent_hash": m.split_parent_hash,
            "split_parent_external_ref": m.split_parent_external_ref,
            "split_parent_amount": m.split_parent_amount,
            "payment_count": m.payment_count,
            "entry_status": m.entry_status.lower() if m.entry_status else None,
            "entry_id": m.entry_id,
            "match_group_id": m.match_group_id,
        }

    def get_lot_detail(
        self, db: Session, *, lot_id: str, inline_cap: int = LOT_DETAIL_INLINE_CAP
    ) -> Optional[Dict[str, Any]]:
        summary_row = movement_lot_repository.get_lot_summary(db, lot_id=lot_id)
        if summary_row is None:
            return None
        summary = self._row_to_summary(summary_row)
        if summary["member_count"] > inline_cap:
            # A giant lot would ship tens of MB and mount one DOM node per
            # movement — the UI switches to /graph + paginated /members.
            return {"lot": summary, "members": [], "keys": [], "members_included": False}
        members = [
            self._member_row_to_dict(m)
            for m in movement_lot_repository.list_members_with_status(db, lot_id=lot_id)
        ]
        keys = [
            {
                "id": k.id,
                "member_id": k.member_id,
                "key_type": k.key_type,
                "key_value": k.key_value,
            }
            for k in movement_lot_repository.list_keys_for_lot(db, lot_id=lot_id)
        ]
        return {"lot": summary, "members": members, "keys": keys, "members_included": True}

    def list_lot_members(
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
    ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        """One page of members; None when the lot itself does not exist."""
        if not movement_lot_repository.lots_exist(db, lot_ids=[lot_id]):
            return None
        rows, total = movement_lot_repository.list_members_page(
            db,
            lot_id=lot_id, skip=skip, limit=limit,
            movement_type=movement_type, direction=direction,
            entry_status=entry_status, search=search,
            key_type=key_type, key_value=key_value,
        )
        return [self._member_row_to_dict(r) for r in rows], total

    def list_lot_key_values(
        self,
        db: Session,
        *,
        lot_id: str,
        key_type: str,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Optional[Tuple[List[Dict[str, Any]], int]]:
        """Paginated distinct values of a key type (drawer list). None when the
        lot does not exist."""
        if not movement_lot_repository.lots_exist(db, lot_ids=[lot_id]):
            return None
        rows, total = movement_lot_repository.list_key_values(
            db, lot_id=lot_id, key_type=key_type, search=search, skip=skip, limit=limit
        )
        return (
            [{"key_value": r.key_value, "member_count": r.member_count} for r in rows],
            total,
        )

    def get_lot_graph(
        self,
        db: Session,
        *,
        lot_id: str,
        key_type: Optional[str] = None,
        key_value: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Mega-aggregate graph of a lot: exactly 3 link nodes (PACS008 / MSGID /
        PO, each carrying its distinct-value count) + one movement node per
        (movement_type, direction) with summed amount. Payload stays tiny (a few
        dozen rows) regardless of lot size. When (key_type, key_value) is given,
        ``groups`` is scoped to the members carrying that exact value — the
        related-nodes view when a value is picked in the drawer."""
        summary_row = movement_lot_repository.get_lot_summary(db, lot_id=lot_id)
        if summary_row is None:
            return None
        scoped = bool(key_type and key_value)
        key_type_rows = movement_lot_repository.summarize_key_types(db, lot_id=lot_id)
        group_rows = movement_lot_repository.aggregate_members_by_type_direction(
            db, lot_id=lot_id, key_type=key_type, key_value=key_value
        )
        # Edges are lot-wide (the skeleton); when scoped, the frontend already
        # filters the visible nodes so the full edge set is still correct.
        edge_rows = movement_lot_repository.list_graph_edges(db, lot_id=lot_id)
        type_rows = movement_lot_repository.count_members_by_type(db, lot_id=lot_id)
        return {
            "lot_id": lot_id,
            "key_types": [
                {
                    "key_type": r.key_type,
                    "distinct_count": r.distinct_count,
                    "member_link_count": r.member_link_count,
                }
                for r in key_type_rows
            ],
            "groups": [
                {
                    "movement_type": r.movement_type,
                    "direction": r.direction,
                    "member_count": r.member_count,
                    "total_amount": r.total_amount,
                    "pending_count": r.pending_count,
                    "matched_count": r.matched_count,
                    "excluded_count": r.excluded_count,
                    "pending_payment_amount": r.pending_payment_amount,
                }
                for r in group_rows
            ],
            "edges": [
                {
                    "movement_type": r.movement_type,
                    "direction": r.direction,
                    "key_type": r.key_type,
                }
                for r in edge_rows
            ],
            "meta": {
                "member_count": summary_row.member_count or 0,
                "type_counts": {t.movement_type: t.member_count for t in type_rows},
                # Lot-wide pending-payment total (for the aggregate "Pending
                # payments" node in the unfocused view).
                "pending_payment_amount": summary_row.pending_payment_amount,
                "pending_payment_count": summary_row.pending_payment_count or 0,
            },
        }


lot_service = LotService()
