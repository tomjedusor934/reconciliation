"""Reattribution of the movements the ingestion could not key at all.

WHY THIS EXISTS, next to rcp_link_service. That tool reattributes AGGREGATE
return legs whose msgid the datamart cannot link — it is fed an extract that
carries the missing link. This one has no extract: it takes the movements that
ended the ingestion with ``reco_id = 'Not Supported'`` (a known prefix in an
unhandled shape) or ``reco_id IS NULL`` (transient, nothing resolved yet), and
asks whether the movement ITSELF still names something the datamart can answer.

Measured on the outward float (flow 16) on 2026-08-24: 999 'Not Supported' and
172 NULL, and roughly 380 of them do name a key — a PaymentNumber in ``ref_no``,
a PaymentNumber in a return-shaped TransactionParticulars, or the TransactionID
of a movement already sitting in a lot ('RECTIF PF0008529'). The rest carry
nothing to look up: a card movement with an empty TP, or free text an operator
typed. Those are reported as such and stay for a human — this tool never
invents a link from an amount.

WHAT IT DOES NOT DO. It does not split. Every rule here yields ONE
PaymentNumber or one named movement, so an orphan has exactly one destination or
none; several candidates is an ambiguity to show, not a split to compute. The
movement is therefore RETARGETED (its ``reco_id`` set, and on a batch-booking
flow a lot member added), never withdrawn in favour of ghosts. That also means
it can only ever move a movement that is in no lot: the commit refuses anything
whose ``reco_id`` is not still the sentinel it was analysed with.

Nothing is written by ``analyze``. ``commit`` applies only the movements the
operator ticked, and re-validates each one against the database first.
"""
import logging
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.flow import Flow, FlowSource, ParserType
from app.models.ingestion_run import IngestionRun
from app.models.movement_lot import MovementLot, MovementLotMember
from app.models.reconciliation_entry import EntryStatus, ReconciliationEntry
from app.schemas.lot import LotKeyIn, LotMemberIn
from app.services.audit_service import audit_service
from app.services.lot_service import lot_service
from app.services.rcp_link_service import (
    ST_NO_TARGET,
    ST_PROPOSED,
    TARGET_LOT,
    TARGET_RECO,
    _emit,
    _movement_type,
    q2,
    rcp_link_service,
)

logger = logging.getLogger(__name__)

# The reco_id values that mean "the ingestion could not place this movement".
# 'Not Supported' is the parser's sentinel for a known prefix in an unhandled
# shape; NULL is its transient twin (nothing resolved YET, retried every run).
UNRESOLVED_RECO_ID = "Not Supported"

# Statuses of this tool, on top of the two it shares with rcp_link_service.
ST_NO_KEY = "NO_KEY"                  # nothing in the movement to look up
ST_KEY_AMBIGUOUS = "KEY_AMBIGUOUS"    # several candidates — a human decides
ORPHAN_ACTIONABLE = (ST_PROPOSED,)

# Which rule found the key. Shown to the operator as the EVIDENCE behind a
# proposal: a PaymentNumber read off ref_no is not the same claim as one scraped
# out of free text, and the operator must be able to tell them apart.
RULE_REF_NO = "REF_NO"                    # ref_no (after the last '##')
RULE_TP_RETURN = "TP_RETURN_SHAPE"        # PREFIX/<NCC|NCP|RRS|RCC|RCP>/I|O/<po>/…
RULE_TP_MOVEMENT = "TP_NAMES_MOVEMENT"    # 'RECTIF PF0008529' → that movement's lot
RULE_TP_DIGITS = "TP_DIGITS"              # a 6+ digit run in free text — weakest

KIND_PO = "PO"            # a std.Payment PaymentNumber, resolved on the datamart
KIND_MOVEMENT = "MOVEMENT"  # a TransactionID of a movement already in a lot

# Return/reject segments, mirrored from the parser. Kept as a literal rather
# than imported: shared/dags is not on the backend's path, and a segment added
# there is a parser fix, while adding it here only widens what the operator is
# OFFERED. The two lists drifting apart costs proposals, never correctness —
# every candidate is validated against the datamart before it becomes one.
TP_RETURN_SEGS = ("NCC", "NCP", "RRS", "RCC", "RCP")

# A movement's own identifier, as Finacle spells it in free text ('RECTIF
# PF0008529'). Anchored on a word boundary so an amount never matches.
_MOVEMENT_REF = re.compile(r"\b((?:PF|P|S)\d{6,})\b", re.IGNORECASE)
# A STANDALONE digit run — never a fragment of a longer token. Without the
# guards, 'SDDXBREJ/SDDXB260821000288645/…' offers '260821000288645', which is a
# TransactionRef with its prefix chopped off, not a PaymentNumber; the parser
# resolves that shape through std.Payment.TransactionRef, and this tool must not
# compete with it by handing the operator a mutilated key.
_DIGIT_RUN = re.compile(r"(?<![0-9A-Za-z])\d{6,}(?![0-9A-Za-z])")
_RETURN_SHAPE = re.compile(
    r"^[^/]*/(" + "|".join(TP_RETURN_SEGS) + r")/[IO]/([^/]{4,})", re.IGNORECASE
)


@dataclass(frozen=True)
class OrphanKey:
    """One thing a stranded movement still names, and how we know."""
    value: str
    kind: str   # KIND_PO | KIND_MOVEMENT
    rule: str


def orphan_keys(
    transaction_particulars: Optional[str], ref_no: Optional[str]
) -> List[OrphanKey]:
    """What a stranded movement still names — the FIRST rule that yields
    anything, in decreasing order of trust. Pure: no DB, no datamart.

    Order matters. ``ref_no`` is a field Finacle filled deliberately; a
    return-shaped TP is a documented convention; a TransactionID in free text is
    an operator's own reference; a bare digit run is a guess and comes last. The
    rules are NOT merged: a movement that answers a trusted rule must not be
    dragged into ambiguity by a digit run somewhere else in its label.
    """
    tp = (transaction_particulars or "").strip()
    ref = (ref_no or "").strip()
    if ref:
        value = ref.split("##")[-1].strip()
        if value:
            return [OrphanKey(value, KIND_PO, RULE_REF_NO)]

    match = _RETURN_SHAPE.match(tp)
    if match:
        return [OrphanKey(match.group(2).strip(), KIND_PO, RULE_TP_RETURN)]

    named = {m.group(1).upper() for m in _MOVEMENT_REF.finditer(tp)}
    if named:
        return [OrphanKey(v, KIND_MOVEMENT, RULE_TP_MOVEMENT) for v in sorted(named)]

    digits = {m.group(0) for m in _DIGIT_RUN.finditer(tp)}
    if digits:
        return [OrphanKey(v, KIND_PO, RULE_TP_DIGITS) for v in sorted(digits)]
    return []


class RcpOrphanService:
    # -- analysis ------------------------------------------------------

    def analyze(
        self,
        db: Session,
        *,
        flow_id: int,
        connection_id: Optional[int] = None,
        limit: int = 5000,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """List the flow's stranded movements and, for each, where it belongs.
        Writes nothing."""
        def step(phase: str, done: int = 0, total: int = 0) -> None:
            if progress:
                progress(phase, done, total)

        step("lecture des mouvements non rattachés")
        rows = self._orphan_entries(db, flow_id=flow_id, limit=limit)
        _emit(progress, f"{len(rows)} mouvement(s) sans lot dans le flux {flow_id}")

        # Phase 1 — what each movement still names. Pure, no I/O.
        step("extraction des clés", 0, len(rows))
        keyed: List[Tuple[Any, Any, List[OrphanKey]]] = [
            (entry, source, orphan_keys(entry.transaction_particulars, entry.ref_no))
            for entry, source in rows
        ]
        by_rule: Dict[str, int] = {}
        for _, _, keys in keyed:
            by_rule[keys[0].rule if keys else ST_NO_KEY] = (
                by_rule.get(keys[0].rule if keys else ST_NO_KEY, 0) + 1
            )
        _emit(
            progress,
            "clés extraites : " + ", ".join(f"{k}={v}" for k, v in sorted(by_rule.items())),
        )

        # Phase 2 — ONE datamart round-trip for every PaymentNumber candidate of
        # the run, then one target lookup per source. Same shape as the link
        # analysis: the cost is per RUN, never per movement.
        pos = sorted({k.value for _, _, keys in keyed for k in keys if k.kind == KIND_PO})
        step("interrogation du datamart", 0, len(pos))
        payments, datamart_error = rcp_link_service.resolve_payments(
            db, connection_id, pos, progress
        )
        targets_by_source: Dict[int, Dict[str, Dict[str, Any]]] = {}

        def targets_for(source: Any, entry_flow_id: int) -> Dict[str, Dict[str, Any]]:
            if source.id not in targets_by_source:
                targets_by_source[source.id] = rcp_link_service.targets_for_pos(
                    db, pos, payments,
                    flow_id=entry_flow_id,
                    flow_source_id=source.id,
                    parser_type=source.parser_type,
                )
            return targets_by_source[source.id]

        # Phase 3 — the movements named in free text ('RECTIF PF0008529'): one
        # query for the whole run, resolved app-side (no datamart involved).
        named = sorted(
            {k.value for _, _, keys in keyed for k in keys if k.kind == KIND_MOVEMENT}
        )
        step("mouvements cités en clair", 0, len(named))
        lots_by_movement = self._lots_by_movement_ref(db, flow_id=flow_id, refs=named)
        if named:
            _emit(
                progress,
                f"mouvements cités : {len(lots_by_movement)}/{len(named)} retrouvés dans un lot",
            )

        step("résolution des cibles", 0, len(keyed))
        proposals: List[Dict[str, Any]] = []
        for position, (entry, source, keys) in enumerate(keyed, start=1):
            if position % 200 == 0:
                step("résolution des cibles", position, len(keyed))
            proposals.append(
                self._proposal(
                    entry, source, keys,
                    targets=targets_for(source, entry.flow_id) if keys else {},
                    lots_by_movement=lots_by_movement,
                    datamart_error=datamart_error,
                )
            )
        summary: Dict[str, int] = {"total": len(proposals)}
        for proposal in proposals:
            summary[proposal["status"]] = summary.get(proposal["status"], 0) + 1
        _emit(
            progress,
            "propositions : "
            + ", ".join(f"{k}={v}" for k, v in sorted(summary.items()) if k != "total"),
        )
        step("terminé")
        return {
            "flow_id": flow_id,
            "proposals": proposals,
            "summary": summary,
            "datamart_error": datamart_error,
        }

    @staticmethod
    def _orphan_entries(
        db: Session, *, flow_id: int, limit: int
    ) -> List[Tuple[Any, Any]]:
        """The flow's PENDING movements the ingestion could not place, with the
        source they came from — the source's ``parser_type`` is what decides
        whether a target is a lot or a reconciliation key."""
        return (
            db.query(ReconciliationEntry, FlowSource)
            .join(IngestionRun, ReconciliationEntry.ingestion_run_id == IngestionRun.id)
            .join(FlowSource, IngestionRun.flow_source_id == FlowSource.id)
            .filter(ReconciliationEntry.flow_id == flow_id)
            .filter(ReconciliationEntry.status == EntryStatus.PENDING)
            .filter(
                or_(
                    ReconciliationEntry.reco_id.is_(None),
                    ReconciliationEntry.reco_id == UNRESOLVED_RECO_ID,
                )
            )
            .order_by(ReconciliationEntry.value_date.desc(), ReconciliationEntry.id)
            .limit(limit)
            .all()
        )

    @staticmethod
    def _lots_by_movement_ref(
        db: Session, *, flow_id: int, refs: Sequence[str]
    ) -> Dict[str, List[Tuple[str, str]]]:
        """{TransactionID: [(lot_id, member external_ref)]} for the movements a
        free-text label names.

        Matched on the member's ``external_ref`` STRIPPED of its part-tran
        suffix: Finacle writes 'PF0008529#2' on the movement and 'PF0008529' in
        the label. A TransactionID landing in several lots is an ambiguity the
        operator resolves, never something to average out.
        """
        refs = [r for r in refs if r]
        if not refs:
            return {}
        wanted = {r.upper() for r in refs}
        found: Dict[str, List[Tuple[str, str]]] = {}
        conditions = [
            MovementLotMember.external_ref.ilike(f"{r}%") for r in sorted(wanted)
        ]
        rows = (
            db.query(MovementLotMember.lot_id, MovementLotMember.external_ref)
            .join(MovementLot, MovementLot.id == MovementLotMember.lot_id)
            .filter(MovementLot.flow_id == flow_id)
            .filter(or_(*conditions))
            .all()
        )
        for lot_id, external_ref in rows:
            base = (external_ref or "").split("#")[0].strip().upper()
            if base not in wanted:
                continue  # ILIKE 'PF001%' also matches PF0010…, the prefix is not the key
            hits = found.setdefault(base, [])
            if (lot_id, external_ref) not in hits:
                hits.append((lot_id, external_ref))
        return found

    def _proposal(
        self,
        entry: Any,
        source: Any,
        keys: Sequence[OrphanKey],
        *,
        targets: Dict[str, Dict[str, Any]],
        lots_by_movement: Dict[str, List[Tuple[str, str]]],
        datamart_error: str,
    ) -> Dict[str, Any]:
        """One movement's verdict — and the evidence behind it.

        ``rule`` and ``key`` are part of the payload on purpose: a proposal the
        operator cannot audit is a proposal they have to take on faith, and the
        weakest rule (a digit run in free text) must be visibly weaker than a
        PaymentNumber Finacle wrote into ``ref_no``.
        """
        to_lots = source.parser_type == ParserType.FINACLE_BATCH_BOOKING_TRUE
        proposal: Dict[str, Any] = {
            "source_hash": entry.source_hash,
            "entry_id": entry.id,
            "flow_id": entry.flow_id,
            "source_code": getattr(source, "code", "") or "",
            "reco_id": entry.reco_id,
            "amount": entry.amount,
            "currency": entry.currency,
            "direction": entry.direction,
            "value_date": entry.value_date,
            "external_ref": entry.external_ref,
            "transaction_particulars": entry.transaction_particulars,
            "ref_no": entry.ref_no,
            "remarks_1": entry.remarks_1,
            "rule": keys[0].rule if keys else "",
            "keys": [k.value for k in keys],
            "target_kind": TARGET_LOT if to_lots else TARGET_RECO,
            "target_id": "",
            "target_label": "",
            "candidates": [],
            "status": ST_NO_KEY,
            "message": "",
        }
        if not keys:
            proposal["message"] = "rien à chercher dans ce mouvement — arbitrage manuel"
            return proposal
        if len(keys) > 1:
            proposal["status"] = ST_KEY_AMBIGUOUS
            proposal["candidates"] = [k.value for k in keys]
            proposal["message"] = (
                f"{len(keys)} clés possibles ({keys[0].rule}) — arbitrage manuel"
            )
            return proposal

        key = keys[0]
        if key.kind == KIND_MOVEMENT:
            hits = lots_by_movement.get(key.value.upper(), [])
            if not hits:
                proposal["status"] = ST_NO_TARGET
                proposal["message"] = f"{key.value} n'est dans aucun lot du flux"
                return proposal
            if len({lot_id for lot_id, _ in hits}) > 1:
                proposal["status"] = ST_KEY_AMBIGUOUS
                proposal["candidates"] = sorted({lot_id for lot_id, _ in hits})
                proposal["message"] = f"{key.value} figure dans plusieurs lots"
                return proposal
            proposal["status"] = ST_PROPOSED
            proposal["target_id"] = hits[0][0]
            proposal["target_label"] = f"lot de {hits[0][1]}"
            proposal["message"] = f"rattaché au lot du mouvement {key.value}"
            return proposal

        resolution = targets.get(key.value)
        if resolution is None or not resolution.get("target_id"):
            proposal["status"] = ST_NO_TARGET
            reason = (resolution or {}).get("reason", "")
            proposal["message"] = (
                f"{key.value} : aucune cible ({reason or 'inconnu du datamart'})"
                + (f" — {datamart_error}" if datamart_error else "")
            )
            return proposal
        proposal["status"] = ST_PROPOSED
        proposal["target_id"] = resolution["target_id"]
        proposal["target_label"] = resolution.get("label", resolution["target_id"])
        proposal["target_kind"] = resolution.get(
            "target_kind", TARGET_LOT if to_lots else TARGET_RECO
        )
        proposal["message"] = f"{key.value} → {proposal['target_label']}"
        return proposal

    # -- commit --------------------------------------------------------

    def commit(
        self,
        db: Session,
        *,
        items: Sequence[Any],
        user_id: Optional[int] = None,
        progress: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Retarget the ticked movements. One transaction each: a movement lands
        whole or not at all, and a later failure never undoes an earlier one."""
        _emit(progress, f"rattachement de {len(items)} mouvement(s)")
        results: List[Dict[str, Any]] = []
        applied = 0
        for position, item in enumerate(items, start=1):
            if progress:
                progress("rattachement", position, len(items))
            try:
                outcome = self._commit_one(db, item=item, user_id=user_id)
                applied += 1 if outcome["applied"] else 0
            except Exception as exc:  # noqa: BLE001 — reported per movement
                db.rollback()
                logger.exception(
                    "[rcp-orphan] commit failed for %s",
                    getattr(item, "source_hash", "?"),
                )
                outcome = {
                    "source_hash": getattr(item, "source_hash", ""),
                    "applied": False,
                    "error": str(exc),
                    "target_id": getattr(item, "target_id", ""),
                }
            if not outcome["applied"]:
                _emit(progress, f"{outcome['source_hash'][:12]} : REFUSÉ — {outcome.get('error')}")
            results.append(outcome)
        _emit(
            progress,
            f"terminé : {applied} rattaché(s), {len(results) - applied} en échec",
        )
        return {"applied": applied, "failed": len(results) - applied, "results": results}

    def _commit_one(self, db: Session, *, item: Any, user_id: Optional[int]) -> Dict[str, Any]:
        target_id = (item.target_id or "").strip()

        def reject(why: str) -> Dict[str, Any]:
            return {
                "source_hash": item.source_hash,
                "applied": False,
                "error": why,
                "target_id": target_id,
            }

        if not target_id:
            return reject("aucune cible")
        entry = (
            db.query(ReconciliationEntry)
            .filter(ReconciliationEntry.source_hash == item.source_hash)
            .one_or_none()
        )
        if entry is None:
            return reject("mouvement introuvable (retiré, émargé, ou déjà rattaché)")
        if entry.status != EntryStatus.PENDING:
            return reject(f"mouvement en statut {entry.status.value}, attendu PENDING")
        # THE GUARD THAT MAKES THIS TOOL SAFE. Only a movement the ingestion left
        # stranded can be retargeted here. A movement already in a lot has been
        # placed by the parser, and moving it would silently unbalance the lot it
        # is sitting in — that is a parser fix, not an operator action.
        if entry.reco_id not in (None, "", UNRESOLVED_RECO_ID):
            return reject(f"mouvement déjà rattaché à {entry.reco_id} — non modifiable ici")

        run = (
            db.query(IngestionRun)
            .filter(IngestionRun.id == entry.ingestion_run_id)
            .one_or_none()
        )
        if run is None or run.flow_source_id is None:
            return reject("source d'ingestion introuvable pour ce mouvement")
        source = (
            db.query(FlowSource).filter(FlowSource.id == run.flow_source_id).one_or_none()
        )
        flow = db.query(Flow).filter(Flow.id == entry.flow_id).one_or_none()
        if source is None or flow is None:
            return reject("flow/source introuvable")

        # WHICH BRANCH is re-derived from the movement's own source, never taken
        # from the payload — same rule as the link commit.
        to_lots = source.parser_type == ParserType.FINACLE_BATCH_BOOKING_TRUE
        lot: Optional[Any] = None
        if to_lots:
            lot = (
                db.query(MovementLot).filter(MovementLot.id == target_id).one_or_none()
            )
            if lot is None:
                return reject(f"lot inconnu: {target_id}")
            if lot.flow_source_id != source.id:
                return reject("lot d'une autre source")
        else:
            known = rcp_link_service.known_reco_ids(
                db, flow_id=entry.flow_id, reco_ids=[target_id]
            )
            if target_id not in known:
                return reject("clé reco que rien ne porte dans le flux")

        previous = entry.reco_id
        members: List[LotMemberIn] = []
        if lot is not None:
            # A REAL movement, so no claim key: apply_lot_batch then recomputes
            # the member hash off the movement's own identity, which is exactly
            # the entry's source_hash (see test_lot_hash_parity) — the lot view
            # resolves the member's status through it.
            members.append(
                LotMemberIn(
                    lot_id=lot.id,
                    movement_type=_movement_type(entry.transaction_particulars),
                    external_ref=entry.external_ref,
                    account=entry.account,
                    currency=entry.currency,
                    amount=q2(Decimal(entry.amount)),
                    value_date=entry.value_date,
                    operation_date=entry.operation_date,
                    direction=entry.direction,
                    transaction_particulars=entry.transaction_particulars,
                    ref_no=entry.ref_no,
                    remarks_1=entry.remarks_1,
                    payment_count=1,
                    keys=self._member_keys(lot),
                )
            )
        # The member lands FIRST. If it fails, the movement is still stranded —
        # recoverable. The reverse order would leave an entry pointing at a lot
        # it is not a member of, which the lot view cannot explain.
        lot_result = (
            lot_service.apply_lot_batch(db, flow=flow, source=source, lots=[], members=members)
            if members
            else {}
        )
        entry.reco_id = target_id
        db.commit()
        audit_service.log_ui_action(
            db,
            user_id=user_id,
            action="rcp_orphan_commit",
            target_type="reconciliation_entry",
            target_id=str(entry.id),
            details={
                "source_hash": entry.source_hash,
                "previous_reco_id": previous,
                "target_id": target_id,
                "target_kind": TARGET_LOT if to_lots else TARGET_RECO,
                "rule": getattr(item, "rule", ""),
                "key": getattr(item, "key", ""),
                "flow_code": flow.code,
                "amount": str(entry.amount),
                "lot_batch": lot_result,
            },
        )
        logger.info(
            "[rcp-orphan] %s rattaché à %s (%s)",
            entry.source_hash[:12], target_id, TARGET_LOT if to_lots else TARGET_RECO,
        )
        return {
            "source_hash": entry.source_hash,
            "applied": True,
            "error": "",
            "target_id": target_id,
            "target_kind": TARGET_LOT if to_lots else TARGET_RECO,
        }

    @staticmethod
    def _member_keys(lot: Any) -> List[LotKeyIn]:
        """The bucket's own identity as searchable keys — what the deep search
        and the key drawer navigate. A real member adds nothing else."""
        keys: List[LotKeyIn] = []
        if lot.bucket_pacs008:
            keys.append(LotKeyIn(key_type="PACS008", key_value=lot.bucket_pacs008))
        if lot.bucket_msgid:
            keys.append(LotKeyIn(key_type="MSGID", key_value=lot.bucket_msgid))
        if lot.bucket_po:
            keys.append(LotKeyIn(key_type="PO", key_value=lot.bucket_po))
        return keys


rcp_orphan_service = RcpOrphanService()
