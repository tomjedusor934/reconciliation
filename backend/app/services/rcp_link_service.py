"""Reattribution of return/reject movements (RCP/RCC/RRS/WCC) to what they undo.

WHY THIS EXISTS. A return produces two legs on the float. The INDIVIDUAL legs
(``SCTXB/NCP/O/<po>``, ``NDRJ##…``) are already resolved by the BB DAG through
``ref_no → PaymentNumber → std.Payment → bucket of the original payment``, so
they land in the original bulk's lot. The AGGREGATE leg — one movement per
return batch, ``msgid`` = BLK…/numeric — has no such key: the datamart carries
no link between that msgid and the payments it returns, so the DAG treats it as
an SP direct, finds no payment for ``MessageIDPACS008 = 'BLK…'`` and files it
alone in a ``PACS_ONLY`` bucket. Both sides then look unbalanced forever.

The extracts (link workbook + return/reject dumps) carry the missing link:
``msgid → origEntityId``, which IS ``std.Payment.PaymentNumber`` of the original
payment. From there the original's ``MessageID``/``MessageIDPACS008`` name its
bucket — the same bucket its individual return leg already sits in.

A return batch almost always spans SEVERAL originals (measured: up to 12+
distinct originals for one msgid), so the movement is not moved but SPLIT: it
becomes a claim-group parent and is replaced by one ghost per target, each worth
that target's exact returned amount. That is the existing machinery
(``movement_split`` + ``split_service`` + ``lot_service``), used verbatim — the
claim key type ``RCPLINK`` is the only thing that distinguishes these groups
from the DAG's own (PACS008/MSGID/PO), so the two can never collide.

TWO FLOWS, TWO KINDS OF TARGET. These movements do not all live on a
batch-booking flow: the outward float (0010130015001) is BB, the inward one
(0010130015002) still runs the CLASSIC finacle parser, where there are no lots
at all. The symptom is identical there — ``compute_reco_id`` keys a bulk direct
on its own ``Remarks_1`` (so the return batch sits alone) while the individual
return legs are keyed on ``COALESCE(MessageIDPACS008, MessageID)`` of the
original payment (``resolve_bulk_returns`` in shared/dags/reco_datamart.py) —
so the fix is the same split, aimed at a RECONCILIATION KEY instead of a lot.
``apply_split_batch`` writes ``reco_id = child.lot_id`` on its ghosts and never
checks that a lot exists, so the classic branch is the very same call minus the
lot batch.

WHICH BRANCH IS NEVER GUESSED. The extract correlates direction and msgid shape
(BLK/I on the BB flow, numeric/O on the classic one), but that is a symptom, not
a rule — and RCC/RRS/WCC have never been observed. The branch is decided by the
``parser_type`` of the source the movement was actually found on.

Nothing is written by ``analyze``. ``commit`` only applies the movements the
operator ticked, and re-validates each one against the database first.
"""
import logging
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import and_, or_, text, tuple_
from sqlalchemy.orm import Session

from app.models.flow import Flow, FlowSource, FlowSourceType, ParserType
from app.models.ingestion_run import IngestionRun
from app.models.movement_lot import LOT_STATUS_MERGED, MovementLot, MovementLotKey, MovementLotMember
from app.models.movement_split import MovementSplit
from app.models.payment_status import EntryPaymentStatus
from app.models.reconciliation_entry import (
    EntryStatus,
    ReconciliationEntry,
    ReconciliationEntryEmargement,
)
from app.repositories.movement_split_repository import movement_split_repository
from app.schemas.lot import LotKeyIn, LotMemberIn
from app.schemas.split import SplitChildIn, SplitGroupIn, SplitParentIn
from app.services.audit_service import audit_service
from app.services.lot_service import lot_service
from app.services.rcp_link_parser import (
    CTRL_NOT_FOUND,
    BucketKey,
    ControlResult,
    DumpRow,
    LinkMovement,
    build_dump_index,
    control_all,
    ghost_external_ref,
    parse_dump_file,
    parse_link_workbook,
    payment_bucket,
    q2,
)
from app.services.source_connection_service import source_connection_service
from app.services.split_service import split_service

logger = logging.getLogger(__name__)

# Claim key type of the groups this tool creates. Deliberately NOT one of the
# DAG's (PACS008 / MSGID / PO): a manual reattribution group must never merge
# with a group the ingestion computed.
CLAIM_TYPE = "RCPLINK"

# Rows per INSERT into the lookup temp table (fast_executemany).
PO_INSERT_BATCH = 1000
# Fallback IN-list size. Deliberately small: a long IN list is what pushed SQL
# Server off the PaymentNumber index in the first place.
PO_LOOKUP_CHUNK = 200
# Session-scoped lookup table. Dropped on both ends — the connection is pooled.
TEMP_PO_TABLE = "#reco_rcp_po"
# msgids per ILIKE sweep (stage 2 only). Each sweep is one scan of
# reconciliation_entry testing every pattern against every row, so this is the
# expensive path — kept for the ids remarks_1 could not answer.
MSGID_SWEEP_CHUNK = 60
# msgids per exact remarks_1 lookup (stage 1). A plain IN list, so it can be
# large: Postgres hashes it and the scan cost does not grow with the list.
REMARKS_LOOKUP_CHUNK = 2000
# Mirrors RECO_FINACLE_BB_MAX_PO_KEYS: a member carrying thousands of PO keys is
# not what navigates the graph.
MAX_PO_KEYS = 50

# Per-movement reattribution statuses.
ST_PROPOSED = "PROPOSED"                # ready to commit
ST_TO_RECOMMIT = "TO_RECOMMIT"          # committed once, movement came back live
ST_TO_REPLAY = "TO_REPLAY"              # committed once, replayable from movement_split
ST_ALREADY_COMMITTED = "ALREADY_COMMITTED"
ST_EMARGED = "EMARGED"                  # warning: found only in émargement
ST_ENTRY_NOT_FOUND = "ENTRY_NOT_FOUND"
ST_ENTRY_AMBIGUOUS = "ENTRY_AMBIGUOUS"
ST_ENTRY_NOT_PENDING = "ENTRY_NOT_PENDING"
ST_NO_DUMP_ROWS = "NO_DUMP_ROWS"
ST_NO_TARGET = "NO_TARGET"
ST_FLOW_UNSUPPORTED = "FLOW_UNSUPPORTED"  # source is neither BB nor classic finacle

ACTIONABLE = (ST_PROPOSED, ST_TO_RECOMMIT, ST_TO_REPLAY)

# What ``_entry_out`` reports for a movement that no longer has a live row: it
# was withdrawn on purpose and its ghosts stand in for it.
ENTRY_STATUS_REPLACED = "replaced"

# What a ghost is aimed at. LOT = a movement_lot uuid (batch-booking flow);
# RECO = the flow's own reconciliation key (classic bulk flow, no lots).
TARGET_LOT = "LOT"
TARGET_RECO = "RECO"

# Why one payment could not be pointed at a target.
WHY_NO_PAYMENT = "NOT_IN_DATAMART"      # PaymentNumber unknown to std.Payment
WHY_NO_BUCKET = "NO_BUCKET"             # payment carries none of pacs/msgid/po
WHY_NO_LOT = "NO_LOT"                   # key known, nothing in the app carries it
WHY_MULTI_LOT = "MULTI_LOT"             # the PO points at several targets


def _chunks(values: Sequence[Any], size: int) -> Iterable[Sequence[Any]]:
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _emit(progress: Optional[Any], message: str) -> None:
    """Write one line to the job's log (and the server log). Tolerates a plain
    callable progress — the batch runs identically without a log sink."""
    logger.info("[rcp] %s", message)
    sink = getattr(progress, "log", None)
    if sink:
        sink(message)


def _movement_type(transaction_particulars: Optional[str]) -> str:
    """TP prefix, same split rule as the DAG's ``_tp_parts`` ('##' then '/')."""
    tp = (transaction_particulars or "").strip()
    if not tp:
        return "SCTXB"
    parts = tp.split("##") if "##" in tp else tp.split("/")
    return (parts[0].strip().upper() or "SCTXB")[:16]


def _direction_value(direction: Any) -> Optional[str]:
    return getattr(direction, "value", direction)


class RcpLinkService:
    # ------------------------------------------------------------------
    # Analyze (read-only)
    # ------------------------------------------------------------------

    def analyze(
        self,
        db: Session,
        *,
        link_file: Tuple[str, bytes],
        dump_files: Sequence[Tuple[str, bytes]],
        connection_id: Optional[int] = None,
        flow_id: Optional[int] = None,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Parse, control, resolve — and propose. Writes nothing.

        ``progress`` (optional) is called at each phase so the polling endpoint
        can say what is running; the batch behaves identically without it.
        """
        def step(phase: str, done: int = 0, total: int = 0) -> None:
            if progress:
                progress(phase, done, total)

        step("lecture des fichiers")
        link_name, link_content = link_file
        movements, link_report = parse_link_workbook(link_content, link_name)
        _emit(
            progress,
            f"{link_name} : {link_report.rows} lignes, {len(movements)} à traiter ["
            + ", ".join(f"{k}={v}" for k, v in sorted(link_report.services.items())) + "]",
        )

        dump_rows: List[DumpRow] = []
        dump_reports = []
        for name, content in dump_files:
            rows, report = parse_dump_file(content, name)
            dump_rows.extend(rows)
            dump_reports.append(report)
            note = f" — ERREUR: {report.error}" if report.error else ""
            _emit(
                progress,
                f"{name} : {report.rows} lignes, {report.rows_with_msgid} avec msgid, "
                f"{report.distinct_msgids} msgid distincts, montant="
                f"{report.amount_column or '?'}{note}",
            )
            if report.rows and not report.rows_with_msgid:
                _emit(progress, f"ATTENTION {name} : aucun msgid exploitable, fichier inutilisable")
        index, dump_duplicates = build_dump_index(dump_rows)
        if dump_duplicates:
            _emit(
                progress,
                f"{dump_duplicates} ligne(s) en double entre fichiers ignorée(s) "
                f"({len(dump_rows)} lues, {len(dump_rows) - dump_duplicates} retenues) — "
                "les extraits quotidiens sont cumulatifs",
            )

        step("contrôle des montants", 0, len(movements))
        controls, duplicate_msgids = control_all(movements, index)
        controls_by_msgid = {c.msgid: c for c in controls}
        summary = self._control_summary(controls)
        _emit(
            progress,
            "contrôle : "
            + ", ".join(f"{k}={v}" for k, v in sorted(summary.items()) if k != "total"),
        )
        if duplicate_msgids:
            _emit(progress, f"ATTENTION {len(duplicate_msgids)} msgid en double dans le xlsx")

        proposals, datamart_error = self._resolve(
            db,
            movements=movements,
            index=index,
            controls=controls_by_msgid,
            connection_id=connection_id,
            flow_id=flow_id,
            progress=progress,
        )

        prop_summary = self._proposal_summary(proposals)
        _emit(
            progress,
            "propositions : "
            + ", ".join(f"{k}={v}" for k, v in sorted(prop_summary.items()) if k != "total"),
        )
        # Volumetry of the upload itself, kept out of the status breakdown above.
        prop_summary["dump_rows_read"] = len(dump_rows)
        prop_summary["dump_duplicates"] = dump_duplicates
        step("terminé")
        return {
            "link_file": self._file_report(link_report),
            "dump_files": [self._file_report(r) for r in dump_reports],
            "duplicate_msgids": duplicate_msgids,
            "controls": [self._control_out(c) for c in controls],
            "control_summary": self._control_summary(controls),
            "proposals": proposals,
            "summary": prop_summary,
            "datamart_error": datamart_error,
        }

    # -- resolution ----------------------------------------------------

    def _resolve(
        self,
        db: Session,
        *,
        movements: Sequence[LinkMovement],
        index: Dict[str, List[DumpRow]],
        controls: Dict[str, ControlResult],
        connection_id: Optional[int],
        flow_id: Optional[int],
        progress: Optional[Any] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        def step(phase: str, done: int = 0, total: int = 0) -> None:
            if progress:
                progress(phase, done, total)

        msgids = [m.msgid for m in movements]
        live = self._find_entries(
            db, ReconciliationEntry, msgids, flow_id, progress, ""
        )
        missing = [m for m in msgids if m not in live]
        emarged = self._find_entries(
            db, ReconciliationEntryEmargement, missing, flow_id, progress, "émargement — "
        )
        _emit(
            progress,
            f"mouvements trouvés : {len(live)} vivants, {len(emarged)} émargés, "
            f"{len(msgids) - len(live) - len(emarged)} introuvables",
        )
        step("splits déjà commités")
        committed = self._committed_claims(db, msgids)
        if committed:
            _emit(
                progress,
                f"{len(committed)} msgid déjà réattribués (split RCPLINK existant) — "
                "rejouables depuis movement_split",
            )
        # The sources those parents were found on: a replayed movement no longer
        # has an ingestion entry to travel with, and the source is what decides
        # lots vs reconciliation keys.
        replay_sources = self._sources_by_id(
            db, {p.flow_source_id for p in committed.values()}
        )

        # One datamart round-trip for every PO of the run, then one lot lookup.
        all_pos = sorted(
            {
                row.orig_entity_id
                for m in movements
                for row in index.get(m.msgid, [])
                if row.orig_entity_id
            }
        )
        step("interrogation du datamart", 0, len(all_pos))
        payments, datamart_error = self.resolve_payments(
            db, connection_id, all_pos, progress
        )

        # The resolution is the same for every movement of a source, and it costs
        # a handful of queries — resolve the run's whole PO set once per source
        # instead of once per movement (214 movements, 2 085 POs in the
        # 2026-08-12 extract).
        targets_by_source: Dict[int, Dict[str, Dict[str, Any]]] = {}

        def targets_for(source: Any, flow_id_of_entry: int) -> Dict[str, Dict[str, Any]]:
            if source.id not in targets_by_source:
                targets_by_source[source.id] = self.targets_for_pos(
                    db, all_pos, payments,
                    flow_id=flow_id_of_entry,
                    flow_source_id=source.id,
                    parser_type=source.parser_type,
                )
            return targets_by_source[source.id]

        step("résolution des cibles", 0, len(movements))
        _emit(progress, f"résolution des cibles pour {len(movements)} mouvement(s)")
        proposals: List[Dict[str, Any]] = []
        for position, movement in enumerate(movements, start=1):
            if position % 200 == 0:
                step("résolution des cibles", position, len(movements))
            rows = index.get(movement.msgid, [])
            control = controls.get(movement.msgid)
            proposal: Dict[str, Any] = {
                "msgid": movement.msgid,
                "direction": movement.direction,
                "tran_id": movement.tran_id,
                "sp_date": movement.sp_date,
                "num_records": movement.num_records,
                "settlement_amount": movement.settlement_amount,
                "control_status": control.status if control else CTRL_NOT_FOUND,
                "control_delta_amount": control.delta_amount if control else Decimal("0"),
                "control_delta_count": control.delta_count if control else 0,
                "service_id": movement.service_id,
                "flow_id": None,
                "flow_code": "",
                "source_code": "",
                "target_kind": "",
                "status": ST_PROPOSED,
                "message": "",
                "entry": None,
                "targets": [],
                "unresolved_payments": [],
                "resolved_amount": Decimal("0.00"),
                "candidates": [],
            }

            hits = live.get(movement.msgid, [])
            if not rows:
                proposal["status"] = ST_NO_DUMP_ROWS
                proposal["message"] = "aucune ligne de paiement pour ce msgid"
                proposals.append(proposal)
                continue
            if len(hits) > 1:
                proposal["status"] = ST_ENTRY_AMBIGUOUS
                proposal["message"] = f"{len(hits)} mouvements vivants portent ce msgid"
                proposal["candidates"] = [self._entry_out(e, src) for e, src in hits]
                proposals.append(proposal)
                continue

            # WHERE THE MOVEMENT IS. Live is the normal case. Failing that, an
            # already-committed movement is described by its split parent —
            # withdrawn on purpose, and (since the re-ingestion guard) it stays
            # that way, so ``movement_split`` is what a replay reads.
            replay = False
            if hits:
                entry, source = hits[0]
            elif movement.msgid in emarged:
                entry, source = emarged[movement.msgid][0]
                proposal["status"] = ST_EMARGED
                proposal["message"] = "mouvement déjà émargé — aucune action"
                proposal["entry"] = self._entry_out(entry, source)
                self._stamp_flow(proposal, entry, source)
                proposals.append(proposal)
                continue
            elif movement.msgid in committed:
                entry = committed[movement.msgid]
                source = replay_sources.get(entry.flow_source_id)
                if source is None:
                    proposal["status"] = ST_ALREADY_COMMITTED
                    proposal["message"] = (
                        "déjà réattribué, source d'origine introuvable — non rejouable"
                    )
                    proposals.append(proposal)
                    continue
                replay = True
            else:
                proposal["status"] = ST_ENTRY_NOT_FOUND
                proposal["message"] = "aucun mouvement de l'app ne porte ce msgid"
                proposals.append(proposal)
                continue

            proposal["entry"] = (
                self._split_entry_out(entry, source) if replay
                else self._entry_out(entry, source)
            )
            self._stamp_flow(proposal, entry, source)
            if source.parser_type not in (
                ParserType.FINACLE_BATCH_BOOKING_TRUE, ParserType.FINACLE_DB
            ):
                proposal["status"] = ST_FLOW_UNSUPPORTED
                proposal["message"] = (
                    f"source en parser {source.parser_type} — ni batch booking ni bulk classique"
                )
                proposals.append(proposal)
                continue
            if not replay and entry.status != EntryStatus.PENDING:
                proposal["status"] = ST_ENTRY_NOT_PENDING
                proposal["message"] = (
                    f"mouvement en statut {entry.status.value} — le retrait "
                    "ne touche que les entrées PENDING"
                )
                proposals.append(proposal)
                continue

            targets, unresolved = self._targets_for(
                rows, targets_for(source, entry.flow_id)
            )
            proposal["targets"] = targets
            proposal["unresolved_payments"] = unresolved
            proposal["resolved_amount"] = q2(
                sum((t["amount"] for t in targets), Decimal("0"))
            )
            if not targets:
                proposal["status"] = ST_NO_TARGET
                proposal["message"] = (
                    "aucune cible d'origine retrouvée"
                    + (f" ({datamart_error})" if datamart_error else "")
                )
            elif replay:
                proposal["status"] = ST_TO_REPLAY
                proposal["message"] = (
                    "déjà réattribué — rejouer met à jour les fantômes existants "
                    "(mêmes identités), sans ré-ingestion"
                )
            elif movement.msgid in committed:
                proposal["status"] = ST_TO_RECOMMIT
                proposal["message"] = (
                    "déjà réattribué, mais le mouvement est redevenu vivant "
                    "(ré-ingestion) — à re-committer"
                )
            elif unresolved:
                proposal["message"] = (
                    f"{len(unresolved)} paiement(s) non rattaché(s) — split partiel"
                )
            proposals.append(proposal)

        self._fill_flow_codes(db, proposals)
        return proposals, datamart_error

    @staticmethod
    def _fill_flow_codes(db: Session, proposals: Sequence[Dict[str, Any]]) -> None:
        """One query for every flow the run touched — the operator reads a code,
        not an id, to tell the outward float from the inward one."""
        flow_ids = sorted({p["flow_id"] for p in proposals if p.get("flow_id")})
        if not flow_ids:
            return
        codes = {
            row[0]: row[1]
            for row in db.query(Flow.id, Flow.code).filter(Flow.id.in_(flow_ids)).all()
        }
        for proposal in proposals:
            proposal["flow_code"] = codes.get(proposal.get("flow_id"), "")

    @staticmethod
    def _targets_for(
        rows: Sequence[DumpRow], targets: Dict[str, Dict[str, Any]]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Group a movement's returned payments by where their original sits —
        a lot on a batch-booking flow, a reconciliation key on a classic one.

        A payment returned twice under one msgid is ONE slice: the amounts add
        up, the payment counts once.

        ``pos`` is dropped above ``MAX_PO_KEYS``: the commit indexes the PO ids
        of a slice only while they stay few enough (``_member_keys``), so a
        longer list is payload nobody reads — and the whole run's list is
        40 250 ids on the 2026-08 extract, which would go into the report JSON,
        into ``rcp_run.result`` and down to the browser. ``payment_count`` stays
        exact whatever happens to the list.
        """
        pos: Dict[str, Decimal] = {}
        for row in rows:
            if not row.orig_entity_id:
                continue
            pos[row.orig_entity_id] = pos.get(row.orig_entity_id, Decimal("0")) + row.amount

        grouped: Dict[str, Dict[str, Any]] = {}
        unresolved: List[Dict[str, Any]] = []
        for po, amount in sorted(pos.items()):
            resolution = targets.get(po)
            if resolution is None or not resolution.get("target_id"):
                unresolved.append(
                    {
                        "po": po,
                        "amount": q2(amount),
                        "reason": (resolution or {}).get("reason", WHY_NO_PAYMENT),
                    }
                )
                continue
            target = grouped.setdefault(
                resolution["target_id"],
                {
                    "target_id": resolution["target_id"],
                    "target_kind": resolution.get("target_kind", TARGET_LOT),
                    # Buckets only exist on a lot target; a reconciliation key
                    # has none, and the UI shows the key itself instead.
                    "bucket_kind": resolution.get("bucket_kind", ""),
                    "bucket_pacs008": resolution.get("bucket_pacs008", ""),
                    "bucket_msgid": resolution.get("bucket_msgid", ""),
                    "bucket_po": resolution.get("bucket_po", ""),
                    "bucket_ref": resolution.get("bucket_ref", ""),
                    "label": resolution.get("label", resolution["target_id"]),
                    "resolved_via": resolution.get("resolved_via", ""),
                    "amount": Decimal("0"),
                    "payment_count": 0,
                    "pos": [],
                },
            )
            target["amount"] += amount
            target["payment_count"] += 1
            target["pos"].append(po)
        for target in grouped.values():
            target["amount"] = q2(target["amount"])
            if len(target["pos"]) > MAX_PO_KEYS:
                target["pos"] = []
        return (
            sorted(grouped.values(), key=lambda t: (-abs(t["amount"]), t["target_id"])),
            unresolved,
        )

    # -- lookups -------------------------------------------------------

    def _find_entries(
        self,
        db: Session,
        model: Any,
        msgids: Sequence[str],
        flow_id: Optional[int],
        progress: Optional[Any] = None,
        phase: str = "",
    ) -> Dict[str, List[Tuple[Any, Any]]]:
        """{msgid: [(entry, source)]} — movements whose ``transaction_particulars``
        quote the msgid, on any FINACLE_DB source.

        The source travels with the entry because it is what decides everything
        downstream: its ``parser_type`` says whether this movement is split into
        lots (batch booking) or onto reconciliation keys (classic bulk). That is
        never inferred from the msgid shape or the direction.

        TWO STAGES, because the substring sweep is what made this a batch. An SP
        direct movement stores the message id VERBATIM in ``remarks_1``
        ('SCTXB/I/BLK2026196001064' → 'BLK2026196001064'), so stage 1 asks for
        equality against the whole set at once: Postgres hashes the array and
        the scan costs one lookup per row, whatever the number of ids. Stage 2
        keeps the original ``ILIKE '%id%'`` sweep but only for the ids stage 1
        did not answer — a handful instead of all of them.

        Before, 1 627 ids meant 28 full scans testing 60 patterns each (~450M
        substring comparisons) and the request died on nginx's 300s timeout.
        """
        found: Dict[str, List[Tuple[Any, Any]]] = {}
        msgids = [m for m in msgids if m]
        if not msgids:
            return found

        def base_query():
            query = (
                db.query(model, FlowSource)
                .join(IngestionRun, model.ingestion_run_id == IngestionRun.id)
                .join(FlowSource, IngestionRun.flow_source_id == FlowSource.id)
                .filter(FlowSource.source_type == FlowSourceType.FINACLE_DB)
            )
            return query.filter(model.flow_id == flow_id) if flow_id else query

        def keep(entry: Any, source: Any, candidates: Sequence[str]) -> None:
            """A row can only be claimed by an id its particulars actually quote —
            the equality on remarks_1 is a shortcut, never the criterion."""
            particulars = (entry.transaction_particulars or "").upper()
            for msgid in candidates:
                if msgid.upper() in particulars:
                    found.setdefault(msgid, []).append((entry, source))

        # Stage 1 — exact remarks_1, one scan for the whole set.
        if progress:
            progress(f"{phase}recherche des mouvements (clé directe)", 0, len(msgids))
        by_upper: Dict[str, List[str]] = {}
        for msgid in msgids:
            by_upper.setdefault(msgid.upper(), []).append(msgid)
        for chunk in _chunks(msgids, REMARKS_LOOKUP_CHUNK):
            rows = base_query().filter(model.remarks_1.in_(list(chunk))).all()
            for entry, source in rows:
                keep(entry, source, by_upper.get((entry.remarks_1 or "").upper(), []))

        # Stage 2 — substring sweep, only for what is still missing.
        missing = [m for m in msgids if m not in found]
        if not missing:
            _emit(progress, f"{phase}{len(found)}/{len(msgids)} msgid trouvés sur remarks_1")
            return found
        sweeps = (len(missing) + MSGID_SWEEP_CHUNK - 1) // MSGID_SWEEP_CHUNK
        _emit(
            progress,
            f"{phase}{len(found)}/{len(msgids)} msgid trouvés sur remarks_1, "
            f"{sweeps} balayage(s) ILIKE pour {len(missing)} restant(s)",
        )
        for index, chunk in enumerate(_chunks(missing, MSGID_SWEEP_CHUNK), start=1):
            if progress:
                progress(f"{phase}recherche des mouvements (balayage)", index, sweeps)
            conditions = [
                model.transaction_particulars.ilike(f"%{_escape_like(m)}%", escape="\\")
                for m in chunk
            ]
            for entry, source in base_query().filter(or_(*conditions)).all():
                keep(entry, source, chunk)
        return found

    def _committed_claims(self, db: Session, msgids: Sequence[str]) -> Dict[str, Any]:
        """{msgid: its registered RCPLINK split parent}.

        The parent row is what makes a committed movement REPLAYABLE: its real
        movement was withdrawn (and, since the re-ingestion guard, stays
        withdrawn), so ``movement_split`` is the only remaining description of
        it — amount, dates, account, particulars — and it is enough to re-emit
        the group with corrected slices.

        One row per claim: these groups are built by this tool, one parent per
        msgid. Should a claim ever carry several, the canonical one wins — the
        same order ``resolve_group_canonicals`` uses, so the ghosts keep their
        anchor.
        """
        values = [m.upper() for m in msgids if m]
        if not values:
            return {}
        by_claim: Dict[str, Any] = {}
        for chunk in _chunks(values, 500):
            rows = (
                db.query(MovementSplit)
                .filter(
                    MovementSplit.claim_key_type == CLAIM_TYPE,
                    MovementSplit.claim_key_value.in_(list(chunk)),
                )
                .order_by(
                    MovementSplit.value_date,
                    MovementSplit.external_ref,
                    MovementSplit.source_hash,
                )
                .all()
            )
            for row in rows:
                by_claim.setdefault(row.claim_key_value, row)
        return {m: by_claim[m.upper()] for m in msgids if m.upper() in by_claim}

    def resolve_payments(  # public: shared with rcp_orphan_service
        self,
        db: Session,
        connection_id: Optional[int],
        po_ids: Sequence[str],
        progress: Optional[Any] = None,
    ) -> Tuple[Dict[str, List[Tuple[str, str]]], str]:
        """{PaymentNumber: [(MessageIDPACS008, MessageID)]} from the datamart.

        Read-only, in a transaction that is always rolled back — same guard as
        the connection test endpoint. A missing/unreachable connection is not
        fatal: the app-side fallbacks (entry_payment_status, lot PO keys) still
        resolve most payments, and the error is reported to the operator.

        THE LOOKUP IS A TEMP-TABLE JOIN, not an ``IN`` list. 14 534 PaymentNumbers
        as fifteen 1 000-term ``IN`` predicates made SQL Server re-plan and often
        re-scan ``std.Payment`` for each one; the run sat on that phase for
        minutes. Loading the keys into a temp table and joining ONCE is what the
        ingestion DAG has always done (``_temp_join`` in reco_datamart_bb.py) —
        one seek per key against the PaymentNumber index instead.
        """
        if not po_ids:
            return {}, ""
        if not connection_id:
            return {}, "aucune connexion datamart sélectionnée"
        connection = source_connection_service.get_connection(db, connection_id)
        if connection is None:
            return {}, f"connexion {connection_id} introuvable"

        values = sorted({(p or "").strip() for p in po_ids if (p or "").strip()})
        _emit(progress, f"datamart : {len(values)} PaymentNumber à résoudre")
        rows: List[Tuple[Any, Any, Any]] = []
        try:
            engine = source_connection_service.get_engine(connection)
            with engine.connect() as remote:
                try:
                    rows = self._payments_via_temp_table(remote, values, progress)
                except Exception as exc:  # noqa: BLE001 — degraded, not fatal
                    logger.warning("[rcp] temp table indisponible (%s), repli IN", exc)
                    _emit(progress, f"table temporaire refusée ({exc}) — repli sur des IN")
                    rows = self._payments_via_in_list(remote, values, progress)
        except Exception as exc:  # noqa: BLE001 — surfaced to the operator
            logger.warning("[rcp] datamart lookup failed: %s", exc)
            _emit(progress, f"datamart injoignable : {exc}")
            return {}, f"datamart injoignable: {exc}"

        payments: Dict[str, List[Tuple[str, str]]] = {}
        for po, pacs, msgid in rows:
            key = (po or "").strip()
            if not key:
                continue
            pair = ((pacs or "").strip(), (msgid or "").strip())
            bucket = payments.setdefault(key, [])
            if pair not in bucket:
                bucket.append(pair)
        _emit(
            progress,
            f"datamart : {len(payments)}/{len(values)} PaymentNumber trouvés dans std.Payment",
        )
        return payments, ""

    _PAYMENT_SELECT = (
        "SELECT LTRIM(RTRIM(p.PaymentNumber)) AS po, "
        "       LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs, "
        "       LTRIM(RTRIM(p.MessageID)) AS msgid "
        "FROM std.Payment p "
    )

    def _payments_via_temp_table(
        self, remote: Any, values: Sequence[str], progress: Optional[Any]
    ) -> List[Tuple[Any, Any, Any]]:
        """Load the keys into #reco_rcp_po, index it, join std.Payment once.

        The index is NON-UNIQUE and built after the load, exactly as in the DAG:
        the datamart's collation is case-INSENSITIVE while Python de-duplicated
        case-sensitively, so two spellings that are one key there would abort the
        whole run on a PRIMARY KEY. Uniqueness was never needed — the index only
        has to make the join a seek.

        The temp table is session-scoped and this connection goes back to a POOL,
        so it is dropped defensively on both ends: a leftover #reco_rcp_po would
        make the next run fail on CREATE.
        """
        cursor = remote.connection.cursor()
        try:
            cursor.execute(f"IF OBJECT_ID('tempdb..{TEMP_PO_TABLE}') IS NOT NULL DROP TABLE {TEMP_PO_TABLE}")
            cursor.execute(f"CREATE TABLE {TEMP_PO_TABLE} (po VARCHAR(64) NOT NULL)")
            try:
                cursor.fast_executemany = True
            except AttributeError:      # non-pyodbc driver
                pass
            for index, chunk in enumerate(_chunks(values, PO_INSERT_BATCH), start=1):
                cursor.executemany(
                    f"INSERT INTO {TEMP_PO_TABLE} (po) VALUES (?)", [(v,) for v in chunk]
                )
                if progress:
                    progress(
                        "datamart — chargement des clés",
                        min(index * PO_INSERT_BATCH, len(values)),
                        len(values),
                    )
            cursor.execute(f"CREATE CLUSTERED INDEX ix_reco_rcp_po ON {TEMP_PO_TABLE} (po)")
            if progress:
                progress("datamart — jointure std.Payment", 0, len(values))
            _emit(progress, "datamart : jointure std.Payment en cours")
            cursor.execute(
                self._PAYMENT_SELECT
                + f"INNER JOIN {TEMP_PO_TABLE} t ON p.PaymentNumber = t.po "
                "WHERE p.IsCurrent = 1"
            )
            rows = [tuple(r) for r in cursor.fetchall()]
            if progress:
                progress("datamart — jointure std.Payment", len(values), len(values))
            return rows
        finally:
            try:
                cursor.execute(
                    f"IF OBJECT_ID('tempdb..{TEMP_PO_TABLE}') IS NOT NULL DROP TABLE {TEMP_PO_TABLE}"
                )
            except Exception:  # noqa: BLE001 — best effort
                pass
            cursor.close()

    def _payments_via_in_list(
        self, remote: Any, values: Sequence[str], progress: Optional[Any]
    ) -> List[Tuple[Any, Any, Any]]:
        """Fallback when the connection may not create a temp table. Smaller
        batches than before: a short IN list keeps the plan on an index seek."""
        rows: List[Tuple[Any, Any, Any]] = []
        transaction = remote.begin()
        try:
            done = 0
            for chunk in _chunks(values, PO_LOOKUP_CHUNK):
                params = {f"p{i}": value for i, value in enumerate(chunk)}
                placeholders = ", ".join(f":{name}" for name in params)
                result = remote.execute(
                    text(
                        self._PAYMENT_SELECT
                        + "WHERE p.IsCurrent = 1 "
                        f"  AND p.PaymentNumber IN ({placeholders})"
                    ),
                    params,
                )
                rows.extend(tuple(r) for r in result.fetchall())
                done += len(chunk)
                if progress:
                    progress("datamart — lookup par lots", done, len(values))
        finally:
            transaction.rollback()
        return rows

    def targets_for_pos(  # public: shared with rcp_orphan_service
        self,
        db: Session,
        po_ids: Iterable[str],
        payments: Dict[str, List[Tuple[str, str]]],
        *,
        flow_id: int,
        flow_source_id: int,
        parser_type: Any,
    ) -> Dict[str, Dict[str, Any]]:
        """{PaymentNumber: where its returned amount belongs}.

        Batch-booking flow → the LOT holding the original payment's bucket.
        Classic bulk flow → the RECONCILIATION KEY the original payment's own
        return legs are already filed under. One branch per parser, never per
        msgid shape.
        """
        po_ids = [p for p in po_ids if p]
        if not po_ids:
            return {}
        if parser_type != ParserType.FINACLE_BATCH_BOOKING_TRUE:
            return self._reco_keys_for_pos(db, po_ids, payments, flow_id=flow_id)
        return self._lots_for_pos(db, po_ids, payments, flow_source_id)

    def _reco_keys_for_pos(
        self,
        db: Session,
        po_ids: Sequence[str],
        payments: Dict[str, List[Tuple[str, str]]],
        *,
        flow_id: int,
    ) -> Dict[str, Dict[str, Any]]:
        """Classic bulk flow: the key the original payment's return legs carry.

        ``COALESCE(NULLIF(MessageIDPACS008,''), NULLIF(MessageID,''))`` — the
        EXACT expression ``resolve_bulk_returns`` runs in the ingestion DAG
        (shared/dags/reco_datamart.py). Its docstring claims the opposite order;
        the SQL is what actually keys the entries, and a ghost keyed the other
        way round would sit in a group of its own forever.

        A key nobody carries is refused: it is the classic analogue of "the lot
        does not exist", and it would otherwise create a ghost in the void.
        ``entry_payment_status`` (keyed by the flow's reco_id, whatever the flow)
        fills in when std.Payment has nothing.
        """
        resolved: Dict[str, Dict[str, Any]] = {}
        pending: List[str] = []
        for po in po_ids:
            keys = []
            for pacs, msgid in payments.get(po, []):
                key = (pacs or "").strip() or (msgid or "").strip()
                if key and key not in keys:
                    keys.append(key)
            if len(keys) == 1:
                resolved[po] = {"target_id": keys[0], "resolved_via": "datamart"}
            elif len(keys) > 1:
                resolved[po] = {"target_id": None, "reason": WHY_MULTI_LOT}
            else:
                pending.append(po)

        if pending:
            status_map = self._reco_ids_from_payment_status(db, pending)
            still: List[str] = []
            for po in pending:
                key = status_map.get(po)
                if key:
                    resolved[po] = {
                        "target_id": key, "resolved_via": "entry_payment_status"
                    }
                else:
                    still.append(po)
            for po in still:
                resolved[po] = {
                    "target_id": None,
                    "reason": WHY_NO_PAYMENT if po not in payments else WHY_NO_BUCKET,
                }

        known = self.known_reco_ids(
            db,
            flow_id=flow_id,
            reco_ids=[r["target_id"] for r in resolved.values() if r.get("target_id")],
        )
        for resolution in resolved.values():
            target = resolution.get("target_id")
            if target and target not in known:
                resolution["target_id"] = None
                resolution["reason"] = WHY_NO_LOT
        for resolution in resolved.values():
            if resolution.get("target_id"):
                resolution.setdefault("target_kind", TARGET_RECO)
                resolution.setdefault("label", resolution["target_id"])
        return resolved

    def known_reco_ids(
        self, db: Session, *, flow_id: int, reco_ids: Sequence[str]
    ) -> set:
        """Which of ``reco_ids`` are actually carried by an entry of the flow —
        live or émargée. An émargé group still counts: the original bulk being
        already reconciled is exactly the normal case."""
        values = sorted({r for r in reco_ids if r})
        if not values:
            return set()
        found = set()
        for chunk in _chunks(values, 1000):
            for model in (ReconciliationEntry, ReconciliationEntryEmargement):
                rows = (
                    db.query(model.reco_id)
                    .filter(model.flow_id == flow_id, model.reco_id.in_(list(chunk)))
                    .distinct()
                    .all()
                )
                found.update(row[0] for row in rows)
        return found

    def _reco_ids_from_payment_status(
        self, db: Session, po_ids: Sequence[str]
    ) -> Dict[str, Optional[str]]:
        """{po_id: reco_id} from ``entry_payment_status``, dropping the PO ids
        that point at several groups (nothing here can disambiguate them)."""
        by_po: Dict[str, set] = {}
        for chunk in _chunks(list(po_ids), 1000):
            rows = (
                db.query(EntryPaymentStatus.po_id, EntryPaymentStatus.reco_id)
                .filter(EntryPaymentStatus.po_id.in_(list(chunk)))
                .distinct()
                .all()
            )
            for po, reco_id in rows:
                by_po.setdefault(po, set()).add(reco_id)
        return {
            po: next(iter(ids)) if len(ids) == 1 else None
            for po, ids in by_po.items()
        }

    def _lots_for_pos(
        self,
        db: Session,
        po_ids: Iterable[str],
        payments: Dict[str, List[Tuple[str, str]]],
        flow_source_id: int,
    ) -> Dict[str, Dict[str, Any]]:
        """Batch-booking flow: {PaymentNumber: lot resolution}, datamart bucket
        first, then the two app-side fallbacks."""
        po_ids = [p for p in po_ids if p]
        if not po_ids:
            return {}

        # 1. bucket of the original payment (the DAG's own rule)
        buckets: Dict[str, List[BucketKey]] = {}
        for po in po_ids:
            keys = []
            for pacs, msgid in payments.get(po, []):
                key = payment_bucket(pacs, msgid, po)
                if key is not None and key not in keys:
                    keys.append(key)
            if keys:
                buckets[po] = keys
        lot_by_bucket = self._lots_by_bucket(
            db,
            {key for keys in buckets.values() for key in keys},
            flow_source_id,
        )

        resolved: Dict[str, Dict[str, Any]] = {}
        pending: List[str] = []
        for po in po_ids:
            hits = {
                lot_by_bucket[key]["target_id"]: lot_by_bucket[key]
                for key in buckets.get(po, [])
                if key in lot_by_bucket
            }
            if len(hits) == 1:
                resolved[po] = dict(next(iter(hits.values())), resolved_via="datamart")
            elif len(hits) > 1:
                resolved[po] = {"target_id": None, "reason": WHY_MULTI_LOT}
            else:
                pending.append(po)

        # 2. entry_payment_status: the payment's own reco_id IS its lot
        if pending:
            status_map = self._lots_from_payment_status(db, pending, flow_source_id)
            still: List[str] = []
            for po in pending:
                hit = status_map.get(po)
                if hit and hit.get("target_id"):
                    resolved[po] = dict(hit, resolved_via="entry_payment_status")
                elif hit and hit.get("reason") == WHY_MULTI_LOT:
                    # Genuinely ambiguous — a further lookup cannot disambiguate.
                    resolved[po] = hit
                else:
                    still.append(po)
            pending = still

        # 3. the lot keys the DAG indexed for small buckets
        if pending:
            key_map = self._lots_from_lot_keys(db, pending, flow_source_id)
            for po in pending:
                hit = key_map.get(po)
                if hit and hit.get("target_id"):
                    resolved[po] = dict(hit, resolved_via="lot_key")
                else:
                    resolved[po] = hit or {
                        "target_id": None,
                        "reason": WHY_NO_PAYMENT if po not in payments else WHY_NO_LOT,
                    }
        return resolved

    def _lots_by_bucket(
        self, db: Session, keys: Iterable[BucketKey], flow_source_id: int
    ) -> Dict[BucketKey, Dict[str, Any]]:
        keys = list(keys)
        if not keys:
            return {}
        found: Dict[BucketKey, Dict[str, Any]] = {}
        for chunk in _chunks(keys, 500):
            rows = (
                db.query(MovementLot)
                .filter(self.bucket_filter(flow_source_id, chunk))
                .all()
            )
            by_identity = {
                (r.bucket_kind, r.bucket_pacs008, r.bucket_msgid, r.bucket_po, r.bucket_ref): r
                for r in rows
            }
            for key in chunk:
                row = by_identity.get((key.kind, key.pacs, key.msgid, key.po, key.ref))
                if row is not None:
                    found[key] = self._lot_out(row)
        return found

    @staticmethod
    def bucket_filter(flow_source_id: int, keys: Sequence[BucketKey]) -> Any:
        """Criterion selecting the lots of ``keys`` — a row-value IN over the
        bucket identity, which is exactly the unique constraint's column order,
        so a chunk costs one index seek instead of an OR chain of five
        equalities. Compiled in tests/test_sql_compiles.py: a row-value IN is
        dialect-specific and only blows up when it runs."""
        return and_(
            MovementLot.flow_source_id == flow_source_id,
            tuple_(
                MovementLot.bucket_kind,
                MovementLot.bucket_pacs008,
                MovementLot.bucket_msgid,
                MovementLot.bucket_po,
                MovementLot.bucket_ref,
            ).in_([(k.kind, k.pacs, k.msgid, k.po, k.ref) for k in keys]),
        )

    def _lots_from_payment_status(
        self, db: Session, po_ids: Sequence[str], flow_source_id: int
    ) -> Dict[str, Dict[str, Any]]:
        reco_by_po: Dict[str, set] = {}
        for chunk in _chunks(list(po_ids), 1000):
            rows = (
                db.query(EntryPaymentStatus.po_id, EntryPaymentStatus.reco_id)
                .filter(EntryPaymentStatus.po_id.in_(list(chunk)))
                .distinct()
                .all()
            )
            for po, reco_id in rows:
                reco_by_po.setdefault(po, set()).add(reco_id)
        return self._resolve_lot_ids(db, reco_by_po, flow_source_id)

    def _lots_from_lot_keys(
        self, db: Session, po_ids: Sequence[str], flow_source_id: int
    ) -> Dict[str, Dict[str, Any]]:
        lot_by_po: Dict[str, set] = {}
        upper = {p.upper(): p for p in po_ids}
        for chunk in _chunks(list(upper.keys()), 1000):
            rows = (
                db.query(MovementLotKey.key_value, MovementLotMember.lot_id)
                .join(MovementLotMember, MovementLotMember.id == MovementLotKey.member_id)
                .filter(
                    MovementLotKey.key_type == "PO",
                    MovementLotKey.key_value.in_(list(chunk)),
                )
                .distinct()
                .all()
            )
            for key_value, lot_id in rows:
                po = upper.get(key_value, key_value)
                lot_by_po.setdefault(po, set()).add(lot_id)
        return self._resolve_lot_ids(db, lot_by_po, flow_source_id)

    def _resolve_lot_ids(
        self, db: Session, lot_ids_by_po: Dict[str, set], flow_source_id: int
    ) -> Dict[str, Dict[str, Any]]:
        """Turn {po: {lot_id}} into lot rows, keeping only this source's lots."""
        all_ids = sorted({lot_id for ids in lot_ids_by_po.values() for lot_id in ids if lot_id})
        if not all_ids:
            return {}
        rows: Dict[str, Any] = {}
        for chunk in _chunks(all_ids, 1000):
            for row in (
                db.query(MovementLot)
                .filter(
                    MovementLot.id.in_(list(chunk)),
                    MovementLot.flow_source_id == flow_source_id,
                )
                .all()
            ):
                rows[row.id] = row
        result: Dict[str, Dict[str, Any]] = {}
        for po, ids in lot_ids_by_po.items():
            hits = {lot_id: rows[lot_id] for lot_id in ids if lot_id in rows}
            if len(hits) == 1:
                result[po] = self._lot_out(next(iter(hits.values())))
            elif len(hits) > 1:
                result[po] = {"target_id": None, "reason": WHY_MULTI_LOT}
            else:
                result[po] = {"target_id": None, "reason": WHY_NO_LOT}
        return result

    @staticmethod
    def _lot_out(row: Any) -> Dict[str, Any]:
        """A lot as a target. A legacy merged lot redirects to its survivor."""
        lot_id = row.id
        redirected = row.status == LOT_STATUS_MERGED and bool(row.merged_into_lot_id)
        if redirected:
            lot_id = row.merged_into_lot_id
        key = BucketKey(
            row.bucket_kind,
            pacs=row.bucket_pacs008,
            msgid=row.bucket_msgid,
            po=row.bucket_po,
            ref=row.bucket_ref,
        )
        # The bucket columns describe the lot the payment was FOUND in; the
        # commit re-reads the target lot's own identity, so a redirect says so.
        label = key.label() if not redirected else f"{key.label()} (fusionné → {lot_id})"
        return {
            "target_id": lot_id,
            "target_kind": TARGET_LOT,
            "bucket_kind": row.bucket_kind,
            "bucket_pacs008": row.bucket_pacs008,
            "bucket_msgid": row.bucket_msgid,
            "bucket_po": row.bucket_po,
            "bucket_ref": row.bucket_ref,
            "label": label,
            "currency": row.currency,
        }

    # -- serialization -------------------------------------------------

    @staticmethod
    def _file_report(report: Any) -> Dict[str, Any]:
        return {
            "file_name": report.file_name,
            "rows": report.rows,
            "rows_with_msgid": report.rows_with_msgid,
            "distinct_msgids": report.distinct_msgids,
            "has_msgid_column": report.has_msgid_column,
            "amount_column": report.amount_column,
            "delimiter": report.delimiter,
            "error": report.error,
            "services": report.services,
        }

    @staticmethod
    def _control_out(control: ControlResult) -> Dict[str, Any]:
        return {
            "msgid": control.msgid,
            "status": control.status,
            "expected_count": control.expected_count,
            "found_count": control.found_count,
            "expected_amount": control.expected_amount,
            "found_amount": control.found_amount,
            "delta_count": control.delta_count,
            "delta_amount": control.delta_amount,
            "files": control.files,
            "service_id": control.service_id,
            "direction": control.direction,
            "tran_id": control.tran_id,
            "sp_date": control.sp_date,
        }

    @staticmethod
    def _stamp_flow(proposal: Dict[str, Any], entry: Any, source: Any) -> None:
        """Record WHERE the movement was found — and therefore what it will be
        split onto. ``target_kind`` comes from the source's parser, never from
        the msgid shape."""
        proposal["flow_id"] = entry.flow_id
        proposal["source_code"] = source.code
        proposal["target_kind"] = (
            TARGET_LOT
            if source.parser_type == ParserType.FINACLE_BATCH_BOOKING_TRUE
            else TARGET_RECO
        )

    @staticmethod
    def _sources_by_id(db: Session, source_ids: Iterable[int]) -> Dict[int, Any]:
        ids = sorted({i for i in source_ids if i})
        if not ids:
            return {}
        return {
            row.id: row
            for row in db.query(FlowSource).filter(FlowSource.id.in_(ids)).all()
        }

    @staticmethod
    def _split_entry_out(parent: Any, source: Any) -> Dict[str, Any]:
        """A withdrawn movement, as described by its ``movement_split`` row.

        Same shape as ``_entry_out`` so the UI does not have to know the
        difference: no id and no reco_id (there is no live row any more), and a
        status saying WHY — it was replaced by its ghosts, not matched.
        """
        return {
            "id": None,
            "flow_id": parent.flow_id,
            "flow_source_id": getattr(source, "id", None),
            "source_hash": parent.source_hash,
            "reco_id": None,
            "account": parent.account,
            "currency": parent.currency,
            "amount": parent.amount,
            "direction": _direction_value(parent.direction),
            "value_date": parent.value_date,
            "operation_date": parent.operation_date,
            "external_ref": parent.external_ref,
            "transaction_particulars": parent.transaction_particulars,
            "ref_no": parent.ref_no,
            "remarks_1": parent.remarks_1,
            "status": ENTRY_STATUS_REPLACED,
        }

    @staticmethod
    def _entry_out(entry: Any, source: Any) -> Dict[str, Any]:
        return {
            "id": entry.id,
            "flow_id": entry.flow_id,
            "flow_source_id": getattr(source, "id", None),
            "source_hash": entry.source_hash,
            "reco_id": entry.reco_id,
            "account": entry.account,
            "currency": entry.currency,
            "amount": entry.amount,
            "direction": _direction_value(entry.direction),
            "value_date": entry.value_date,
            "operation_date": entry.operation_date,
            "external_ref": entry.external_ref,
            "transaction_particulars": entry.transaction_particulars,
            "ref_no": entry.ref_no,
            "remarks_1": entry.remarks_1,
            "status": _direction_value(entry.status),
        }

    @staticmethod
    def _control_summary(controls: Sequence[ControlResult]) -> Dict[str, int]:
        summary: Dict[str, int] = {"total": len(controls)}
        for control in controls:
            summary[control.status] = summary.get(control.status, 0) + 1
        return summary

    @staticmethod
    def _proposal_summary(proposals: Sequence[Dict[str, Any]]) -> Dict[str, int]:
        summary: Dict[str, int] = {"total": len(proposals)}
        for proposal in proposals:
            summary[proposal["status"]] = summary.get(proposal["status"], 0) + 1
        return summary

    # ------------------------------------------------------------------
    # Commit (explicit approval only)
    # ------------------------------------------------------------------

    def commit(
        self,
        db: Session,
        *,
        items: Sequence[Any],
        user_id: Optional[int] = None,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Apply the ticked reattributions, one claim group per movement.

        Each item is re-validated against the database first — the analysis it
        came from is a snapshot, and the run that produced it may be minutes
        old. Amounts are re-signed here from the entry itself, so a client can
        never flip a ghost's sign.

        Two calls per movement, in the DAG's order: the split (parents
        registered, ghosts materialised, real movement withdrawn) then the lot
        members. Each is its own transaction — a movement either lands whole or
        not at all, and a later failure never rolls back an earlier movement.
        """
        _emit(progress, f"commit de {len(items)} mouvement(s)")
        results: List[Dict[str, Any]] = []
        applied = 0
        for position, item in enumerate(items, start=1):
            if progress:
                progress("réattribution", position, len(items))
            try:
                outcome = self._commit_one(db, item=item, user_id=user_id)
                applied += 1 if outcome["applied"] else 0
            except Exception as exc:  # noqa: BLE001 — reported per movement
                db.rollback()
                logger.exception("[rcp] commit failed for %s", getattr(item, "msgid", "?"))
                outcome = {
                    "msgid": getattr(item, "msgid", ""),
                    "applied": False,
                    "error": str(exc),
                    "targets": [],
                }
            if outcome["applied"]:
                _emit(progress, f"{outcome['msgid']} : réattribué sur "
                                f"{len(outcome.get('targets') or [])} cible(s)")
            else:
                _emit(progress, f"{outcome['msgid']} : REFUSÉ — {outcome.get('error')}")
            results.append(outcome)
        _emit(progress, f"commit terminé : {applied} appliqué(s), {len(results) - applied} en échec")
        return {
            "applied": applied,
            "failed": len(results) - applied,
            "results": results,
        }

    def _commit_one(self, db: Session, *, item: Any, user_id: Optional[int]) -> Dict[str, Any]:
        msgid = (item.msgid or "").strip()

        def reject(why: str) -> Dict[str, Any]:
            return {"msgid": msgid, "applied": False, "error": why, "targets": []}

        if not msgid or not item.targets:
            return reject("aucune cible")

        entry = (
            db.query(ReconciliationEntry)
            .filter(ReconciliationEntry.source_hash == item.entry_source_hash)
            .one_or_none()
        )
        # REPLAY. A movement committed once has no live row left — it was
        # withdrawn on purpose and the re-ingestion guard keeps it that way. Its
        # ``movement_split`` row still describes it whole, which is enough to
        # re-emit the group: the ghosts keep their identities (a pure function of
        # claim × bucket, re-anchored on the STORED canonical), their amounts are
        # refreshed, and the slices that disappeared are reaped. Only an RCPLINK
        # parent is replayable here — the DAG owns its own groups.
        parent: Optional[MovementSplit] = None
        if entry is None:
            parent = (
                db.query(MovementSplit)
                .filter(
                    MovementSplit.source_hash == item.entry_source_hash,
                    MovementSplit.claim_key_type == CLAIM_TYPE,
                )
                .one_or_none()
            )
            if parent is None:
                return reject("mouvement introuvable (déjà retiré ou émargé)")
            if parent.claim_key_value != msgid.upper():
                return reject("le split visé porte un autre msgid")
        source_of = entry if entry is not None else parent
        if entry is not None and entry.status != EntryStatus.PENDING:
            return reject(f"mouvement en statut {entry.status.value}, attendu PENDING")
        if msgid.upper() not in (source_of.transaction_particulars or "").upper():
            return reject("le msgid ne figure plus dans le mouvement visé")

        if entry is not None:
            run = (
                db.query(IngestionRun)
                .filter(IngestionRun.id == entry.ingestion_run_id)
                .one_or_none()
            )
            if run is None or run.flow_source_id is None:
                return reject("source d'ingestion introuvable pour ce mouvement")
            flow_source_id, run_id = run.flow_source_id, entry.ingestion_run_id
        else:
            # No ingestion run behind a replay: the ghosts keep the run that
            # created them (upsert_ghost_entries only refreshes it when given one).
            flow_source_id, run_id = parent.flow_source_id, None
        source = db.query(FlowSource).filter(FlowSource.id == flow_source_id).one_or_none()
        flow = db.query(Flow).filter(Flow.id == source_of.flow_id).one_or_none()
        if source is None or flow is None:
            return reject("flow/source introuvable")

        # WHICH BRANCH is re-derived here from the movement's own source, never
        # taken from the payload: a client cannot make a classic movement land
        # in a lot, nor the reverse.
        to_lots = source.parser_type == ParserType.FINACLE_BATCH_BOOKING_TRUE
        target_ids = [t.target_id for t in item.targets]
        lots: Dict[str, Any] = {}
        if to_lots:
            lots = {
                row.id: row
                for row in db.query(MovementLot)
                .filter(MovementLot.id.in_(target_ids))
                .all()
            }
            missing = [t for t in target_ids if t not in lots]
            if missing:
                return reject(f"lot(s) inconnu(s): {', '.join(missing[:3])}")
            foreign = [t for t in target_ids if lots[t].flow_source_id != source.id]
            if foreign:
                return reject(f"lot(s) d'une autre source: {', '.join(foreign[:3])}")
        else:
            known = self.known_reco_ids(db, flow_id=source_of.flow_id, reco_ids=target_ids)
            unknown = [t for t in target_ids if t not in known]
            if unknown:
                return reject(
                    f"clé(s) reco que rien ne porte dans le flux: {', '.join(unknown[:3])}"
                )

        booked = Decimal(source_of.amount)
        sign, total, slice_error = self.validate_slices(
            booked, [Decimal(t.amount) for t in item.targets]
        )
        if slice_error:
            return reject(slice_error)

        claim = (CLAIM_TYPE, msgid.upper())
        # ``event_type`` is the one field ``movement_split`` does not store; on a
        # replay it comes back from a ghost of the group (they all carry the
        # group's). Absent one, None — the column is nullable and
        # ``upsert_ghost_entries`` never rewrites it on an existing ghost.
        event_type = (
            entry.event_type if entry is not None
            else self._group_event_type(db, parent)
        )
        movement_type = _movement_type(source_of.transaction_particulars)
        direction = _direction_value(source_of.direction)

        children: List[SplitChildIn] = []
        members: List[LotMemberIn] = []
        for target in item.targets:
            lot = lots.get(target.target_id)
            # A ghost's identity is (claim, bucket). A classic target has no
            # bucket, so its reconciliation key stands in as the PO component —
            # still a pure function of the destination, still stable across
            # re-commits.
            key = (
                BucketKey(
                    lot.bucket_kind,
                    pacs=lot.bucket_pacs008,
                    msgid=lot.bucket_msgid,
                    po=lot.bucket_po,
                    ref=lot.bucket_ref,
                )
                if lot is not None
                else BucketKey(TARGET_RECO, po=target.target_id)
            )
            external_ref = ghost_external_ref(claim, key)
            amount = q2(sign * abs(Decimal(target.amount)))
            pos = [p for p in (target.pos or []) if p]
            # The count is authoritative, the list is not: analyze drops a list
            # longer than MAX_PO_KEYS (nothing would index it anyway).
            payment_count = target.payment_count or len(pos)
            children.append(
                SplitChildIn(
                    external_ref=external_ref,
                    lot_id=target.target_id,
                    amount=amount,
                    direction=direction,
                    payment_count=payment_count,
                    bucket_kind=lot.bucket_kind if lot is not None else "",
                    bucket_pacs008=lot.bucket_pacs008 if lot is not None else "",
                    bucket_msgid=lot.bucket_msgid if lot is not None else "",
                    bucket_po=lot.bucket_po if lot is not None else "",
                )
            )
            if lot is None:
                continue  # classic flow: no lot, therefore no lot member
            members.append(
                LotMemberIn(
                    lot_id=lot.id,
                    movement_type=movement_type,
                    external_ref=external_ref,
                    account=source_of.account,
                    currency=source_of.currency,
                    amount=amount,
                    value_date=source_of.value_date,
                    operation_date=source_of.operation_date,
                    direction=direction,
                    transaction_particulars=source_of.transaction_particulars,
                    ref_no=source_of.ref_no,
                    remarks_1=source_of.remarks_1,
                    claim_key_type=CLAIM_TYPE,
                    claim_key_value=msgid.upper(),
                    payment_count=payment_count,
                    keys=self._member_keys(key, pos),
                )
            )

        group = SplitGroupIn(
            claim_key_type=CLAIM_TYPE,
            claim_key_value=msgid.upper(),
            account=source_of.account,
            currency=source_of.currency,
            value_date=source_of.value_date,
            operation_date=source_of.operation_date,
            event_type=event_type,
            parents=[
                SplitParentIn(
                    movement_type=movement_type,
                    external_ref=source_of.external_ref,
                    account=source_of.account,
                    currency=source_of.currency,
                    amount=booked,
                    direction=direction,
                    value_date=source_of.value_date,
                    operation_date=source_of.operation_date,
                    transaction_particulars=source_of.transaction_particulars,
                    ref_no=source_of.ref_no,
                    remarks_1=source_of.remarks_1,
                    event_type=event_type,
                    transaction_id=getattr(entry, "transaction_id", None),
                    payload_raw=source_of.payload_raw,
                    payment_count=sum(c.payment_count for c in children),
                    payment_amount=q2(sign * total),
                    shared_key_movements=1,
                )
            ],
            children=children,
        )

        split_result = split_service.apply_split_batch(
            db, flow=flow, source=source, groups=[group], run_id=run_id
        )
        # No members on a classic flow: the ghosts ARE the reconciliation there,
        # there is nothing to attach them to.
        lot_result = (
            lot_service.apply_lot_batch(db, flow=flow, source=source, lots=[], members=members)
            if members
            else {}
        )

        details = {
            "msgid": msgid,
            "entry_source_hash": item.entry_source_hash,
            "booked_amount": str(booked),
            "ghost_total": str(q2(sign * total)),
            "target_kind": TARGET_LOT if to_lots else TARGET_RECO,
            "flow_code": flow.code,
            "targets": [
                {"target_id": c.lot_id, "amount": str(c.amount), "payments": c.payment_count}
                for c in children
            ],
            "split": split_result,
            "lot_batch": lot_result,
        }
        audit_service.log_ui_action(
            db,
            user_id=user_id,
            action="rcp_reattribution_commit",
            target_type="movement_split",
            target_id=msgid[:64],
            details=details,
        )
        logger.info(
            "[rcp] %s réattribué (%s): %d cible(s), %s / %s booké",
            msgid, TARGET_LOT if to_lots else TARGET_RECO,
            len(children), q2(sign * total), booked,
        )
        return {
            "msgid": msgid,
            "applied": True,
            "error": "",
            "ghost_total": q2(sign * total),
            "booked_amount": booked,
            "parents_emarged": split_result.get("parents_emarged", 0),
            "targets": [
                {
                    "target_id": c.lot_id,
                    "target_kind": TARGET_LOT if to_lots else TARGET_RECO,
                    "amount": c.amount,
                    "payment_count": c.payment_count,
                }
                for c in children
            ],
        }

    @staticmethod
    def _group_event_type(db: Session, parent: Any) -> Optional[str]:
        """The ``event_type`` the group's ghosts already carry, if any.

        ``movement_split`` does not store it, so on a replay it is read back off
        a ghost rather than guessed. None is a valid answer: the column is
        nullable, and an existing ghost keeps its own value whatever we send.
        """
        for child in movement_split_repository.list_children(
            db, parent_hash=parent.source_hash
        ):
            if child.event_type:
                return child.event_type
        return None

    @staticmethod
    def validate_slices(
        booked: Decimal, amounts: Sequence[Decimal]
    ) -> Tuple[Decimal, Decimal, str]:
        """(sign, total, error) for a movement's ghosts.

        A ghost is signed like the movement it slices — the client only ever
        sends the magnitude the files add up to, so no payload can flip a
        credit into a debit. The slices may fall SHORT of the booked amount (a
        partial split: some payments never found their lot, and the claim-group
        reconciliation will tag the gap), but they can never exceed it: that
        would mean the dumps claim more than the bank booked.
        """
        sign = Decimal("-1") if booked < 0 else Decimal("1")
        total = q2(sum((abs(a) for a in amounts), Decimal("0")))
        if not amounts:
            return sign, total, "aucun lot cible"
        if any(abs(a) <= 0 for a in amounts):
            return sign, total, "un lot cible porte un montant nul"
        if total > abs(booked):
            return sign, total, (
                f"les fantômes ({total}) dépassent le montant booké ({abs(booked)})"
            )
        return sign, total, ""

    @staticmethod
    def _member_keys(key: BucketKey, pos: Sequence[str]) -> List[LotKeyIn]:
        """Searchable keys of a ghost member: the target bucket's identity plus
        the returned payments' PO ids while they stay few enough to index
        (same cap as the DAG's MAX_PO_KEYS)."""
        keys: List[Tuple[str, str]] = []
        if key.pacs:
            keys.append(("PACS008", key.pacs))
        if key.msgid:
            keys.append(("MSGID", key.msgid))
        if key.po:
            keys.append(("PO", key.po))
        if 0 < len(pos) <= MAX_PO_KEYS:
            keys.extend(("PO", po.upper()) for po in pos)
        return [LotKeyIn(key_type=t, key_value=v) for t, v in sorted(set(keys))]



rcp_link_service = RcpLinkService()
