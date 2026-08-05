import logging
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple

from sqlalchemy import and_, case, exists, func, literal_column, or_, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Targeted trace (set RECO_TRACE_RECO_ID=BLK2026177009757): logs source_hash dedup
# collisions and conflict re-buckets for the traced reco_id at the finacle upsert —
# where 'more movements, less amount' is most likely created. Unset → inert.
RECO_TRACE_RECO_ID = os.environ.get("RECO_TRACE_RECO_ID", "").strip()

from app.models.flow import FlowSource, FlowSourceType
from app.models.ingestion_run import IngestionRun
from app.models.payment_status import EntryPaymentStatus
from app.models.reconciliation_entry import (
    UNRESOLVED_RECO_ID,
    EntryStatus,
    ReconciliationEntry,
    ReconciliationEntryEmargement,
)


class ReconciliationEntryRepository:
    _IN_CHUNK = 10000  # max values per IN () list — a bulk can hold thousands of PO ids

    def bulk_insert(
        self,
        db: Session,
        rows: Sequence[dict],
        *,
        on_conflict_skip: bool = True,
    ) -> Tuple[int, int]:
        """Bulk insert with dedup on source_hash. Returns (inserted, skipped)."""
        if not rows:
            return 0, 0

        skipped = 0
        if on_conflict_skip:
            incoming_hashes = [r["source_hash"] for r in rows]
            # Check duplicates in both live and émargement tables
            existing_live: set[str] = {
                h
                for (h,) in db.query(ReconciliationEntry.source_hash)
                .filter(ReconciliationEntry.source_hash.in_(incoming_hashes))
                .all()
            }
            existing_emargement: set[str] = {
                h
                for (h,) in db.query(ReconciliationEntryEmargement.source_hash)
                .filter(ReconciliationEntryEmargement.source_hash.in_(incoming_hashes))
                .all()
            }
            existing_hashes = existing_live | existing_emargement
            if existing_hashes:
                rows = [r for r in rows if r["source_hash"] not in existing_hashes]
                skipped = len(incoming_hashes) - len(rows)

        if not rows:
            return 0, skipped

        # Collapse rows sharing a source_hash within this batch (a byte-identical
        # duplicate line). A single multi-row INSERT cannot carry the same unique
        # key twice; keep the first occurrence.
        if len({r["source_hash"] for r in rows}) != len(rows):
            seen: Dict[str, dict] = {}
            for r in rows:
                seen.setdefault(r["source_hash"], r)
            dropped = len(rows) - len(seen)
            logger.warning("bulk_insert: dropped %d in-batch duplicate source_hash row(s)", dropped)
            skipped += dropped
            rows = list(seen.values())

        # on_conflict_do_nothing guards against a race / any hash that slipped the
        # pre-check so ingestion never 500s on a duplicate.
        stmt = pg_insert(ReconciliationEntry.__table__).values(rows).on_conflict_do_nothing(
            index_elements=["source_hash"],
        )
        result = db.execute(stmt)
        db.commit()
        inserted = result.rowcount or 0
        skipped += len(rows) - inserted  # any row swallowed by ON CONFLICT
        return inserted, skipped

    def upsert_finacle(self, db: Session, rows: Sequence[dict]) -> Tuple[int, int, int]:
        """Insert new finacle movements; for movements already live & PENDING,
        update the (re)computed reco_id + movement context in place.

        Entries already moved to the émargement table (matched/forced/excluded)
        are immutable → skipped. Returns ``(inserted, updated, skipped)``.
        Relies on the finacle source_hash being reco_id-independent so that the
        same movement maps to a stable row across runs.

        A real movement's ``amount`` is part of its identity and is never
        updated. A batch-booking GHOST's is not: it is the sum of its bucket's
        ``std.Payment`` amounts, which grows as the datamart fills in, so
        ``amount``/``direction`` are refreshed for rows carrying a
        ``split_parent_hash`` and left frozen for everything else.
        """
        if not rows:
            return 0, 0, 0

        incoming_hashes = [r["source_hash"] for r in rows]
        existing_emargement: set[str] = {
            h
            for (h,) in db.query(ReconciliationEntryEmargement.source_hash)
            .filter(ReconciliationEntryEmargement.source_hash.in_(incoming_hashes))
            .all()
        }
        skipped = 0
        if existing_emargement:
            rows = [r for r in rows if r["source_hash"] not in existing_emargement]
            skipped = len(incoming_hashes) - len(rows)
        if not rows:
            return 0, 0, skipped

        if RECO_TRACE_RECO_ID:
            self._trace_dedup(rows)

        # A single INSERT ... ON CONFLICT DO UPDATE cannot affect the same conflict
        # target twice (Postgres CardinalityViolation). Collapse rows that share a
        # source_hash within this batch — they are the same movement (e.g. duplicate
        # SCD2 "current" rows) — keeping the last occurrence.
        if len({r["source_hash"] for r in rows}) != len(rows):
            deduped = {r["source_hash"]: r for r in rows}  # later wins
            skipped += len(rows) - len(deduped)
            rows = list(deduped.values())

        if RECO_TRACE_RECO_ID:
            self._trace_conflicts(db, rows)

        stmt = pg_insert(ReconciliationEntry.__table__).values(rows)
        is_ghost = stmt.excluded.split_parent_hash.isnot(None)
        stmt = stmt.on_conflict_do_update(
            index_elements=["source_hash"],
            set_={
                "reco_id": stmt.excluded.reco_id,
                "transaction_particulars": stmt.excluded.transaction_particulars,
                "ref_no": stmt.excluded.ref_no,
                "remarks_1": stmt.excluded.remarks_1,
                "transaction_id": stmt.excluded.transaction_id,
                "payload_raw": stmt.excluded.payload_raw,
                "ingestion_run_id": stmt.excluded.ingestion_run_id,
                "split_parent_hash": stmt.excluded.split_parent_hash,
                "amount": case(
                    (is_ghost, stmt.excluded.amount),
                    else_=ReconciliationEntry.__table__.c.amount,
                ),
                "direction": case(
                    (is_ghost, stmt.excluded.direction),
                    else_=ReconciliationEntry.__table__.c.direction,
                ),
            },
            # Never touch a non-pending row (excluded entries linger in the live
            # table until swept; matched/forced have already left it).
            where=ReconciliationEntry.__table__.c.status == EntryStatus.PENDING,
        ).returning(literal_column("(xmax = 0)"))
        # xmax = 0 → freshly inserted; non-zero → updated existing row.
        flags = [bool(r[0]) for r in db.execute(stmt).fetchall()]
        db.commit()
        inserted = sum(1 for f in flags if f)
        updated = sum(1 for f in flags if not f)
        if RECO_TRACE_RECO_ID:
            n_target = sum(1 for r in rows if r.get("reco_id") == RECO_TRACE_RECO_ID)
            logger.info(
                "[reco_trace][upsert] target_rows=%d batch_rows=%d inserted=%d updated=%d skipped=%d",
                n_target, len(rows), inserted, updated, skipped,
            )
        return inserted, updated, skipped

    @staticmethod
    def _trace_dedup(rows: Sequence[dict]) -> None:
        """Log source_hash collisions within the batch that involve the traced reco_id:
        these collapse to one row ('later wins'), silently dropping the others' amounts."""
        by_hash: dict[str, list[dict]] = {}
        for r in rows:
            by_hash.setdefault(r["source_hash"], []).append(r)
        for h, group in by_hash.items():
            if len(group) > 1 and any(r.get("reco_id") == RECO_TRACE_RECO_ID for r in group):
                logger.info(
                    "[reco_trace][dedup] source_hash=%s n_rows=%d amounts=%s reco_ids=%s kept_amount=%s",
                    h, len(group), [str(r.get("amount")) for r in group],
                    [r.get("reco_id") for r in group], str(group[-1].get("amount")),
                )

    @staticmethod
    def _trace_conflicts(db: Session, rows: Sequence[dict]) -> None:
        """For the traced reco_id, compare incoming rows against the existing live rows
        on the same source_hash: reveals movements re-bucketed into the reco_id while
        keeping a frozen amount (old_reco_id/old_amount != new), and PENDING-gated skips."""
        target_rows = {
            r["source_hash"]: r for r in rows if r.get("reco_id") == RECO_TRACE_RECO_ID
        }
        if not target_rows:
            return
        existing = (
            db.query(
                ReconciliationEntry.source_hash,
                ReconciliationEntry.reco_id,
                ReconciliationEntry.amount,
                ReconciliationEntry.status,
            )
            .filter(ReconciliationEntry.source_hash.in_(list(target_rows)))
            .all()
        )
        for src_hash, old_reco, old_amount, old_status in existing:
            new = target_rows[src_hash]
            logger.info(
                "[reco_trace][conflict] source_hash=%s old_reco_id=%s old_amount=%s old_status=%s "
                "-> new_reco_id=%s new_amount=%s%s",
                src_hash, old_reco, old_amount, old_status,
                new.get("reco_id"), new.get("amount"),
                "" if str(old_status) == str(EntryStatus.PENDING)
                else " [SKIPPED: not PENDING]",
            )

    def list_unresolved_finacle(
        self, db: Session, *, flow_source_id: int, limit: int = 50000
    ) -> List[ReconciliationEntry]:
        """Live PENDING finacle entries whose reco_id is unresolved (NULL or the
        'Not Supported' sentinel), for the DAG to re-enrich on the next run."""
        return (
            db.query(ReconciliationEntry)
            .join(IngestionRun, ReconciliationEntry.ingestion_run_id == IngestionRun.id)
            .filter(
                IngestionRun.flow_source_id == flow_source_id,
                ReconciliationEntry.status == EntryStatus.PENDING,
                or_(
                    ReconciliationEntry.reco_id.is_(None),
                    ReconciliationEntry.reco_id == UNRESOLVED_RECO_ID,
                ),
            )
            .order_by(ReconciliationEntry.id.asc())
            .limit(limit)
            .all()
        )

    # ---------- MT940 (DAG-extracted) unresolved retry ----------
    def list_unresolved_mt940_keys(
        self, db: Session, *, flow_source_id: int, limit: int = 50000
    ) -> List[str]:
        """Distinct remarks_1 keys of the source's live PENDING entries with an
        unresolved reco_id — the DAG re-runs the std.Payment TransactionRef join
        on them and posts back the resolved {key: reco_id} pairs."""
        rows = (
            db.query(ReconciliationEntry.remarks_1)
            .join(IngestionRun, ReconciliationEntry.ingestion_run_id == IngestionRun.id)
            .filter(
                IngestionRun.flow_source_id == flow_source_id,
                ReconciliationEntry.status == EntryStatus.PENDING,
                or_(
                    ReconciliationEntry.reco_id.is_(None),
                    ReconciliationEntry.reco_id == UNRESOLVED_RECO_ID,
                ),
                ReconciliationEntry.remarks_1.isnot(None),
            )
            .distinct()
            .limit(limit)
            .all()
        )
        return [r[0] for r in rows]

    def resolve_mt940_keys(
        self, db: Session, *, flow_source_id: int, resolved: Dict[str, str]
    ) -> int:
        """Fill reco_id in place on the source's PENDING entries whose remarks_1
        key the DAG resolved against std.Payment. No re-push/upsert: the MT940
        source_hash includes the amount, whose representation may change through
        a DB round-trip, so re-pushed rows could silently duplicate. Idempotent
        (only rows still unresolved are touched); returns the updated count."""
        pairs = {k: v for k, v in (resolved or {}).items() if k and v}
        if not pairs:
            return 0
        run_ids = (
            db.query(IngestionRun.id)
            .filter(IngestionRun.flow_source_id == flow_source_id)
            .scalar_subquery()
        )
        updated = 0
        for key, reco_id in pairs.items():
            updated += (
                db.query(ReconciliationEntry)
                .filter(
                    ReconciliationEntry.ingestion_run_id.in_(run_ids),
                    ReconciliationEntry.status == EntryStatus.PENDING,
                    or_(
                        ReconciliationEntry.reco_id.is_(None),
                        ReconciliationEntry.reco_id == UNRESOLVED_RECO_ID,
                    ),
                    ReconciliationEntry.remarks_1 == key,
                )
                .update({"reco_id": reco_id}, synchronize_session=False)
            )
        if updated:
            db.commit()
        return updated

    # ---------- Bulk-booked instant payments: PO id -> bulk key ----------
    def regroup_bulk_pos(
        self, db: Session, *, flow_id: int, mapping: Dict[str, str]
    ) -> int:
        """Re-key the flow's live PENDING entries currently stored under a PO id that
        turned out to be one member of a BULK booking, onto the bulk's reco_id
        (std.Payment.MessageID).

        Flow-scoped and NOT source-scoped on purpose: the file leg keys on the very
        same PaymentNumber (see mt940_extract.resolve_mt940_reco_ids), so re-keying
        only the finacle side would break the match instead of repairing it.

        One UPDATE per bulk rather than per PO id — a bulk can carry thousands of
        members — with the IN list chunked. Idempotent: ``reco_id != target`` means a
        second call touches nothing. Never touches a non-PENDING row (émargement
        entries are immutable, and their group is already settled).
        """
        by_reco: Dict[str, List[str]] = {}
        for po_id, reco_id in (mapping or {}).items():
            if po_id and reco_id and po_id != reco_id:
                by_reco.setdefault(reco_id, []).append(po_id)
        if not by_reco:
            return 0

        updated = 0
        for reco_id, po_ids in by_reco.items():
            for i in range(0, len(po_ids), self._IN_CHUNK):
                updated += (
                    db.query(ReconciliationEntry)
                    .filter(
                        ReconciliationEntry.flow_id == flow_id,
                        ReconciliationEntry.status == EntryStatus.PENDING,
                        ReconciliationEntry.reco_id.in_(po_ids[i:i + self._IN_CHUNK]),
                        ReconciliationEntry.reco_id != reco_id,
                    )
                    .update({"reco_id": reco_id}, synchronize_session=False)
                )
        if updated:
            db.commit()
            logger.info(
                "regroup_bulk_pos: flow_id=%s %d bulk(s), %d PO id(s) -> %d entries re-keyed",
                flow_id, len(by_reco), sum(len(v) for v in by_reco.values()), updated,
            )
        return updated

    # ---------- File-source reco_id resolution against finacle entries ----------
    @staticmethod
    def reference_keys(ref_no: Optional[str], prefix: Optional[str]) -> List[str]:
        """Lookup keys for an MT940 reference: the raw value plus, when it starts
        with the configured prefix (e.g. 'SCRT'), the stripped value too."""
        rn = (ref_no or "").strip()
        if not rn:
            return []
        keys = [rn]
        if prefix and rn.startswith(prefix) and rn != prefix:
            keys.append(rn[len(prefix):])
        return keys

    def find_finacle_reco_id(
        self, db: Session, *, flow_id: int, keys: List[str]
    ) -> Optional[str]:
        """reco_id of a finacle-source entry in this flow whose reco_id / ref_no /
        remarks_1 matches one of ``keys`` (and is actually resolved). Looks in both
        the live and émargement tables. Scoped to finacle_db sources so an MT940 key
        can only borrow a finacle reco_id (never another file row)."""
        keys = [k for k in keys if k]
        if not keys:
            return None
        for model in (ReconciliationEntry, ReconciliationEntryEmargement):
            row = (
                db.query(model.reco_id)
                .join(IngestionRun, model.ingestion_run_id == IngestionRun.id)
                .join(FlowSource, IngestionRun.flow_source_id == FlowSource.id)
                .filter(
                    model.flow_id == flow_id,
                    FlowSource.source_type == FlowSourceType.FINACLE_DB,
                    model.reco_id.isnot(None),
                    model.reco_id != UNRESOLVED_RECO_ID,
                    or_(
                        model.reco_id.in_(keys),
                        model.ref_no.in_(keys),
                        model.remarks_1.in_(keys),
                    ),
                )
                .first()
            )
            if row and row[0]:
                return row[0]
        return None

    def list_unresolved_reference_entries(self, db: Session, *, flow_id: Optional[int] = None):
        """Live PENDING entries from file sources that opt into finacle reco_id
        lookup (``parser_config.resolve_reco_id_via_finacle = true``) which carry a
        reference key (ref_no) but no reco_id yet. Returns (entry, source) pairs.
        Optionally scoped to a single ``flow_id``."""
        q = (
            db.query(ReconciliationEntry, FlowSource)
            .join(IngestionRun, ReconciliationEntry.ingestion_run_id == IngestionRun.id)
            .join(FlowSource, IngestionRun.flow_source_id == FlowSource.id)
            .filter(
                ReconciliationEntry.status == EntryStatus.PENDING,
                ReconciliationEntry.reco_id.is_(None),
                ReconciliationEntry.ref_no.isnot(None),
                FlowSource.parser_config["resolve_reco_id_via_finacle"].astext == "true",
            )
        )
        if flow_id is not None:
            q = q.filter(ReconciliationEntry.flow_id == flow_id)
        return q.all()

    def resolve_flow_references(self, db: Session, *, flow_id: Optional[int] = None) -> int:
        """Sweep live PENDING entries from lookup-enabled file sources (e.g. MT940
        ``+21`` keys) that carry a reference key (ref_no) but no reco_id, and fill
        reco_id from a matching finacle entry of the same flow. Optionally scoped to
        ``flow_id``. Idempotent; commits once if anything changed; returns the count.

        Reused both as the per-flow step run at the start of file ingestion and as the
        global safety-net sweep before matching."""
        pairs = self.list_unresolved_reference_entries(db, flow_id=flow_id)
        updated = 0
        for entry, source in pairs:
            prefix = (source.parser_config or {}).get("reco_id_prefix")
            keys = self.reference_keys(entry.ref_no, prefix)
            reco_id = self.find_finacle_reco_id(db, flow_id=entry.flow_id, keys=keys)
            if reco_id:
                entry.reco_id = reco_id
                updated += 1
        if updated:
            db.commit()
        return updated

    def get_one(
        self, db: Session, *, entry_id: int, value_date: Optional[datetime] = None
    ) -> Optional[ReconciliationEntry]:
        q = db.query(ReconciliationEntry).filter(ReconciliationEntry.id == entry_id)
        if value_date is not None:
            q = q.filter(ReconciliationEntry.value_date == value_date)
        return q.first()

    def _apply_entry_filters(
        self,
        q,
        model,
        *,
        flow_id=None,
        status=None,
        reco_id=None,
        amount_min=None,
        amount_max=None,
        payment_statuses=None,
        payment_timestamp_from=None,
        payment_timestamp_to=None,
        account=None,
        date_from=None,
        date_to=None,
        search=None,
    ):
        """Shared WHERE builder for the operational list + count (same clauses,
        one place so they can't drift)."""
        if flow_id is not None:
            q = q.filter(model.flow_id == flow_id)
        if status is not None:
            q = q.filter(model.status == status)
        if reco_id:
            q = q.filter(model.reco_id == reco_id)
        # Amount filter compares the ABSOLUTE value (credit +500 and debit -500
        # both match [100, 1000]).
        if amount_min is not None:
            q = q.filter(func.abs(model.amount) >= amount_min)
        if amount_max is not None:
            q = q.filter(func.abs(model.amount) <= amount_max)
        if payment_statuses:
            # OR semantics: keep the row if it carries at least one of the
            # selected statuses (a bulk movement has several). '?' = NULL status.
            real = [s for s in payment_statuses if s != "?"]
            conds = []
            if real:
                conds.append(EntryPaymentStatus.status.in_(real))
            if "?" in payment_statuses:
                conds.append(EntryPaymentStatus.status.is_(None))
            if conds:
                q = q.filter(
                    exists().where(
                        and_(EntryPaymentStatus.reco_id == model.reco_id, or_(*conds))
                    )
                )
        if payment_timestamp_from or payment_timestamp_to:
            # Same semantics as payment_statuses: keep the row if AT LEAST ONE of
            # the group's payments falls in the range. Correlated EXISTS (never a
            # JOIN) — this builder also runs against the émargement table and
            # against a COUNT(), which a join would fan out.
            # Both bounds inclusive, compared as-is: the column is naive and so
            # are the bounds, so an hour bound means that exact wall-clock hour.
            conds = [EntryPaymentStatus.reco_id == model.reco_id]
            if payment_timestamp_from:
                conds.append(EntryPaymentStatus.payment_timestamp >= payment_timestamp_from)
            if payment_timestamp_to:
                conds.append(EntryPaymentStatus.payment_timestamp <= payment_timestamp_to)
            q = q.filter(exists().where(and_(*conds)))
        if account:
            q = q.filter(model.account == account)
        if date_from:
            q = q.filter(model.value_date >= date_from)
        if date_to:
            # date_to is a date; make the upper bound inclusive of the
            # whole day by comparing against the start of the next day.
            upper = date_to + timedelta(days=1) if isinstance(date_to, date) and not isinstance(date_to, datetime) else date_to
            q = q.filter(model.value_date < upper)
        if search:
            ilike = f"%{search}%"
            q = q.filter(
                or_(
                    model.external_ref.ilike(ilike),
                    model.reco_id.ilike(ilike),
                    model.file_name.ilike(ilike),
                )
            )
        return q

    def _entry_models_for_status(self, status: Optional[EntryStatus]) -> List:
        """PENDING lives in the live table; MATCHED/FORCED/EXCLUDED in émargement;
        no status filter → both tables merged."""
        emargement_statuses = {EntryStatus.MATCHED, EntryStatus.FORCED, EntryStatus.EXCLUDED}
        if status == EntryStatus.PENDING:
            return [ReconciliationEntry]
        if status in emargement_statuses:
            return [ReconciliationEntryEmargement]
        return [ReconciliationEntry, ReconciliationEntryEmargement]

    def list_filtered(
        self,
        db: Session,
        *,
        flow_id: Optional[int] = None,
        status: Optional[EntryStatus] = None,
        reco_id: Optional[str] = None,
        amount_min: Optional[Decimal] = None,
        amount_max: Optional[Decimal] = None,
        payment_statuses: Optional[Sequence[str]] = None,
        payment_timestamp_from: Optional[datetime] = None,
        payment_timestamp_to: Optional[datetime] = None,
        account: Optional[str] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        search: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[ReconciliationEntry]:
        models = self._entry_models_for_status(status)
        kw = dict(
            flow_id=flow_id, status=status, reco_id=reco_id,
            amount_min=amount_min, amount_max=amount_max,
            payment_statuses=payment_statuses,
            payment_timestamp_from=payment_timestamp_from,
            payment_timestamp_to=payment_timestamp_to,
            account=account,
            date_from=date_from, date_to=date_to, search=search,
        )

        if len(models) == 1:
            model = models[0]
            q = self._apply_entry_filters(db.query(model), model, **kw)
            return (
                q.order_by(model.value_date.desc(), model.id.desc())
                .offset(skip)
                .limit(limit)
                .all()
            )

        # Both tables — fetch from each, merge, sort, paginate in Python.
        # Use a higher fetch limit per table to ensure correct ordering across both.
        fetch_limit = skip + limit
        rows = []
        for model in models:
            q = self._apply_entry_filters(db.query(model), model, **kw)
            rows += q.order_by(model.value_date.desc(), model.id.desc()).limit(fetch_limit).all()
        rows.sort(key=lambda e: (e.value_date, e.id), reverse=True)
        return rows[skip: skip + limit]

    def count_filtered(
        self,
        db: Session,
        *,
        flow_id: Optional[int] = None,
        status: Optional[EntryStatus] = None,
        reco_id: Optional[str] = None,
        amount_min: Optional[Decimal] = None,
        amount_max: Optional[Decimal] = None,
        payment_statuses: Optional[Sequence[str]] = None,
        payment_timestamp_from: Optional[datetime] = None,
        payment_timestamp_to: Optional[datetime] = None,
        account: Optional[str] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        search: Optional[str] = None,
    ) -> int:
        """Count total entries matching filters (for pagination)."""
        models = self._entry_models_for_status(status)
        kw = dict(
            flow_id=flow_id, status=status, reco_id=reco_id,
            amount_min=amount_min, amount_max=amount_max,
            payment_statuses=payment_statuses,
            payment_timestamp_from=payment_timestamp_from,
            payment_timestamp_to=payment_timestamp_to,
            account=account,
            date_from=date_from, date_to=date_to, search=search,
        )
        total = 0
        for model in models:
            q = self._apply_entry_filters(db.query(func.count(model.id)), model, **kw)
            total += q.scalar() or 0
        return total

    def find_balanced_groups(
        self, db: Session
    ) -> List[Tuple[int, str, str, Decimal]]:
        """Find pending groups that sum to 0.

        Group key = (flow_id, reco_id, currency). Returns list of tuples.
        """
        rows = (
            db.query(
                ReconciliationEntry.flow_id,
                ReconciliationEntry.reco_id,
                ReconciliationEntry.currency,
                func.sum(ReconciliationEntry.amount).label("total"),
            )
            .filter(ReconciliationEntry.status == EntryStatus.PENDING)
            .filter(ReconciliationEntry.reco_id.isnot(None))
            # Unresolved finacle movements share the 'Not Supported' sentinel — never
            # let them group/net together.
            .filter(ReconciliationEntry.reco_id != UNRESOLVED_RECO_ID)
            .group_by(
                ReconciliationEntry.flow_id,
                ReconciliationEntry.reco_id,
                ReconciliationEntry.currency,
            )
            .having(func.sum(ReconciliationEntry.amount) == 0)
            .all()
        )
        return [(r.flow_id, r.reco_id, r.currency, r.total) for r in rows]

    def diagnose_pending_groups(
        self, db: Session
    ) -> List[Tuple[int, Optional[str], str, int, Decimal]]:
        """Diagnostic sibling of find_balanced_groups WITHOUT the ``HAVING SUM=0``
        filter: per (flow_id, reco_id, currency) PENDING group, returns
        (flow_id, reco_id, currency, count, sum). Lets a debug pass see which reco_ids
        fail to net to 0 (e.g. bulk) and by how much. Same scope as the matcher."""
        rows = (
            db.query(
                ReconciliationEntry.flow_id,
                ReconciliationEntry.reco_id,
                ReconciliationEntry.currency,
                func.count(ReconciliationEntry.id).label("n"),
                func.sum(ReconciliationEntry.amount).label("total"),
            )
            .filter(ReconciliationEntry.status == EntryStatus.PENDING)
            .filter(ReconciliationEntry.reco_id.isnot(None))
            .filter(ReconciliationEntry.reco_id != UNRESOLVED_RECO_ID)
            .group_by(
                ReconciliationEntry.flow_id,
                ReconciliationEntry.reco_id,
                ReconciliationEntry.currency,
            )
            .all()
        )
        return [(r.flow_id, r.reco_id, r.currency, r.n, r.total) for r in rows]

    def mark_matched(
        self,
        db: Session,
        *,
        flow_id: int,
        reco_id: str,
        currency: str,
        match_group_id: int,
    ) -> int:
        now = datetime.now(timezone.utc)
        result = (
            db.query(ReconciliationEntry)
            .filter(
                ReconciliationEntry.flow_id == flow_id,
                ReconciliationEntry.reco_id == reco_id,
                ReconciliationEntry.currency == currency,
                ReconciliationEntry.status == EntryStatus.PENDING,
            )
            .update(
                {
                    "status": EntryStatus.MATCHED,
                    "match_group_id": match_group_id,
                    "matched_at": now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return result

    def mark_forced(
        self,
        db: Session,
        *,
        entry_ids: List[int],
        match_group_id: int,
    ) -> int:
        now = datetime.now(timezone.utc)
        count = (
            db.query(ReconciliationEntry)
            .filter(
                ReconciliationEntry.id.in_(entry_ids),
                ReconciliationEntry.status == EntryStatus.PENDING,
            )
            .update(
                {
                    "status": EntryStatus.FORCED,
                    "match_group_id": match_group_id,
                    "matched_at": now,
                },
                synchronize_session=False,
            )
        )
        db.commit()
        return count

    def mark_excluded(
        self, db: Session, *, entry_id: int
    ) -> int:
        result = (
            db.query(ReconciliationEntry)
            .filter(ReconciliationEntry.id == entry_id)
            .update({"status": EntryStatus.EXCLUDED}, synchronize_session=False)
        )
        db.commit()
        return result

    def list_by_reco_ids(
        self, db: Session, *, reco_ids: Sequence[str]
    ) -> List[Tuple[str, object]]:
        """``(source, entry)`` for these reco_ids, across BOTH entry tables.

        Reading the live table alone would return nothing for an already
        reconciled group: ``move_matched_to_emargement`` DELETEs those rows from
        the live table. ``source`` ('live' / 'emargement') is carried out because
        the two tables can hold the same ``id`` — it is copied on the move, and
        no cross-table constraint enforces uniqueness.
        """
        if not reco_ids:
            return []
        ids = list(reco_ids)
        out: List[Tuple[str, object]] = []
        for source, model in (
            ("live", ReconciliationEntry),
            ("emargement", ReconciliationEntryEmargement),
        ):
            rows = (
                db.query(model)
                .filter(model.reco_id.in_(ids))
                .order_by(model.value_date.asc())
                .all()
            )
            out.extend((source, row) for row in rows)
        out.sort(key=lambda pair: (pair[1].value_date, pair[1].id))
        return out

    # ---------- KPIs / dashboard ----------
    def kpis_by_flow(self, db: Session) -> List[dict]:
        """Aggregate counts from both live and émargement tables."""
        rows = db.execute(
            text(
                """
                SELECT flow_id,
                       status,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(amount), 0) AS amount_total,
                       COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) AS amount_credit,
                       COALESCE(SUM(amount) FILTER (WHERE amount < 0), 0) AS amount_debit
                FROM (
                    SELECT flow_id, status::text AS status, amount
                    FROM reco.reconciliation_entry
                    UNION ALL
                    SELECT flow_id, status::text AS status, amount
                    FROM reco.reconciliation_entry_emargement
                ) combined
                GROUP BY flow_id, status
                """
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    # ---------- Émargement ----------
    def move_to_emargement(
        self, db: Session, *, entry_ids: List[int]
    ) -> int:
        """Move non-pending entries from the live table to the émargement table."""
        if not entry_ids:
            return 0
        now = datetime.now(timezone.utc)
        # Use CTE: DELETE … RETURNING → INSERT into émargement
        placeholders = ", ".join(str(int(eid)) for eid in entry_ids)
        sql = text(f"""
            WITH moved AS (
                DELETE FROM reco.reconciliation_entry
                WHERE id IN ({placeholders})
                RETURNING *
            )
            INSERT INTO reco.reconciliation_entry_emargement (
                id, flow_id, ingestion_run_id, reco_id, account, currency, amount,
                direction, value_date, operation_date, event_type, external_ref,
                file_name, transaction_particulars, ref_no, remarks_1, transaction_id,
                payload_raw, source_hash, split_parent_hash, status, match_group_id,
                matched_at, emarged_at, created_at, updated_at
            )
            SELECT id, flow_id, ingestion_run_id, reco_id, account, currency, amount,
                   direction, value_date, operation_date, event_type, external_ref,
                   file_name, transaction_particulars, ref_no, remarks_1, transaction_id,
                   payload_raw, source_hash, split_parent_hash, status, match_group_id,
                   matched_at, :now, created_at, updated_at
            FROM moved
            ON CONFLICT (source_hash) DO NOTHING
        """)
        result = db.execute(sql, {"now": now})
        moved = result.rowcount or 0
        db.commit()
        return moved

    def move_matched_to_emargement(
        self,
        db: Session,
        *,
        flow_id: int,
        reco_id: str,
        currency: str,
    ) -> int:
        """Move all matched/forced entries for a given group key to émargement."""
        now = datetime.now(timezone.utc)
        sql = text("""
            WITH moved AS (
                DELETE FROM reco.reconciliation_entry
                WHERE flow_id = :flow_id
                  AND reco_id = :reco_id
                  AND currency = :currency
                  AND status IN ('MATCHED', 'FORCED')
                RETURNING *
            )
            INSERT INTO reco.reconciliation_entry_emargement (
                id, flow_id, ingestion_run_id, reco_id, account, currency, amount,
                direction, value_date, operation_date, event_type, external_ref,
                file_name, transaction_particulars, ref_no, remarks_1, transaction_id,
                payload_raw, source_hash, split_parent_hash, status, match_group_id,
                matched_at, emarged_at, created_at, updated_at
            )
            SELECT id, flow_id, ingestion_run_id, reco_id, account, currency, amount,
                   direction, value_date, operation_date, event_type, external_ref,
                   file_name, transaction_particulars, ref_no, remarks_1, transaction_id,
                   payload_raw, source_hash, split_parent_hash, status, match_group_id,
                   matched_at, :now, created_at, updated_at
            FROM moved
            ON CONFLICT (source_hash) DO NOTHING
        """)
        result = db.execute(sql, {
            "flow_id": flow_id,
            "reco_id": reco_id,
            "currency": currency,
            "now": now,
        })
        moved = result.rowcount or 0
        db.commit()
        return moved

    def move_from_emargement_to_live(
        self, db: Session, *, entry_id: int, commit: bool = True
    ) -> int:
        """Move a single entry back from émargement to the live table (unexclude).

        Bare ``'PENDING'`` literal is coerced to the column's enum type — do NOT
        schema-qualify the enum (the type lives in the default schema, not ``reco``).
        """
        sql = text("""
            WITH moved AS (
                DELETE FROM reco.reconciliation_entry_emargement
                WHERE id = :entry_id
                RETURNING *
            )
            INSERT INTO reco.reconciliation_entry (
                id, flow_id, ingestion_run_id, reco_id, account, currency, amount,
                direction, value_date, operation_date, event_type, external_ref,
                file_name, transaction_particulars, ref_no, remarks_1, transaction_id,
                payload_raw, source_hash, split_parent_hash, status, match_group_id,
                matched_at, created_at, updated_at
            )
            SELECT id, flow_id, ingestion_run_id, reco_id, account, currency, amount,
                   direction, value_date, operation_date, event_type, external_ref,
                   file_name, transaction_particulars, ref_no, remarks_1, transaction_id,
                   payload_raw, source_hash, split_parent_hash, 'PENDING',
                   NULL, NULL,
                   created_at, :now
            FROM moved
            ON CONFLICT (source_hash) DO NOTHING
        """)
        now = datetime.now(timezone.utc)
        result = db.execute(sql, {"entry_id": entry_id, "now": now})
        moved = result.rowcount or 0
        if commit:
            db.commit()
        return moved

    def get_emargement_entry(
        self, db: Session, *, entry_id: int
    ) -> Optional[ReconciliationEntryEmargement]:
        """Get a single entry from the émargement table."""
        return (
            db.query(ReconciliationEntryEmargement)
            .filter(ReconciliationEntryEmargement.id == entry_id)
            .first()
        )

    # ---------- Dashboard helpers ----------
    def oldest_pending_per_flow(self, db: Session) -> dict:
        """Return {flow_id: oldest_value_date} for pending entries."""
        rows = db.execute(
            text(
                """
                SELECT flow_id, MIN(value_date) AS oldest
                FROM reco.reconciliation_entry
                WHERE status = 'PENDING'
                GROUP BY flow_id
                """
            )
        ).all()
        return {r.flow_id: r.oldest for r in rows}

    def kpis_by_flow_at_run(self, db: Session, *, run_id: int) -> List[dict]:
        """KPIs scoped to a specific reconciliation run.

        matched_count = entries matched by runs <= run_id
        pending_count = current PENDING + entries matched by runs > run_id (not yet matched at that time)
        excluded_count = always current (manual exclusions aren't run-scoped)
        """
        # Matched / forced up to (and including) this run
        matched_rows = db.execute(
            text(
                """
                SELECT e.flow_id,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(e.amount), 0) AS amount_total,
                       'MATCHED' AS status
                FROM reco.reconciliation_entry_emargement e
                JOIN reco.match_group mg ON e.match_group_id = mg.id
                WHERE mg.reconciliation_run_id <= :run_id
                  AND e.status IN ('MATCHED', 'FORCED')
                GROUP BY e.flow_id
                """
            ),
            {"run_id": run_id},
        ).all()

        # Pending as of this run = currently PENDING + emargement matched AFTER this run
        pending_rows = db.execute(
            text(
                """
                SELECT flow_id,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(amount), 0) AS amount_total,
                       'PENDING' AS status
                FROM (
                    SELECT flow_id, amount
                    FROM reco.reconciliation_entry
                    WHERE status = 'PENDING'
                    UNION ALL
                    SELECT e.flow_id, e.amount
                    FROM reco.reconciliation_entry_emargement e
                    JOIN reco.match_group mg ON e.match_group_id = mg.id
                    WHERE mg.reconciliation_run_id > :run_id
                      AND e.status IN ('MATCHED', 'FORCED')
                ) sub
                GROUP BY flow_id
                """
            ),
            {"run_id": run_id},
        ).all()

        # Excluded count (always current)
        excluded_rows = db.execute(
            text(
                """
                SELECT flow_id,
                       COUNT(*) AS cnt,
                       COALESCE(SUM(amount), 0) AS amount_total,
                       'EXCLUDED' AS status
                FROM reco.reconciliation_entry_emargement
                WHERE status = 'EXCLUDED'
                GROUP BY flow_id
                """
            )
        ).all()

        result = []
        for r in matched_rows:
            result.append(dict(r._mapping))
        for r in pending_rows:
            result.append(dict(r._mapping))
        for r in excluded_rows:
            result.append(dict(r._mapping))
        return result


reconciliation_entry_repository = ReconciliationEntryRepository()
