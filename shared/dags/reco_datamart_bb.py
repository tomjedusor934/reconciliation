"""Datamart extraction for BATCH BOOKING TRUE finacle sources (lot clustering).

On a batch-booking flow, N Paysis-side bulk movements (SCTXB/SDDXB/SDXBB) are
settled by M smaller Finacle-side aggregates (NDGB) that INTERLEAVE payments
across the N bulks — one movement no longer maps to one reconciliation key.
This module clusters movements into LOTS instead: each movement resolves a SET
of keys against ``std.Payment``, movements sharing any key belong to the same
lot (union-find, seeded with the lots persisted by previous runs), and every
entry is pushed with ``reco_id = lot uuid`` so the standard sum-to-zero engine
matches per lot with no engine change.

Key extraction per movement type (prefix of ``TransactionParticulars``):
- SCTXB/SDDXB/SDXBB direct (``PREFIX/I|O/...``): ``Remarks_1`` carries the
  pacs008 message id → key PACS008 + one MSGID key per distinct
  ``std.Payment.MessageID`` found via ``MessageIDPACS008 = Remarks_1``.
- SCTXB/SDDXB/SDXBB return (``PREFIX/NCC|NCP/...``): PO id in ``ref_no``
  (fallback: TP segment[3], the legacy bulk-return location) → key PO + the
  ORIGINAL bulk's PACS008 via the ``PaymentNumber`` lookup (business rule:
  returns reconcile inside the original bulk's lot, with its NDGB). The
  payment's MessageID is NOT taken: it adds no link and was pure glue in the
  52k mega-lot. Watch lot growth with ``RECO_FINACLE_BB_TRACE_KEYS``.
- NDGB (the Finacle aggregate): ``Remarks_1`` carries the ``MessageID`` → key
  MSGID + per payment found via ``MessageID = Remarks_1``: PACS008 when
  non-empty, else PO (= ``PaymentNumber``, the underlying single SWIFT/BKRTP).
- NDRJ (reject of any payment type): PO id in ``ref_no`` = ``paysis##<po_id>``
  → key PO + MSGID/PACS008 via ``PaymentNumber`` lookup.
- SWIFT/SCRT1/BKRTP instant payments: with a ``ref_no`` they are singles → key
  PO = ``ref_no``, no lookup. WITHOUT one, the movement is the AGGREGATE of a
  bulk-booked IP (multiline & co) or a reversal: TP segment[1] is the payment's
  ``TransactionRef`` → its ``MessageID`` → key MSGID + the same fan-out as NDGB
  (PACS008 when non-empty, else PO). That fan-out is what links the aggregate to
  its member singles, which carry only their PO key.
- NDRT (reject-of-return) and SP ``PREFIX/RCC/…`` rejects: ``ref_no`` is the
  RETURN's PaymentNumber in ``std.[Return]`` (reserved word — bracketed) →
  ``OriginalPo`` → original payment in ``std.Payment`` → keys: own return PO
  (pairs the NDRT↔RCC legs) + OriginalPo + original MSGID/PACS008.
- Anything else → reco_id "Not Supported" (never in a lot, retried by re-stream).

Label-like MessageIDs ('LUXEMBOURG', 'ESCH/ALZETTE'…) are KEPT as keys —
business links go through them — but ``degenerate_msgids`` logs them each run
(they are the usual suspects when a mega-lot shows up in LOT DEBUG).

A movement whose key set comes back EMPTY (e.g. SP direct with an empty
``Remarks_1``) gets ``reco_id = None`` — transient, re-enriched through
``/tasks/finacle/unresolved`` on every run, exactly like the legacy extractor.

Entries follow the regular ``/tasks/finacle/runs`` lifecycle (upsert may update
reco_id while the entry is PENDING); the lots/members/keys land in dedicated
tables via ``/tasks/finacle-bb/lots/*`` AFTER the entries, so a failed lot push
fails the run and the next run self-heals (same clustering, idempotent writes).
"""
import json
import logging
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from reco_common import (
    finacle_bb_get_key_map,
    finacle_bb_post_lot_batch,
    finacle_complete_run,
    finacle_list_unresolved,
    finacle_post_batch,
    finacle_start_run,
    task_list_finacle_sources,
)
from reco_datamart import (
    BULK_RETURN_SEGS,
    CHUNK_SIZE,
    DATAMART_CONN_ID,
    INSTANT_PREFIXES,
    PO_INSERT_BATCH,
    UNRESOLVED_RECO_ID,
    _channel_id,
    _clean,
    _compute_since,
    _dm_ref_no,
    _dm_remarks_1,
    _tp_parts,
    get_pyodbc_conn,
    iter_movement_chunks,
    movement_row_to_entry,
    resolve_reversals,
    reversal_ref_for,
)

logger = logging.getLogger(__name__)

PARSER_TYPE_BB = "finacle_batch_booking_true"

SP_BULK_PREFIXES = {"SCTXB", "SDDXB", "SDXBB"}  # Paysis-side bulks
# Instant payments (SWIFT/SCRT1/BKRTP) come from INSTANT_PREFIXES, imported from
# reco_datamart so both parsers share ONE list: with a ref_no they are singles
# keyed by it (PO), without one they are the aggregate/reversal shape resolved
# through TransactionRef.
NDGB_PREFIX = "NDGB"                            # Finacle-side aggregate
NDRJ_PREFIX = "NDRJ"                            # reject (any payment type)
NDRT_PREFIX = "NDRT"                            # reject-of-return, via std.[Return]
BULK_REJECT_SEGS = {"RCC"}                      # PREFIX/RCC/… reject shape, via std.[Return]

KEY_PACS008 = "PACS008"
KEY_MSGID = "MSGID"
KEY_PO = "PO"

BB_CHUNK_SIZE = int(os.environ.get("RECO_FINACLE_BB_CHUNK_SIZE", str(CHUNK_SIZE)))
# Members per lot POST. Each one is a full backend transaction, so fewer/bigger
# beats more/smaller: a 600k-member run is 60 round-trips instead of 300. The
# backend chunks the key INSERT itself (KEY_INSERT_CHUNK), so a high-fan-out
# member in the batch no longer builds one oversized statement.
MEMBER_PUSH_BATCH = int(os.environ.get("RECO_FINACLE_BB_MEMBER_BATCH", "10000"))
# A MessageID mapped to more than this many DISTINCT pacs008 is a recurring
# label ('ESCH/ALZETTE', 'ADEM-VIR<ts>'…), not an identifier — never a key.
MSGID_MAX_PACS = int(os.environ.get("RECO_FINACLE_BB_MSGID_MAX_PACS", "3"))
BB_DEBUG_SAMPLE = int(os.environ.get("RECO_FINACLE_BB_DEBUG_SAMPLE", "10"))
BB_DEBUG_TOP_KEYS = int(os.environ.get("RECO_FINACLE_BB_DEBUG_TOP_KEYS", "15"))
BB_DEBUG_SAMPLE_KEYS = int(os.environ.get("RECO_FINACLE_BB_DEBUG_SAMPLE_KEYS", "20"))

# A key is a (type, value) tuple; a movement resolves a frozenset of them.
Key = Tuple[str, str]


# ---------------------------------------------------------------------------
# Pure functions (unit-tested without any I/O)
# ---------------------------------------------------------------------------

def classify_bb_movement(row: Dict[str, Any]) -> Optional[str]:
    """Movement type handled by the BB flow, from the TP prefix; None for
    anything else (empty TP, MOSEL/Webripost plain text, GL, unknown)."""
    parts = _tp_parts(row)
    if not parts:
        return None
    prefix = parts[0].strip().upper()
    if (
        prefix in SP_BULK_PREFIXES
        or prefix in INSTANT_PREFIXES
        or prefix in (NDGB_PREFIX, NDRJ_PREFIX, NDRT_PREFIX)
    ):
        return prefix
    return None


def ndrj_po_id(row: Dict[str, Any]) -> Optional[str]:
    """PO id of a reject: ``ref_no`` formatted ``paysis##<po_id>`` → segment
    after the LAST '##'; a ref_no without '##' is taken as the PO id itself."""
    ref = _clean(_dm_ref_no(row))
    if not ref:
        return None
    if "##" in ref:
        return _clean(ref.split("##")[-1])
    return ref


def sp_return_po_id(row: Dict[str, Any], parts: Optional[List[str]] = None) -> Optional[str]:
    """PO id of an SP bulk RETURN (``PREFIX/NCC|NCP/...``): primarily ``ref_no``,
    falling back to TP segment[3] (the legacy bulk-return location)."""
    ref = _clean(_dm_ref_no(row))
    if ref:
        return _clean(ref.split("##")[-1]) if "##" in ref else ref
    parts = _tp_parts(row) if parts is None else parts
    if len(parts) >= 4:
        return _clean(parts[3])
    return None


@dataclass
class BBLookupInputs:
    """Distinct values needing a std.Payment / std.[Return] lookup, per key family."""
    sp_pacs008: Set[str] = field(default_factory=set)  # SP direct Remarks_1
    ndgb_msgids: Set[str] = field(default_factory=set)  # NDGB Remarks_1
    po_ids: Set[str] = field(default_factory=set)       # NDRJ + SP returns
    return_po_ids: Set[str] = field(default_factory=set)  # NDRT + SP /RCC rejects
    instant_txn_refs: Set[str] = field(default_factory=set)  # IP aggregate/reversal TP seg[1]


def collect_lookup_inputs(rows: Iterable[Dict[str, Any]], acc: BBLookupInputs) -> None:
    """Accumulate lookup inputs from raw std.Movement rows OR app entry dicts
    (the ``_tp_parts`` / ``_dm_*`` lowercase fallbacks make both shapes work)."""
    for row in rows:
        parts = _tp_parts(row)
        if not parts:
            continue
        prefix = parts[0].strip().upper()
        seg1 = parts[1].strip().upper() if len(parts) > 1 else ""
        if prefix in SP_BULK_PREFIXES:
            if seg1 in BULK_REJECT_SEGS:
                po = sp_return_po_id(row, parts)
                if po:
                    acc.return_po_ids.add(po)
            elif seg1 in BULK_RETURN_SEGS:
                po = sp_return_po_id(row, parts)
                if po:
                    acc.po_ids.add(po)
            else:
                pacs = _clean(_dm_remarks_1(row))
                if pacs:
                    acc.sp_pacs008.add(pacs)
        elif prefix == NDGB_PREFIX:
            msgid = _clean(_dm_remarks_1(row))
            if msgid:
                acc.ndgb_msgids.add(msgid)
        elif prefix == NDRJ_PREFIX:
            po = ndrj_po_id(row)
            if po:
                acc.po_ids.add(po)
        elif prefix == NDRT_PREFIX:
            po = ndrj_po_id(row)
            if po:
                acc.return_po_ids.add(po)
        elif prefix in INSTANT_PREFIXES:
            # An IP with a ref_no is a single (keyed by it, no lookup). Without one
            # it is the aggregate/reversal shape: TP segment[1] is the payment's
            # TransactionRef. ``reversal_ref_for`` already carries every exclusion
            # (NCC/NCP shapes, NDRT, instant WITH a ref_no…).
            ref = reversal_ref_for(row)
            if ref:
                acc.instant_txn_refs.add(ref)


def degenerate_msgids(
    pacs_map: Dict[str, List[str]],
    msgid_map: Dict[str, List[Tuple[Optional[str], Optional[str]]]],
    *,
    max_pacs: int = MSGID_MAX_PACS,
) -> Set[str]:
    """MessageIDs that look like recurring LABELS, not identifiers: associated
    with more than ``max_pacs`` distinct pacs008 across std.Payment. Observed
    in prod: 'ESCH/ALZETTE', 'ADEM-VIR<timestamp>', 'LUXEMBOURG'. LOG-ONLY —
    they stay valid keys (business links legitimately go through them, e.g. a
    700M NDGB), but knowing them explains mega-lots at a glance."""
    pacs_of: Dict[str, Set[str]] = {}
    for pacs, msgids in pacs_map.items():
        for msgid in msgids:
            pacs_of.setdefault(msgid, set()).add(pacs)
    for msgid, entries in msgid_map.items():
        for pacs, _po in entries:
            if pacs:
                pacs_of.setdefault(msgid, set()).add(pacs)
    return {m for m, ps in pacs_of.items() if len(ps) > max_pacs}


def movement_keys(
    row: Dict[str, Any],
    pacs_map: Dict[str, List[str]],
    msgid_map: Dict[str, List[Tuple[Optional[str], Optional[str]]]],
    po_map: Dict[str, List[Tuple[Optional[str], Optional[str]]]],
    return_map: Optional[Dict[str, List[Tuple[Optional[str], Optional[str], Optional[str]]]]] = None,
    txnref_map: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[FrozenSet[Key]]:
    """Key set of one movement.

    Returns None for movement types outside the BB flow ('Not Supported'),
    an EMPTY frozenset when the type is known but no key could be extracted
    yet (transient — retried next run), else the (type, value) key set.
    ``pacs_map``: pacs008 -> [MessageID...] ; ``msgid_map``: MessageID ->
    [(pacs008|None, po|None)...] ; ``po_map``: po -> [(MessageID|None,
    pacs008|None)...] ; ``return_map``: return PaymentNumber ->
    [(OriginalPo|None, MessageID|None, pacs008|None)...] (std.[Return]) ;
    ``txnref_map``: TransactionRef -> MessageID (std.Payment), for the IP
    aggregates that carry no ref_no.
    """
    parts = _tp_parts(row)
    if not parts:
        return None
    prefix = parts[0].strip().upper()
    seg1 = parts[1].strip().upper() if len(parts) > 1 else ""

    if prefix in SP_BULK_PREFIXES:
        if seg1 in BULK_REJECT_SEGS:
            # Reject of a return (SP side, PREFIX/RCC/…): same std.[Return]
            # indirection as NDRT.
            return _return_keys(sp_return_po_id(row, parts), return_map)
        if seg1 in BULK_RETURN_SEGS:
            # Return of one payment (NCC/NCP): its own PO + the ORIGINAL
            # bulk's PACS008 (business rule: returns reconcile inside the
            # original bulk's lot, with its NDGB) — but never the payment's
            # MessageID: it adds no link (NDGB reach the bulk through the
            # pacs) and was pure glue in the 52k mega-lot.
            po = sp_return_po_id(row, parts)
            if not po:
                return frozenset()
            keys = {(KEY_PO, po)}
            for _msgid, pacs in po_map.get(po, []):
                if pacs:
                    keys.add((KEY_PACS008, pacs))
            return frozenset(keys)
        # Direct (I/O) — and any other SP shape: Remarks_1 carries the pacs008
        # (a reversal hiding in the bulk flow has no Remarks_1 → empty set → retry).
        pacs = _clean(_dm_remarks_1(row))
        if not pacs:
            return frozenset()
        keys = {(KEY_PACS008, pacs)}
        for msgid in pacs_map.get(pacs, []):
            keys.add((KEY_MSGID, msgid))
        return frozenset(keys)

    if prefix == NDGB_PREFIX:
        msgid = _clean(_dm_remarks_1(row))
        if not msgid:
            return frozenset()
        keys = {(KEY_MSGID, msgid)}
        for pacs, po in msgid_map.get(msgid, []):
            if pacs:
                keys.add((KEY_PACS008, pacs))
            elif po:
                # Empty pacs008 = the underlying payment is a SWIFT/BKRTP single,
                # linked to its movement through the PO key (= its ref_no).
                keys.add((KEY_PO, po))
        return frozenset(keys)

    if prefix == NDRJ_PREFIX:
        po = ndrj_po_id(row)
        if not po:
            return frozenset()
        keys = {(KEY_PO, po)}
        for msgid, pacs in po_map.get(po, []):
            if msgid:
                keys.add((KEY_MSGID, msgid))
            if pacs:
                keys.add((KEY_PACS008, pacs))
        return frozenset(keys)

    if prefix == NDRT_PREFIX:
        return _return_keys(ndrj_po_id(row), return_map)

    if prefix in INSTANT_PREFIXES:
        ref = _clean(_dm_ref_no(row))
        if ref:
            return frozenset({(KEY_PO, ref)})         # single keyed by its PO
        # No ref_no → the AGGREGATE of a bulk-booked IP (multiline & co), or a
        # reversal hiding in the IP flow. TP segment[1] is the payment's
        # TransactionRef; its MessageID names the group, then the SAME fan-out as
        # NDGB — an empty pacs008 means the members are singles, reachable only
        # through their PO key. That fan-out IS the link to them.
        txn_ref = reversal_ref_for(row)
        if not txn_ref:
            return frozenset()
        msgid = _clean((txnref_map or {}).get(txn_ref))
        if not msgid:
            return frozenset()                        # not in std.Payment yet → retry
        keys = {(KEY_MSGID, msgid)}
        for pacs, po in msgid_map.get(msgid, []):
            if pacs:
                keys.add((KEY_PACS008, pacs))
            elif po:
                keys.add((KEY_PO, po))
        return frozenset(keys)

    return None


def _return_keys(
    po: Optional[str],
    return_map: Optional[Dict[str, List[Tuple[Optional[str], Optional[str], Optional[str]]]]],
) -> FrozenSet[Key]:
    """Keys of a reject-of-return (NDRT / ``PREFIX/RCC/…``): its own return PO
    (pairs the NDRT↔RCC legs of the same std.[Return] row), the OriginalPo
    (links the original single movement keyed by that PaymentNumber), and the
    original payment's MSGID + PACS008 (links the original bulk's lot).
    Business note: the dominant link is expected to be the PACS008 for NDRT
    (like NDRJ) and the MSGID for /RCC (classic bulk-return lots) — the
    union-find treats every key the same."""
    if not po:
        return frozenset()
    keys = {(KEY_PO, po)}
    for orig_po, msgid, pacs in (return_map or {}).get(po, []):
        if orig_po:
            keys.add((KEY_PO, orig_po))
        if msgid:
            keys.add((KEY_MSGID, msgid))
        if pacs:
            keys.add((KEY_PACS008, pacs))
    return frozenset(keys)


def bb_reco_for(
    keys: Optional[FrozenSet[Key]], key_to_lot: Dict[Key, str]
) -> Optional[str]:
    """reco_id of one movement given the cluster plan: 'Not Supported' for
    non-BB types, None when keyless or unknown to the plan (a row that appeared
    between the two streaming passes — retried next run), else the lot uuid."""
    if keys is None:
        return UNRESOLVED_RECO_ID
    if not keys:
        return None
    for key in keys:
        lot_id = key_to_lot.get(key)
        if lot_id:
            return lot_id
    return None


class UnionFind:
    """Minimal DSU with path compression (union by attachment)."""

    def __init__(self) -> None:
        self.parent: Dict[Any, Any] = {}

    def add(self, x: Any) -> None:
        self.parent.setdefault(x, x)

    def find(self, x: Any) -> Any:
        self.add(x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


@dataclass
class ClusterPlan:
    key_to_lot: Dict[Key, str]           # every seen key -> FINAL lot uuid
    new_lots: List[str]                   # lot uuids to create
    merges: List[Dict[str, str]]          # [{"absorbed_lot_id", "surviving_lot_id"}]


def build_clusters(
    key_sets: Iterable[FrozenSet[Key]],
    existing: Dict[Key, Tuple[str, str]],
) -> ClusterPlan:
    """Union-find over movement key sets, seeded with the persisted key map.

    ``existing``: key -> (lot_id, lot_created_at ISO). Keys already belonging
    to the same lot are kept together through a synthetic per-lot anchor node
    (even when no new movement bridges them). Per connected component: the
    OLDEST existing lot survives (tiebreak on lot_id), other existing lots are
    absorbed into it, and a component with no existing lot gets a new uuid4.
    Deterministic for identical inputs.
    """
    dsu = UnionFind()
    for key, (lot_id, _created) in existing.items():
        dsu.union(key, ("__lot__", lot_id))
    all_keys: Set[Key] = set(existing.keys())
    for keys in key_sets:
        if not keys:
            continue
        ordered = sorted(keys)
        all_keys.update(ordered)
        first = ordered[0]
        for key in ordered[1:]:
            dsu.union(first, key)

    comp_keys: Dict[Any, List[Key]] = {}
    for key in all_keys:
        comp_keys.setdefault(dsu.find(key), []).append(key)
    comp_lots: Dict[Any, Dict[str, str]] = {}
    for key, (lot_id, created) in existing.items():
        comp_lots.setdefault(dsu.find(key), {})[lot_id] = created or ""

    key_to_lot: Dict[Key, str] = {}
    new_lots: List[str] = []
    merges: List[Dict[str, str]] = []
    for root in sorted(comp_keys, key=str):  # deterministic component order
        lots = comp_lots.get(root)
        if lots:
            survivor = min(lots.items(), key=lambda item: (item[1], item[0]))[0]
            for lot_id in sorted(lots):
                if lot_id != survivor:
                    merges.append(
                        {"absorbed_lot_id": lot_id, "surviving_lot_id": survivor}
                    )
        else:
            survivor = str(uuid.uuid4())
            new_lots.append(survivor)
        for key in comp_keys[root]:
            key_to_lot[key] = survivor
    return ClusterPlan(key_to_lot=key_to_lot, new_lots=new_lots, merges=merges)


def _descriptor(row: Dict[str, Any]) -> Dict[str, Any]:
    """The fields key extraction needs — kept per movement across pass 1 so
    clustering can run before the pass-2 re-stream (memory: a few short strings
    per movement).

    ``Initiating_channel`` is here because ``reversal_ref_for`` reads it: pass 1
    builds the plan from descriptors while pass 2 re-keys the RAW rows, so any
    field one sees and the other doesn't would yield different key sets — and a
    movement whose pass-2 keys are absent from the plan resolves to None.
    """
    return {
        "TransactionParticulars": row.get("TransactionParticulars")
        or row.get("transaction_particulars"),
        "PaymentOrderID_Ref": _dm_ref_no(row),
        "Remarks_1": _dm_remarks_1(row),
        "Initiating_channel": _channel_id(row),
    }


def _build_member(
    entry: Dict[str, Any], movement_type: str, keys: FrozenSet[Key], lot_id: str
) -> Dict[str, Any]:
    """Member payload from the ALREADY-BUILT entry dict — the backend recomputes
    the finacle source_hash from these very fields, guaranteeing hash parity
    with the entry push."""
    return {
        "lot_id": lot_id,
        "movement_type": movement_type,
        "external_ref": entry.get("external_ref"),
        "account": entry.get("account"),
        "currency": entry.get("currency"),
        "amount": entry.get("amount"),
        "value_date": entry.get("value_date"),
        "operation_date": entry.get("operation_date"),
        "direction": entry.get("direction"),
        "transaction_particulars": entry.get("transaction_particulars"),
        "ref_no": entry.get("ref_no"),
        "remarks_1": entry.get("remarks_1"),
        "keys": [{"key_type": kt, "key_value": kv} for kt, kv in sorted(keys)],
    }


def _safe_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _single_lot_report(
    lot_id: str,
    lot_members: List[Dict[str, Any]],
    *,
    top_keys: int = BB_DEBUG_TOP_KEYS,
    sample: int = BB_DEBUG_SAMPLE,
    sample_keys: int = BB_DEBUG_SAMPLE_KEYS,
) -> Dict[str, Any]:
    """Deep dive on ONE lot's members — shared by the biggest-lot debug report
    and the traced-key reports. The key-degree ranking exposes a degenerate
    key gluing unrelated movements, the net amount shows why the lot never
    sums to zero, and ``sample_members`` are the member dicts EXACTLY as
    pushed to /tasks/finacle-bb/lots/batch. ``keys`` inside samples are
    truncated — one SP bulk member can carry thousands of key rows."""
    by_type = Counter(m["movement_type"] for m in lot_members)
    amount_by_type: Dict[str, Decimal] = {}
    net = Decimal("0")
    for m in lot_members:
        amt = _safe_decimal(m.get("amount"))
        amount_by_type[m["movement_type"]] = (
            amount_by_type.get(m["movement_type"], Decimal("0")) + amt
        )
        net += amt
    value_dates = sorted(str(m["value_date"]) for m in lot_members if m.get("value_date"))

    key_degree: Counter = Counter()
    distinct_by_type: Dict[str, Set[str]] = {}
    for m in lot_members:
        for k in m["keys"]:
            key_degree[(k["key_type"], k["key_value"])] += 1
            distinct_by_type.setdefault(k["key_type"], set()).add(k["key_value"])
    top = key_degree.most_common(top_keys)
    carriers: Dict[Key, Counter] = {key: Counter() for key, _ in top}
    for m in lot_members:
        for k in m["keys"]:
            key = (k["key_type"], k["key_value"])
            if key in carriers:
                carriers[key][m["movement_type"]] += 1
    histogram: Counter = Counter()
    for deg in key_degree.values():
        histogram["1" if deg == 1 else "2" if deg == 2 else "3-10" if deg <= 10 else ">10"] += 1

    def _sample_member(m: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(m)
        keys = m.get("keys") or []
        if len(keys) > sample_keys:
            out["keys"] = keys[:sample_keys]
            out["keys_truncated"] = len(keys) - sample_keys
        return out

    return {
        "lot_id": lot_id,
        "members": len(lot_members),
        "by_type": dict(by_type),
        "amount_by_type": {t: str(a) for t, a in sorted(amount_by_type.items())},
        "net_amount": str(net),
        "value_date_min": value_dates[0] if value_dates else None,
        "value_date_max": value_dates[-1] if value_dates else None,
        "distinct_keys_by_type": {t: len(v) for t, v in sorted(distinct_by_type.items())},
        "key_degree_histogram": dict(histogram),
        "top_keys_by_degree": [
            {"key": f"{kt}:{kv}", "members": deg, "by_type": dict(carriers[(kt, kv)])}
            for (kt, kv), deg in top
        ],
        "sample_members": [_sample_member(m) for m in lot_members[:sample]],
    }


def lot_debug_report(
    members_buf: List[Dict[str, Any]],
    plan: ClusterPlan,
    *,
    top_lots: int = 5,
    top_keys: int = BB_DEBUG_TOP_KEYS,
    sample: int = BB_DEBUG_SAMPLE,
    sample_keys: int = BB_DEBUG_SAMPLE_KEYS,
) -> Optional[Dict[str, Any]]:
    """End-of-run debug report (pure — unit-tested without I/O): lot sizes +
    a deep dive on the BIGGEST lot of the run (see ``_single_lot_report``)."""
    if not members_buf:
        return None
    sizes = Counter(m["lot_id"] for m in members_buf)
    biggest_id, _ = sizes.most_common(1)[0]
    big = [m for m in members_buf if m["lot_id"] == biggest_id]

    report = _single_lot_report(
        biggest_id, big, top_keys=top_keys, sample=sample, sample_keys=sample_keys
    )
    report["is_new"] = biggest_id in set(plan.new_lots)
    report["merges_as_survivor"] = sum(
        1 for mg in plan.merges if mg["surviving_lot_id"] == biggest_id
    )
    return {
        "lots_total": len(sizes),
        "members_total": len(members_buf),
        "top_lots": [{"lot_id": lid, "members": n} for lid, n in sizes.most_common(top_lots)],
        "biggest_lot": report,
    }


def parse_trace_keys(raw: str) -> List[Key]:
    """'PACS008:26070…,PO:123' → [(type, value), …]. Values may contain ':'
    (MSGIDs with timestamps): only the FIRST colon splits."""
    keys: List[Key] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        kt, kv = part.split(":", 1)
        if kt.strip() and kv.strip():
            keys.append((kt.strip().upper(), kv.strip()))
    return keys


def trace_key_reports(
    members_buf: List[Dict[str, Any]],
    plan: ClusterPlan,
    traced_keys: List[Key],
    *,
    top_keys: int = BB_DEBUG_TOP_KEYS,
    sample: int = BB_DEBUG_SAMPLE,
    sample_keys: int = BB_DEBUG_SAMPLE_KEYS,
    max_bridges: int = 20,
) -> List[Dict[str, Any]]:
    """One report per traced key (env RECO_FINACLE_BB_TRACE_KEYS): the lot
    that carries it, its full deep dive, plus the CHAINING diagnostics — who
    carries the traced key, and the bridge members (carrying ≥ 2 distinct
    PACS008) that explain why the lot is bigger than the expected
    one-bulk perimeter."""
    reports: List[Dict[str, Any]] = []
    members_by_lot: Dict[str, List[Dict[str, Any]]] = {}
    for m in members_buf:
        members_by_lot.setdefault(m["lot_id"], []).append(m)

    for kt, kv in traced_keys:
        label = f"{kt}:{kv}"
        lot_id = plan.key_to_lot.get((kt, kv))
        if not lot_id:
            reports.append({"traced_key": label, "found": False,
                            "note": "key unknown to this run's cluster plan"})
            continue
        lot_members = members_by_lot.get(lot_id)
        if not lot_members:
            reports.append({"traced_key": label, "found": False, "lot_id": lot_id,
                            "note": "lot known from the key map but no member "
                                    "of it was pushed this run"})
            continue

        report = _single_lot_report(
            lot_id, lot_members, top_keys=top_keys, sample=sample, sample_keys=sample_keys
        )
        report["traced_key"] = label
        report["found"] = True
        report["traced_key_carriers_by_type"] = dict(Counter(
            m["movement_type"] for m in lot_members
            if any(k["key_type"] == kt and k["key_value"] == kv for k in m["keys"])
        ))
        bridges = []
        for m in lot_members:
            pacs = sorted({k["key_value"] for k in m["keys"] if k["key_type"] == KEY_PACS008})
            if len(pacs) >= 2:
                bridges.append({
                    "movement_type": m["movement_type"],
                    "external_ref": m.get("external_ref"),
                    "pacs_count": len(pacs),
                    "pacs": pacs[:10],
                })
        bridges.sort(key=lambda b: (-b["pacs_count"], b["external_ref"] or ""))
        report["bridge_members_total"] = len(bridges)
        report["bridge_members"] = bridges[:max_bridges]
        reports.append(report)
    return reports


# ---------------------------------------------------------------------------
# std.Payment lookups (batched temp-table joins, mirroring resolve_bulk_returns)
# ---------------------------------------------------------------------------

def _temp_join(lookup_conn, values, *, temp_name: str, join_sql: str) -> List[tuple]:
    """Load distinct values into a temp table and run ONE join against
    std.Payment (single scan instead of thousands of IN queries)."""
    vals = [v for v in {_clean(v) for v in values} if v]
    if not vals:
        return []
    cursor = lookup_conn.cursor()
    try:
        cursor.execute(f"IF OBJECT_ID('tempdb..{temp_name}') IS NOT NULL DROP TABLE {temp_name}")
        cursor.execute(f"CREATE TABLE {temp_name} (k VARCHAR(128) PRIMARY KEY)")
        cursor.fast_executemany = True
        for i in range(0, len(vals), PO_INSERT_BATCH):
            cursor.executemany(
                f"INSERT INTO {temp_name} (k) VALUES (?)",
                [(v,) for v in vals[i : i + PO_INSERT_BATCH]],
            )
        cursor.execute(join_sql)
        rows = cursor.fetchall()
        cursor.execute(f"IF OBJECT_ID('tempdb..{temp_name}') IS NOT NULL DROP TABLE {temp_name}")
        return rows
    finally:
        cursor.close()


def resolve_sp_message_ids(lookup_conn, pacs008_ids) -> Dict[str, List[str]]:
    """pacs008 -> [distinct non-empty std.Payment.MessageID] (SP direct bulks)."""
    rows = _temp_join(
        lookup_conn,
        pacs008_ids,
        temp_name="#reco_bb_pacs",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.MessageID)) AS msgid "
            "FROM #reco_bb_pacs t "
            "INNER JOIN std.Payment p ON p.MessageIDPACS008 = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result: Dict[str, Set[str]] = {}
    for pacs, msgid in rows:
        pacs, msgid = _clean(pacs), _clean(msgid)
        if pacs and msgid:
            result.setdefault(pacs, set()).add(msgid)
    logger.info(
        "[ingest_finacle_bb] resolved MessageIDs for %d/%d pacs008 ids",
        len(result), len({_clean(p) for p in pacs008_ids if _clean(p)}),
    )
    return {k: sorted(v) for k, v in result.items()}


def resolve_ndgb_payments(
    lookup_conn, msgids
) -> Dict[str, List[Tuple[Optional[str], Optional[str]]]]:
    """MessageID -> [(pacs008|None, PaymentNumber|None)] (NDGB aggregates)."""
    rows = _temp_join(
        lookup_conn,
        msgids,
        temp_name="#reco_bb_msg",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs, "
            "       LTRIM(RTRIM(p.PaymentNumber)) AS po "
            "FROM #reco_bb_msg t "
            "INNER JOIN std.Payment p ON p.MessageID = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result: Dict[str, Set[Tuple[Optional[str], Optional[str]]]] = {}
    for msgid, pacs, po in rows:
        msgid = _clean(msgid)
        if msgid:
            result.setdefault(msgid, set()).add((_clean(pacs), _clean(po)))
    logger.info(
        "[ingest_finacle_bb] resolved payments for %d/%d NDGB MessageIDs",
        len(result), len({_clean(m) for m in msgids if _clean(m)}),
    )
    return {k: sorted(v, key=lambda t: (t[0] or "", t[1] or "")) for k, v in result.items()}


def resolve_return_payments(
    lookup_conn, return_po_ids
) -> Dict[str, List[Tuple[Optional[str], Optional[str], Optional[str]]]]:
    """Return PaymentNumber -> [(OriginalPo, MessageID, pacs008)] via
    std.[Return] (reserved word — bracketed) then the ORIGINAL payment.
    LEFT JOIN on std.Payment: a Return row whose original payment is not in
    std.Payment yet still yields its OriginalPo key."""
    rows = _temp_join(
        lookup_conn,
        return_po_ids,
        temp_name="#reco_bb_ret",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(r.OriginalPo)) AS orig_po, "
            "       LTRIM(RTRIM(p.MessageID)) AS msgid, "
            "       LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs "
            "FROM #reco_bb_ret t "
            "INNER JOIN std.[Return] r ON r.PaymentNumber = t.k "
            "LEFT JOIN std.Payment p "
            "  ON p.PaymentNumber = r.OriginalPo AND p.IsCurrent = 1 "
            "WHERE r.IsCurrent = 1"
        ),
    )
    result: Dict[str, Set[Tuple[Optional[str], Optional[str], Optional[str]]]] = {}
    for ret_po, orig_po, msgid, pacs in rows:
        ret_po = _clean(ret_po)
        if ret_po:
            result.setdefault(ret_po, set()).add(
                (_clean(orig_po), _clean(msgid), _clean(pacs))
            )
    logger.info(
        "[ingest_finacle_bb] resolved returns for %d/%d return PO ids",
        len(result), len({_clean(p) for p in return_po_ids if _clean(p)}),
    )
    return {
        k: sorted(v, key=lambda t: (t[0] or "", t[1] or "", t[2] or ""))
        for k, v in result.items()
    }


def resolve_po_payments(
    lookup_conn, po_ids
) -> Dict[str, List[Tuple[Optional[str], Optional[str]]]]:
    """PaymentNumber -> [(MessageID|None, pacs008|None)] (NDRJ + SP returns)."""
    rows = _temp_join(
        lookup_conn,
        po_ids,
        temp_name="#reco_bb_po",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.MessageID)) AS msgid, "
            "       LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs "
            "FROM #reco_bb_po t "
            "INNER JOIN std.Payment p ON p.PaymentNumber = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result: Dict[str, Set[Tuple[Optional[str], Optional[str]]]] = {}
    for po, msgid, pacs in rows:
        po = _clean(po)
        if po:
            result.setdefault(po, set()).add((_clean(msgid), _clean(pacs)))
    logger.info(
        "[ingest_finacle_bb] resolved payments for %d/%d PO ids",
        len(result), len({_clean(p) for p in po_ids if _clean(p)}),
    )
    return {k: sorted(v, key=lambda t: (t[0] or "", t[1] or "")) for k, v in result.items()}


# ---------------------------------------------------------------------------
# Orchestration (shared by ingest_finacle_bb and orchestrate_ingestion DAGs)
# ---------------------------------------------------------------------------

def _ingest_bb_source(conn, lookup_conn, source: Dict[str, Any], dag_run_id: Optional[str]) -> int:
    """One BB source, in two streaming passes over std.Movement.

    Pass 1 keeps a light descriptor per movement and accumulates the lookup
    inputs; three temp-table joins resolve every key family at once; the
    union-find (seeded with the backend's persisted key map) produces the
    cluster plan; pass 2 re-streams, pushes entries with reco_id = lot uuid and
    buffers the members; the lots/merges/members are pushed LAST so a failed
    lot push fails the run (idempotent re-run heals). ``conn`` streams
    std.Movement; ``lookup_conn`` runs the temp-table joins (a single MSSQL
    connection can't have a second active command mid-stream).
    """
    flow_code, source_code = source["flow_code"], source["source_code"]
    accounts = source.get("accounts") or []
    since = _compute_since(source.get("last_success_at"), source.get("backfill_since"))
    logger.info(
        "[ingest_finacle_bb] %s/%s: extracting %d account(s) since %s",
        flow_code, source_code, len(accounts), since.isoformat(),
    )
    run_id = finacle_start_run(flow_code, source_code, dag_run_id)
    total = 0
    try:
        # Entries the app still couldn't resolve (reco_id NULL / "Not Supported").
        unresolved = finacle_list_unresolved(flow_code, source_code).get("entries", [])

        # Pass 1 — light descriptors + lookup inputs (fresh movements + retries).
        descriptors: List[Dict[str, Any]] = []
        for chunk in iter_movement_chunks(
            conn, accounts=accounts, since=since, chunk_size=BB_CHUNK_SIZE
        ):
            descriptors.extend(_descriptor(row) for row in chunk)
        inputs = BBLookupInputs()
        collect_lookup_inputs(descriptors, inputs)
        collect_lookup_inputs(unresolved, inputs)

        # One temp-table join per key family resolves everything at once.
        pacs_map = resolve_sp_message_ids(lookup_conn, inputs.sp_pacs008)
        # The IP aggregates' TransactionRef yields their MessageID, which must be
        # in the msgid_map for the fan-out to reach the bulk's payments — hence
        # this join BEFORE resolve_ndgb_payments, whose input it feeds.
        txnref_map, _ = resolve_reversals(lookup_conn, inputs.instant_txn_refs)
        msgid_map = resolve_ndgb_payments(
            lookup_conn, inputs.ndgb_msgids | {m for m in txnref_map.values() if m}
        )
        po_map = resolve_po_payments(lookup_conn, inputs.po_ids)
        return_map = resolve_return_payments(lookup_conn, inputs.return_po_ids)

        # Observability ONLY — label-like MessageIDs ('LUXEMBOURG',
        # 'ESCH/ALZETTE'…) are KEPT as keys: business links go through them
        # (a 700M NDGB reconciles via one), but they are the usual suspects
        # when a mega-lot shows up in LOT DEBUG.
        labels = degenerate_msgids(pacs_map, msgid_map)
        if labels:
            logger.warning(
                "[ingest_finacle_bb] %s/%s: %d label-like MessageID(s) kept as keys, e.g. %s",
                flow_code, source_code, len(labels), sorted(labels)[:5],
            )

        # Clustering, seeded with the persisted key map (lots span runs).
        existing_raw = finacle_bb_get_key_map(source["source_id"]).get("keys", [])
        existing: Dict[Key, Tuple[str, str]] = {
            (k["key_type"], k["key_value"]): (k["lot_id"], k.get("lot_created_at") or "")
            for k in existing_raw
        }
        key_sets = []
        for record in descriptors + list(unresolved):
            keys = movement_keys(record, pacs_map, msgid_map, po_map, return_map, txnref_map=txnref_map)
            if keys:
                key_sets.append(keys)
        plan = build_clusters(key_sets, existing)
        logger.info(
            "[ingest_finacle_bb] %s/%s: %d key set(s) -> %d new lot(s), %d merge(s) "
            "(%d existing keys)",
            flow_code, source_code, len(key_sets), len(plan.new_lots),
            len(plan.merges), len(existing),
        )

        # Pass 2 — re-stream, push entries (reco_id = final lot uuid), buffer members.
        members_buf: List[Dict[str, Any]] = []
        for chunk in iter_movement_chunks(
            conn, accounts=accounts, since=since, chunk_size=BB_CHUNK_SIZE
        ):
            entries, errors = [], []
            for row in chunk:
                keys = movement_keys(row, pacs_map, msgid_map, po_map, return_map, txnref_map=txnref_map)
                reco_id = bb_reco_for(keys, plan.key_to_lot)
                try:
                    entry = movement_row_to_entry(row, reco_id)
                except ValueError as exc:
                    errors.append(f"TransactionID={row.get('TransactionID')}: {exc}")
                    continue
                entries.append(entry)
                if keys and reco_id:
                    members_buf.append(
                        _build_member(entry, classify_bb_movement(row) or "?", keys, reco_id)
                    )
            finacle_post_batch(run_id, entries, errors)
            total += len(entries)

        # Retry — re-enrich the app's unresolved entries and re-push (upsert in
        # place); the ones that now resolve also become lot members.
        for i in range(0, len(unresolved), BB_CHUNK_SIZE):
            batch = []
            for entry in unresolved[i : i + BB_CHUNK_SIZE]:
                keys = movement_keys(entry, pacs_map, msgid_map, po_map, return_map, txnref_map=txnref_map)
                out = dict(entry)
                out["reco_id"] = bb_reco_for(keys, plan.key_to_lot)
                batch.append(out)
                if keys and out["reco_id"]:
                    members_buf.append(
                        _build_member(out, classify_bb_movement(entry) or "?", keys, out["reco_id"])
                    )
            finacle_post_batch(run_id, batch, [])
            total += len(batch)

        # End-of-run debug: biggest lot, key degrees, exact pushed member
        # format — logged BEFORE the push so a failed lot push still leaves
        # the report in the Airflow logs.
        report = lot_debug_report(members_buf, plan)
        if report:
            logger.info(
                "[ingest_finacle_bb] %s/%s: LOT DEBUG %s",
                flow_code, source_code,
                json.dumps(report, default=str, ensure_ascii=False),
            )
        traced = parse_trace_keys(os.environ.get("RECO_FINACLE_BB_TRACE_KEYS", ""))
        for trace in trace_key_reports(members_buf, plan, traced):
            logger.info(
                "[ingest_finacle_bb] %s/%s: LOT TRACE %s",
                flow_code, source_code,
                json.dumps(trace, default=str, ensure_ascii=False),
            )

        # Lot push — lots + merges with the first member chunk, then the rest.
        # Only lots actually referenced by members are created (an entry that
        # errored out never creates an empty lot).
        used_lots = {m["lot_id"] for m in members_buf}
        lots_payload = [{"lot_id": lot_id} for lot_id in plan.new_lots if lot_id in used_lots]
        base = {"flow_code": flow_code, "source_code": source_code}
        finacle_bb_post_lot_batch(
            {**base, "lots": lots_payload, "merges": plan.merges,
             "members": members_buf[:MEMBER_PUSH_BATCH]}
        )
        for i in range(MEMBER_PUSH_BATCH, len(members_buf), MEMBER_PUSH_BATCH):
            finacle_bb_post_lot_batch(
                {**base, "lots": [], "merges": [],
                 "members": members_buf[i : i + MEMBER_PUSH_BATCH]}
            )

        finacle_complete_run(run_id)
        logger.info(
            "[ingest_finacle_bb] %s/%s: run #%s done (%d pushed incl. %d retried, "
            "%d member(s), %d lot(s) created, %d merge(s))",
            flow_code, source_code, run_id, total, len(unresolved),
            len(members_buf), len(lots_payload), len(plan.merges),
        )
    except Exception:
        try:
            finacle_complete_run(run_id, failed=True, error="see Airflow logs")
        except Exception:  # noqa: BLE001
            logger.warning("[ingest_finacle_bb] could not mark run #%s as failed", run_id)
        raise
    return total


def run_finacle_bb_ingestion(dag_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Extract + cluster movements for every active BATCH BOOKING TRUE source."""
    sources = [
        s
        for s in task_list_finacle_sources().get("sources", [])
        if s.get("parser_type") == PARSER_TYPE_BB
    ]
    summary: Dict[str, Any] = {"ingested": [], "skipped": [], "errors": []}
    if not sources:
        logger.info("[ingest_finacle_bb] No active batch-booking source — nothing to do.")
        return summary

    # Local import: keeps this module importable (tests, DAG parsing) without
    # the MSSQL provider installed.
    from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook

    hook = MsSqlHook(mssql_conn_id=DATAMART_CONN_ID)
    # Two connections: one streams std.Movement, the other runs std.Payment lookups.
    conn = get_pyodbc_conn(hook)
    lookup_conn = get_pyodbc_conn(hook)
    try:
        for source in sources:
            label = f"{source['flow_code']}/{source['source_code']}"
            if not (source.get("accounts") or []):
                logger.warning("[ingest_finacle_bb] %s has no reference account — skipped.", label)
                summary["skipped"].append(label)
                continue
            try:
                _ingest_bb_source(conn, lookup_conn, source, dag_run_id)
                summary["ingested"].append(label)
            except Exception as exc:  # noqa: BLE001
                logger.error("[ingest_finacle_bb] %s failed: %s", label, exc, exc_info=True)
                summary["errors"].append({"source": label, "error": str(exc)})
    finally:
        for c in (conn, lookup_conn):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    if summary["errors"]:
        raise RuntimeError(f"finacle BB ingestion finished with errors: {summary['errors']}")
    return summary
