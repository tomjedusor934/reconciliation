"""Deep search — resolve any identifier to everything attached to it.

Answers "where does this value live?" for a payment number, a lot uuid, a
reco_id, a movement reference or a lot clustering key, and returns the lots,
movements and payments it reaches — **whether or not the data is already
émargé**.

Two passes:

1. **exact** — equality on indexed columns only (see search_repository);
2. **broad** — ILIKE, ONLY when the exact pass found nothing AND the caller
   asked for it. It is a sequential scan of two multi-million-row tables, so it
   is never implicit and always reports back that it ran.

Between the two, an expansion step walks the identifier graph: the reco_ids and
lot_ids discovered are developed into entries (both tables), lot summaries,
payments and merge chains.
"""
import logging
from collections import defaultdict
from decimal import Decimal
from typing import Any, Dict, List, Set

from sqlalchemy.orm import Session

from app.repositories.movement_lot_repository import movement_lot_repository
from app.repositories.reconciliation_entry_repository import (
    reconciliation_entry_repository,
)
from app.repositories.search_repository import search_repository
from app.schemas.reconciliation import (
    DeepSearchResponse,
    ReconciliationEntryResponse,
    SearchMatch,
    SearchPayment,
    TransversalGroup,
)
from app.services.lot_service import lot_service

logger = logging.getLogger(__name__)

# Caps. A single identifier normally resolves to a handful of rows; a bulk lot
# can hold thousands of payments, hence the higher payment cap.
MAX_ENTRIES = 2000
MAX_PAYMENTS = 1000
MAX_BROAD_ROWS = 200


class SearchService:
    def search(self, db: Session, *, query: str, broad: bool = False) -> DeepSearchResponse:
        value = (query or "").strip()
        if not value:
            return DeepSearchResponse(query="")

        matches: List[SearchMatch] = []
        reco_ids: Set[str] = set()
        lot_ids: Set[str] = set()

        # ── Pass 1: exact resolution ──────────────────────────────────
        for reco_id in search_repository.reco_ids_by_po_id(db, value=value):
            reco_ids.add(reco_id)
            matches.append(
                SearchMatch(source_table="entry_payment_status", field="po_id", value=value)
            )

        for hit in search_repository.lot_ids_by_key_value(db, value=value):
            lot_ids.add(hit["lot_id"])
            matches.append(
                SearchMatch(
                    source_table="movement_lot_key",
                    field=f"key {hit['key_type']}",
                    value=value,
                )
            )

        for reco_id in search_repository.bulk_reco_id_by_po_id(db, value=value):
            reco_ids.add(reco_id)
            matches.append(
                SearchMatch(source_table="payment_bulk_key", field="po_id", value=value)
            )

        if search_repository.lot_exists(db, value=value):
            lot_ids.add(value)
            matches.append(SearchMatch(source_table="movement_lot", field="id", value=value))

        for hit in search_repository.reco_ids_from_entries(db, value=value):
            if hit["reco_id"]:
                reco_ids.add(hit["reco_id"])
            matches.append(
                SearchMatch(
                    source_table=(
                        "reconciliation_entry"
                        if hit["source"] == "live"
                        else "reconciliation_entry_emargement"
                    ),
                    field=hit["field"] or "?",
                    value=value,
                )
            )

        # ── Pass 2: broad fallback, opt-in and only if pass 1 was empty ─
        broad_used = False
        if broad and not matches:
            broad_used = True
            for hit in search_repository.reco_ids_from_entries_broad(
                db, value=value, limit=MAX_BROAD_ROWS
            ):
                if hit["reco_id"]:
                    reco_ids.add(hit["reco_id"])
                matches.append(
                    SearchMatch(
                        source_table=(
                            "reconciliation_entry"
                            if hit["source"] == "live"
                            else "reconciliation_entry_emargement"
                        ),
                        field=hit["field"] or "?",
                        value=value,
                    )
                )
            for hit in search_repository.lot_ids_by_key_value_broad(
                db, value=value, limit=MAX_BROAD_ROWS
            ):
                lot_ids.add(hit["lot_id"])
                matches.append(
                    SearchMatch(
                        source_table="movement_lot_key",
                        field=f"key {hit['key_type']}",
                        value=value,
                    )
                )

        # ── Expansion ─────────────────────────────────────────────────
        # A reco_id that is also a lot id IS that lot (batch-booking flow).
        lot_ids |= movement_lot_repository.lots_exist(db, lot_ids=list(reco_ids))
        # Merges are followed both ways: an absorbed lot keeps the payments and
        # the émargé entries of its members.
        lot_ids = set(search_repository.merge_chain(db, lot_ids=list(lot_ids)))
        # A lot uuid is the reco_id of its entries.
        reco_ids |= lot_ids

        lots = lot_service.get_lot_summaries(db, lot_ids=sorted(lot_ids))

        groups, entries_truncated = self._build_groups(db, reco_ids=reco_ids)
        payments = [
            SearchPayment(
                reco_id=r.reco_id,
                po_id=r.po_id,
                status=r.status,
                amount=r.amount,
                payment_timestamp=r.payment_timestamp,
            )
            for r in search_repository.payments_for_reco_ids(
                db, reco_ids=list(reco_ids), limit=MAX_PAYMENTS
            )
        ]
        truncated = entries_truncated or len(payments) >= MAX_PAYMENTS

        return DeepSearchResponse(
            query=value,
            matches=matches,
            lots=lots,
            groups=groups,
            payments=payments,
            broad_used=broad_used,
            truncated=truncated,
        )

    def _build_groups(self, db: Session, *, reco_ids: Set[str]):
        """Entries of these reco_ids as balanced groups, live AND émargement.

        Same (reco_id, currency) grouping and sum/is_balanced semantics as the
        former transversal view — but computed over both tables, so a
        reconciled group no longer reports a bogus non-zero sum.
        """
        if not reco_ids:
            return [], False

        pairs = reconciliation_entry_repository.list_by_reco_ids(
            db, reco_ids=list(reco_ids)
        )
        truncated = len(pairs) > MAX_ENTRIES
        pairs = pairs[:MAX_ENTRIES]

        grouped: Dict[tuple, list] = defaultdict(list)
        for source, entry in pairs:
            item = ReconciliationEntryResponse.model_validate(entry)
            item.source = source
            grouped[(entry.reco_id, entry.currency)].append(item)

        groups = []
        for (reco_id, currency), items in sorted(
            grouped.items(), key=lambda kv: (kv[0][0] or "", kv[0][1] or "")
        ):
            total = sum((i.amount for i in items), Decimal("0"))
            groups.append(
                TransversalGroup(
                    reco_id=reco_id or "",
                    currency=currency,
                    entries=items,
                    sum=total,
                    is_balanced=(total == Decimal("0")),
                )
            )
        return groups, truncated


search_service = SearchService()
