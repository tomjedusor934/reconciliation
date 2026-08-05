"""Datamart extraction for BATCH BOOKING TRUE finacle sources (bucket splitting).

On a batch-booking flow, N Paysis-side bulk movements (SCTXB/SDDXB/SDXBB) are
settled by M smaller Finacle-side aggregates (NDGB) that INTERLEAVE payments
across the N bulks — one movement no longer maps to one reconciliation key.

The reconciliation unit is the BUCKET: one pacs008 batch carries many payment
orders, those orders are grouped by their ``std.Payment.MessageID``, and each
(pacs008, MessageID) pair is one lot. A bucket's id is
``uuid5(ns, "<flow_source_id>|<kind>|<pacs>|<msgid>|<po>|<ref>")`` — a pure
function of its identity, so the same bucket always yields the same uuid and
this DAG names its lots without reading anything back from the backend.

This REPLACES the union-find over {PACS008, MSGID, PO} keys. That clustering was
transitive, and Finacle's MessageIDs are frequently labels rather than
identifiers ('LUXEMBOURG', 'ESCH/ALZETTE', 'ADEM-VIR<ts>'): one shared label
glued unrelated pacs008 together until a single lot held 50 898 movements,
35 pacs008 and 3 488 MessageIDs, and could never sum to zero. Pairing kills the
transitivity: (X, LUXEMBOURG) and (Y, LUXEMBOURG) are simply two buckets.

GHOST MOVEMENTS. A movement whose payments span several buckets belongs to none
of them: an SP bulk is ONE booking for a whole pacs008, and an NDGB carrying a
label MessageID spans every pacs008 that reused the label. Such a movement is
registered as a SPLIT PARENT and replaced by one ghost entry per bucket. The real
movement is then withdrawn from ``reconciliation_entry`` so it cannot double
count against its own ghosts.

The ghosts SHARE OUT the movement's own booked amount, weighted by each bucket's
payment sum (``allocate_amount``, largest-remainder to the cent). Payment sums
are weights, never amounts: pricing a ghost AT its bucket's payment sum let a
movement emit a slice unrelated to what the bank booked. In prod, 184 NDGB
movements resolved the same 20 000-payment group and each emitted a ghost worth
the FULL group, inflating one bucket to -334 M€. Allocation makes ``Σ ghosts ==
the booked amount`` true BY CONSTRUCTION, so no run can invent money.

Whether std.Payment agrees with the accounting is a separate question, kept as a
fact on the parent (``payment_amount`` vs ``amount``) rather than as a ghost — an
imaginary movement was the wrong place to carry a data-quality gap.

A consequence worth knowing: when BOTH sides of a bucket are ghosts, they are
weighted from the same std.Payment groups and cancel by construction — the lot
matches without proving anything. The backend flags those lots ``synthetic_only``
so the distinction stays visible.

Payment resolution per movement type (prefix of ``TransactionParticulars``):
- SCTXB/SDDXB/SDXBB direct (``PREFIX/I|O/...``): ``Remarks_1`` is the pacs008 →
  every payment with that ``MessageIDPACS008``. The one that really splits.
- SCTXB/SDDXB/SDXBB return (``PREFIX/NCC|NCP/...``): PO id in ``ref_no``
  (fallback: TP segment[3]) → that single payment → the ORIGINAL bulk's bucket.
- NDGB (the Finacle aggregate): ``Remarks_1`` is the ``MessageID`` → every
  payment under it. Splits when the MessageID is a label spanning pacs008.
- NDRJ (reject of any payment type): PO id in ``ref_no`` = ``paysis##<po_id>``.
- SWIFT/SCRT1/BKRTP instant payments: with a ``ref_no`` they are singles, keyed
  by it (bucket PO, no lookup). WITHOUT one, the movement is the AGGREGATE of a
  bulk-booked IP or a reversal: TP segment[1] is the payment's ``TransactionRef``
  → its ``MessageID`` → the same fan-out as NDGB.
- NDRT (reject-of-return) and SP ``PREFIX/RCC/…``: ``ref_no`` is the RETURN's
  PaymentNumber in ``std.[Return]`` (reserved word — bracketed) → ``OriginalPo``
  → the original payment's bucket.
- Anything else → reco_id "Not Supported" (never in a lot, retried by re-stream).

A movement that resolves NO payment falls back to the bucket its own fields
name (the pacs008 it quotes, its MessageID, its PO) so it still lands somewhere
and meets its counterpart later; only a movement with no usable field at all
gets ``reco_id = None`` — transient, re-enriched through
``/tasks/finacle/unresolved`` on every run.

Entries follow the regular ``/tasks/finacle/runs`` lifecycle. Splits are pushed
to ``/tasks/finacle-bb/splits/*`` FIRST (parents registered, ghosts created, real
movements withdrawn — one transaction), then the buckets and members to
``/tasks/finacle-bb/lots/*``, so no intermediate state has a movement and its
ghosts both counting.
"""
import hashlib
import json
import logging
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from reco_common import (
    finacle_bb_post_lot_batch,
    finacle_bb_post_split_batch,
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
    PAYMENT_AMOUNT_COL,
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
# keyed by it (bucket PO), without one they are the aggregate/reversal shape
# resolved through TransactionRef.
NDGB_PREFIX = "NDGB"                            # Finacle-side aggregate
NDRJ_PREFIX = "NDRJ"                            # reject (any payment type)
NDRT_PREFIX = "NDRT"                            # reject-of-return, via std.[Return]
BULK_REJECT_SEGS = {"RCC"}                      # PREFIX/RCC/… reject shape, via std.[Return]

KEY_PACS008 = "PACS008"
KEY_MSGID = "MSGID"
KEY_PO = "PO"

BUCKET_PAIR = "PAIR"
BUCKET_PACS_ONLY = "PACS_ONLY"
BUCKET_MSGID_ONLY = "MSGID_ONLY"
BUCKET_PO = "PO"
BUCKET_RESIDUAL = "RESIDUAL"

# Frozen namespace for the bucket uuid5. Changing it re-keys every lot in
# existence and orphans every reco_id already written — it is a constant, not a
# setting.
BUCKET_NAMESPACE = uuid.UUID("6f0d5c6e-4a2b-5f3d-9e18-2c7b41a0d5e9")

BB_CHUNK_SIZE = int(os.environ.get("RECO_FINACLE_BB_CHUNK_SIZE", str(CHUNK_SIZE)))
# Members per lot POST. Each one is a full backend transaction, so fewer/bigger
# beats more/smaller: a 600k-member run is 60 round-trips instead of 300. The
# backend chunks the key INSERT itself (KEY_INSERT_CHUNK).
MEMBER_PUSH_BATCH = int(os.environ.get("RECO_FINACLE_BB_MEMBER_BATCH", "10000"))
# GHOSTS per split POST — not parents. A parent is not a unit of constant cost:
# a pacs008 spread over ~100 MessageIDs yields ~100 ghosts, so batching 500
# parents meant 50 000 rows in one request and one client-interpolated INSERT
# (600s read timeout in prod, 2026-08-04). The batch must be sized by what it
# actually carries.
SPLIT_CHILD_BATCH = int(os.environ.get("RECO_FINACLE_BB_SPLIT_CHILD_BATCH", "2000"))
# A movement's aggregate key shared by more than this many movements is worth
# logging: it means Finacle booked one payment batch as many entries and every
# one of them resolves the whole group (see aggregate_key).
SHARED_KEY_WARN = int(os.environ.get("RECO_FINACLE_BB_SHARED_KEY_WARN", "1"))
# Payment PO ids emitted as searchable keys on a member. A bulk bucket holds
# thousands of them and they are NOT what navigates the graph (the bucket's own
# pacs008/MessageID are), so the fan-out is capped: singles, returns and rejects
# stay fully keyed, bulk buckets keep only their identity. Per-payment lookup
# inside a bulk is served by reco.entry_payment_status (po_id -> reco_id).
MAX_PO_KEYS = int(os.environ.get("RECO_FINACLE_BB_MAX_PO_KEYS", "50"))
BB_DEBUG_SAMPLE = int(os.environ.get("RECO_FINACLE_BB_DEBUG_SAMPLE", "10"))
BB_DEBUG_TOP_KEYS = int(os.environ.get("RECO_FINACLE_BB_DEBUG_TOP_KEYS", "15"))
BB_DEBUG_SAMPLE_KEYS = int(os.environ.get("RECO_FINACLE_BB_DEBUG_SAMPLE_KEYS", "20"))

# A key is a (type, value) tuple; kept as searchable metadata on a member.
Key = Tuple[str, str]


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PaymentRef:
    """One ``std.Payment`` row as this module needs it."""
    pacs: str
    msgid: str
    po: str
    amount: Decimal


@dataclass(frozen=True)
class BucketKey:
    """A bucket's identity. Absent components are '' — never None — so the value
    round-trips through the wire and the database unchanged (the uniqueness
    constraint over these columns would not bite on NULLs)."""
    kind: str
    pacs: str = ""
    msgid: str = ""
    po: str = ""
    ref: str = ""

    def payload(self, lot_id: str) -> Dict[str, Any]:
        return {
            "lot_id": lot_id,
            "bucket_kind": self.kind,
            "bucket_pacs008": self.pacs,
            "bucket_msgid": self.msgid,
            "bucket_po": self.po,
            "bucket_ref": self.ref,
        }

    def label(self) -> str:
        parts = [p for p in (self.pacs, self.msgid, self.po, self.ref) if p]
        return f"{self.kind}:{'|'.join(parts)}" if parts else self.kind


@dataclass
class PaymentGroup:
    """The payments of one movement that fall into one bucket."""
    amount: Decimal = Decimal("0")
    pos: List[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.pos)


@dataclass(frozen=True)
class MovementResolution:
    """What a movement resolved to. ``payments`` empty + ``fallback`` set means
    'known shape, std.Payment has nothing yet' — the movement still lands in the
    bucket its own fields name."""
    payments: Tuple[PaymentRef, ...]
    fallback: Optional[BucketKey]


@dataclass
class MovementPlan:
    """What to push for one movement."""
    entries: List[Dict[str, Any]] = field(default_factory=list)
    members: List[Dict[str, Any]] = field(default_factory=list)
    buckets: Dict[str, BucketKey] = field(default_factory=dict)
    parent: Optional[Dict[str, Any]] = None


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
    """Distinct values needing a std.Payment / std.[Return] lookup, per family."""
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
            # An IP with a ref_no is a single (bucket PO, no lookup). Without one
            # it is the aggregate/reversal shape: TP segment[1] is the payment's
            # TransactionRef. ``reversal_ref_for`` already carries every exclusion
            # (NCC/NCP shapes, NDRT, instant WITH a ref_no…).
            ref = reversal_ref_for(row)
            if ref:
                acc.instant_txn_refs.add(ref)


# ---------------------------------------------------------------------------
# Bucket identity
# ---------------------------------------------------------------------------

def bucket_id(flow_source_id: int, key: BucketKey) -> str:
    """The lot uuid for a bucket — a pure function of its identity.

    This is what makes the pipeline stateless: two runs, two batches, or a retry
    after a crash all name the same bucket identically, so there is nothing to
    cluster, merge, or read back.
    """
    raw = f"{flow_source_id}|{key.kind}|{key.pacs}|{key.msgid}|{key.po}|{key.ref}"
    return str(uuid.uuid5(BUCKET_NAMESPACE, raw))


def payment_bucket(pacs: Optional[str], msgid: Optional[str], po: Optional[str]) -> Optional[BucketKey]:
    """The bucket one payment belongs to.

    A payment with no pacs008 is a single (SWIFT/BKRTP): it is reached through
    its PaymentNumber, never its MessageID, because that is the only key its own
    movement carries. Hence PO taking precedence over MSGID here.
    """
    pacs, msgid, po = _clean(pacs) or "", _clean(msgid) or "", _clean(po) or ""
    if pacs and msgid:
        return BucketKey(BUCKET_PAIR, pacs=pacs, msgid=msgid)
    if pacs:
        return BucketKey(BUCKET_PACS_ONLY, pacs=pacs)
    if po:
        return BucketKey(BUCKET_PO, po=po)
    if msgid:
        return BucketKey(BUCKET_MSGID_ONLY, msgid=msgid)
    return None


def aggregate_key(row: Dict[str, Any]) -> Optional[Key]:
    """The lookup key a movement resolves its payments through.

    Counting these over a run tells how many movements share one key. Finacle
    books a 20 000-payment SDD batch as 184 separate entries carrying the same
    ``Remarks_1``, and each of them then resolves the WHOLE group — there is no
    key to divide it. The count does not change any behaviour (the allocation
    already bounds each movement by its own amount), it makes the situation
    visible instead of silent.

    Mirrors ``collect_lookup_inputs``' dispatch. The IP aggregate shape is left
    out: its key only exists after the TransactionRef lookup, and it is not the
    shape that collides.
    """
    parts = _tp_parts(row)
    if not parts:
        return None
    prefix = parts[0].strip().upper()
    seg1 = parts[1].strip().upper() if len(parts) > 1 else ""

    if prefix in SP_BULK_PREFIXES:
        if seg1 in BULK_REJECT_SEGS or seg1 in BULK_RETURN_SEGS:
            po = sp_return_po_id(row, parts)
            return (KEY_PO, po) if po else None
        pacs = _clean(_dm_remarks_1(row))
        return (KEY_PACS008, pacs) if pacs else None
    if prefix == NDGB_PREFIX:
        msgid = _clean(_dm_remarks_1(row))
        return (KEY_MSGID, msgid) if msgid else None
    if prefix in (NDRJ_PREFIX, NDRT_PREFIX):
        po = ndrj_po_id(row)
        return (KEY_PO, po) if po else None
    if prefix in INSTANT_PREFIXES:
        ref = _clean(_dm_ref_no(row))
        return (KEY_PO, ref) if ref else None
    return None


def count_aggregate_keys(rows: Iterable[Dict[str, Any]]) -> Counter:
    """{aggregate key: how many movements resolve through it}."""
    counts: Counter = Counter()
    for row in rows:
        key = aggregate_key(row)
        if key:
            counts[key] += 1
    return counts


def ghost_external_ref(parent_external_ref: Optional[str], key: BucketKey) -> str:
    """Identity of one ghost: its parent's, suffixed per bucket.

    Nothing else is needed — a ghost shares its parent's account and dates, so
    this alone makes the finacle hash differ, and the backend recovers the parent
    by hashing the prefix back. Bounded to fit ``external_ref`` VARCHAR(128).
    """
    raw = f"{key.kind}|{key.pacs}|{key.msgid}|{key.po}|{key.ref}"
    tag = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]
    return f"{(parent_external_ref or 'MID:?')[:100]}~{tag}"


# ---------------------------------------------------------------------------
# Payment resolution per movement
# ---------------------------------------------------------------------------

def movement_resolution(
    row: Dict[str, Any],
    pacs_map: Dict[str, List[Tuple[str, str, Decimal]]],
    msgid_map: Dict[str, List[Tuple[str, str, Decimal]]],
    po_map: Dict[str, List[Tuple[str, str, Decimal]]],
    return_map: Optional[Dict[str, List[Tuple[str, str, str, Decimal]]]] = None,
    txnref_map: Optional[Dict[str, Optional[str]]] = None,
) -> Optional[MovementResolution]:
    """The payments behind one movement, plus the bucket it falls back to.

    Returns None for movement types outside the BB flow ('Not Supported').
    A resolution with no payments AND no fallback is transient — the movement
    keeps ``reco_id = None`` and is retried next run.

    ``pacs_map``: pacs008 -> [(msgid, po, amount)] ; ``msgid_map``: MessageID ->
    [(pacs008, po, amount)] ; ``po_map``: po -> [(msgid, pacs008, amount)] ;
    ``return_map``: return PaymentNumber -> [(OriginalPo, msgid, pacs008,
    amount)] (std.[Return]) ; ``txnref_map``: TransactionRef -> MessageID, for
    the IP aggregates that carry no ref_no.
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
            return _return_resolution(sp_return_po_id(row, parts), return_map)
        if seg1 in BULK_RETURN_SEGS:
            # Return of one payment (NCC/NCP): it settles inside the ORIGINAL
            # bulk's bucket, which its PaymentNumber names.
            po = sp_return_po_id(row, parts)
            if not po:
                return MovementResolution((), None)
            payments = tuple(
                PaymentRef(pacs=pacs or "", msgid=msgid or "", po=po, amount=amount)
                for msgid, pacs, amount in po_map.get(po, [])
            )
            return MovementResolution(payments, BucketKey(BUCKET_PO, po=po))
        # Direct (I/O) — and any other SP shape: Remarks_1 carries the pacs008
        # (a reversal hiding in the bulk flow has no Remarks_1 → transient).
        pacs = _clean(_dm_remarks_1(row))
        if not pacs:
            return MovementResolution((), None)
        payments = tuple(
            PaymentRef(pacs=pacs, msgid=msgid or "", po=po or "", amount=amount)
            for msgid, po, amount in pacs_map.get(pacs, [])
        )
        return MovementResolution(payments, BucketKey(BUCKET_PACS_ONLY, pacs=pacs))

    if prefix == NDGB_PREFIX:
        msgid = _clean(_dm_remarks_1(row))
        if not msgid:
            return MovementResolution((), None)
        payments = tuple(
            PaymentRef(pacs=pacs or "", msgid=msgid, po=po or "", amount=amount)
            for pacs, po, amount in msgid_map.get(msgid, [])
        )
        return MovementResolution(payments, BucketKey(BUCKET_MSGID_ONLY, msgid=msgid))

    if prefix == NDRJ_PREFIX:
        po = ndrj_po_id(row)
        if not po:
            return MovementResolution((), None)
        payments = tuple(
            PaymentRef(pacs=pacs or "", msgid=msgid or "", po=po, amount=amount)
            for msgid, pacs, amount in po_map.get(po, [])
        )
        return MovementResolution(payments, BucketKey(BUCKET_PO, po=po))

    if prefix == NDRT_PREFIX:
        return _return_resolution(ndrj_po_id(row), return_map)

    if prefix in INSTANT_PREFIXES:
        ref = _clean(_dm_ref_no(row))
        if ref:
            # A single: its own PaymentNumber IS its bucket, no lookup needed.
            return MovementResolution((), BucketKey(BUCKET_PO, po=ref))
        # No ref_no → the AGGREGATE of a bulk-booked IP (multiline & co), or a
        # reversal hiding in the IP flow. TP segment[1] is the payment's
        # TransactionRef; its MessageID names the group, then the SAME fan-out as
        # NDGB — an empty pacs008 means the members are singles, reachable only
        # through their PO.
        txn_ref = reversal_ref_for(row)
        if not txn_ref:
            return MovementResolution((), None)
        msgid = _clean((txnref_map or {}).get(txn_ref))
        if not msgid:
            return MovementResolution((), None)  # not in std.Payment yet → retry
        payments = tuple(
            PaymentRef(pacs=pacs or "", msgid=msgid, po=po or "", amount=amount)
            for pacs, po, amount in msgid_map.get(msgid, [])
        )
        return MovementResolution(payments, BucketKey(BUCKET_MSGID_ONLY, msgid=msgid))

    return None


def _return_resolution(
    po: Optional[str],
    return_map: Optional[Dict[str, List[Tuple[str, str, str, Decimal]]]],
) -> MovementResolution:
    """Payments of a reject-of-return (NDRT / ``PREFIX/RCC/…``).

    The movement settles against the ORIGINAL payment, so it belongs in the
    original's bucket. Its own return PaymentNumber is the fallback: it pairs the
    NDRT↔RCC legs of the same std.[Return] row when std.Payment knows nothing.
    """
    if not po:
        return MovementResolution((), None)
    payments = tuple(
        PaymentRef(pacs=pacs or "", msgid=msgid or "", po=orig_po or po, amount=amount)
        for orig_po, msgid, pacs, amount in (return_map or {}).get(po, [])
    )
    return MovementResolution(payments, BucketKey(BUCKET_PO, po=po))


def partition_payments(payments: Iterable[PaymentRef]) -> Dict[BucketKey, PaymentGroup]:
    """Group a movement's payments by bucket. More than one group ⇒ the movement
    must be split, because no single bucket can hold it."""
    groups: Dict[BucketKey, PaymentGroup] = {}
    for payment in payments:
        key = payment_bucket(payment.pacs, payment.msgid, payment.po)
        if key is None:
            continue
        group = groups.setdefault(key, PaymentGroup())
        group.amount += payment.amount
        if payment.po:
            group.pos.append(payment.po)
    return groups


def bucket_keys(key: BucketKey, group: Optional[PaymentGroup]) -> List[Key]:
    """Searchable keys of a member: the bucket's own identity, plus its payment
    PO ids while they stay few enough to be worth indexing (see MAX_PO_KEYS)."""
    keys: List[Key] = []
    if key.pacs:
        keys.append((KEY_PACS008, key.pacs))
    if key.msgid:
        keys.append((KEY_MSGID, key.msgid))
    if key.po:
        keys.append((KEY_PO, key.po))
    if group and 0 < group.count <= MAX_PO_KEYS:
        keys.extend((KEY_PO, po) for po in group.pos)
    return sorted(set(keys))


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------

def allocate_amount(
    total: Decimal, groups: Dict[BucketKey, PaymentGroup]
) -> Tuple[List[Tuple[BucketKey, Decimal, PaymentGroup]], Decimal]:
    """Distribute the movement's OWN booked amount over its buckets, weighted by
    each bucket's payment sum → (slices, payment_amount).

    ``Σ slices == total`` EXACTLY — the movement's money is re-allocated, never
    invented. That distinction is the whole point: pricing a slice AT its
    bucket's payment sum let a movement emit a slice unrelated to what the bank
    actually booked. In prod, 184 NDGB movements all resolved the same 20 000
    payment group and each emitted a ghost worth the FULL group (-1 817 225,60),
    inflating one bucket to -334 M€ while a residual ghost "conserved" the total
    with an equally monstrous opposite. Payment sums are WEIGHTS, not amounts.

    ``payment_amount`` (Σ of the group sums, signed like the movement) is
    returned for reporting: comparing it to ``total`` is the real
    "does std.Payment agree with the accounting?" control, and it belongs on the
    parent rather than inside a bucket.

    Degenerate weights fall back gracefully: all-zero sums weigh by payment
    count, and failing that every bucket gets an equal share — a movement must
    always land somewhere.
    """
    ordered = sorted(groups.items(), key=lambda kv: (-abs(kv[1].amount), kv[0].label()))
    payment_amount = sum((abs(g.amount) for _k, g in ordered), Decimal("0"))
    if total < 0:
        payment_amount = -payment_amount

    weights = [abs(g.amount) for _k, g in ordered]
    if not sum(weights):
        weights = [Decimal(g.count) for _k, g in ordered]
    if not sum(weights):
        weights = [Decimal(1)] * len(ordered)

    shares = _largest_remainder(abs(total), weights, _quantum(total))
    sign = Decimal(-1) if total < 0 else Decimal(1)
    slices = [
        (key, sign * share, group)
        for (key, group), share in zip(ordered, shares)
    ]
    return slices, payment_amount


def _quantum(total: Decimal) -> Decimal:
    """Smallest unit the allocation may use: the movement's own precision, and
    never coarser than a cent (a movement booked as '1000' must still split into
    cents, or the remainder would land in whole euros)."""
    return Decimal(1).scaleb(min(total.as_tuple().exponent, -2))


def _largest_remainder(
    magnitude: Decimal, weights: List[Decimal], quantum: Decimal
) -> List[Decimal]:
    """Split ``magnitude`` proportionally to ``weights``, summing back EXACTLY.

    Floor every share to the quantum, then hand the leftover quanta out one by
    one to the largest fractional parts (ties by position, which the caller has
    already ordered deterministically). This is the standard largest-remainder
    apportionment — plain rounding would drift by a cent per bucket.
    """
    total_weight = sum(weights)
    exact = [magnitude * w / total_weight for w in weights]
    floors = [(e / quantum).to_integral_value(rounding=ROUND_FLOOR) * quantum for e in exact]
    leftover = magnitude - sum(floors)

    shares = list(floors)
    extra_units = int((leftover / quantum).to_integral_value(rounding=ROUND_HALF_UP))
    if extra_units > 0:
        order = sorted(
            range(len(shares)), key=lambda i: (-(exact[i] - floors[i]), i)
        )
        for i in order[:extra_units]:
            shares[i] += quantum
    # Pin the scale: Decimal keeps whatever exponent the arithmetic produced
    # (600 vs 600.00 vs 6E+2) and these go over the wire as str().
    return [s.quantize(quantum) for s in shares]


def plan_movement(
    entry: Dict[str, Any],
    movement_type: str,
    resolution: Optional[MovementResolution],
    *,
    flow_source_id: int,
    shared_key_movements: int = 1,
) -> MovementPlan:
    """Everything one movement contributes: entries, members, buckets, parent.

    Three shapes come out of here:
    * not a BB movement, or nothing resolved → the entry alone, with
      ``reco_id`` set to 'Not Supported' or None (retried next run);
    * one bucket → the real movement, whole, with the bucket as its reco_id;
    * several buckets → a split parent plus one ghost per bucket, the ghosts
      sharing out the movement's own amount. The real movement is NOT in
      ``entries``: the backend withdraws it, and its ghosts stand in for it.

    ``shared_key_movements`` is how many movements of the run resolve their
    payments through the SAME aggregate key. Above 1, they each claim the whole
    payment group (Finacle books one batch as several entries with no key to
    tell them apart), so the weights are shared and only the movement's own
    amount keeps the allocation honest. Recorded on the parent, never acted on.
    """
    plan = MovementPlan()
    if resolution is None:
        plan.entries.append({**entry, "reco_id": UNRESOLVED_RECO_ID})
        return plan

    groups = partition_payments(resolution.payments)
    if not groups:
        if resolution.fallback is None:
            plan.entries.append({**entry, "reco_id": None})
            return plan
        groups = {resolution.fallback: PaymentGroup()}

    if len(groups) == 1:
        key, group = next(iter(groups.items()))
        lot_id = bucket_id(flow_source_id, key)
        plan.buckets[lot_id] = key
        plan.entries.append({**entry, "reco_id": lot_id})
        plan.members.append(
            _build_member(entry, movement_type, bucket_keys(key, group), lot_id)
        )
        return plan

    total = _safe_decimal(entry.get("amount"))
    slices, payment_amount = allocate_amount(total, groups)

    children: List[Dict[str, Any]] = []
    for key, amount, group in slices:
        lot_id = bucket_id(flow_source_id, key)
        plan.buckets[lot_id] = key
        ghost_ref = ghost_external_ref(entry.get("external_ref"), key)
        direction = "debit" if amount < 0 else "credit"
        ghost_entry = {
            **entry,
            "reco_id": lot_id,
            "external_ref": ghost_ref,
            "amount": str(amount),
            "direction": direction,
        }
        plan.members.append(
            _build_member(
                ghost_entry,
                movement_type,
                bucket_keys(key, group),
                lot_id,
                split_parent_external_ref=entry.get("external_ref"),
                payment_count=group.count,
            )
        )
        children.append(
            {
                "external_ref": ghost_ref,
                "lot_id": lot_id,
                "amount": str(amount),
                "direction": direction,
                "payment_count": group.count,
                "bucket_kind": key.kind,
                "bucket_pacs008": key.pacs,
                "bucket_msgid": key.msgid,
                "bucket_po": key.po,
            }
        )

    plan.parent = {
        "movement_type": movement_type,
        "external_ref": entry.get("external_ref"),
        "account": entry.get("account"),
        "currency": entry.get("currency"),
        "amount": str(total),
        "direction": entry.get("direction"),
        "value_date": entry.get("value_date"),
        "operation_date": entry.get("operation_date"),
        "transaction_particulars": entry.get("transaction_particulars"),
        "ref_no": entry.get("ref_no"),
        "remarks_1": entry.get("remarks_1"),
        "event_type": entry.get("event_type"),
        "transaction_id": entry.get("transaction_id"),
        "payload_raw": entry.get("payload_raw"),
        # Size of the payment GROUPS this movement resolved — not "the payments
        # of this movement": when an aggregate key is shared, every movement
        # resolves the same group (see shared_key_movements).
        "payment_count": sum(g.count for g in groups.values()),
        "payment_amount": str(payment_amount),
        "shared_key_movements": shared_key_movements,
        "children": children,
    }
    return plan


def split_push_batches(
    parents: List[Dict[str, Any]], *, max_children: int = SPLIT_CHILD_BATCH
) -> Iterable[List[Dict[str, Any]]]:
    """Group split parents into pushes bounded by their GHOST count.

    A parent's ghosts must ALL travel in the same push: the backend reaps, for
    every parent it is given, the ghosts absent from the payload. Splitting one
    parent over two calls would make the second delete what the first created.
    So a parent whose own ghost count exceeds the budget is pushed alone rather
    than cut — going over budget is survivable, losing ghosts is not.
    """
    batch: List[Dict[str, Any]] = []
    count = 0
    for parent in parents:
        n = len(parent.get("children") or [])
        if batch and count + n > max_children:
            yield batch
            batch, count = [], 0
        batch.append(parent)
        count += n
    if batch:
        yield batch


def _descriptor(row: Dict[str, Any]) -> Dict[str, Any]:
    """The fields payment resolution needs — kept per movement across pass 1 so
    the lookups can run before the pass-2 re-stream (memory: a few short strings
    per movement).

    ``Initiating_channel`` is here because ``reversal_ref_for`` reads it: pass 1
    builds the lookup inputs from descriptors while pass 2 re-resolves the RAW
    rows, so any field one sees and the other doesn't would yield different
    resolutions.
    """
    return {
        "TransactionParticulars": row.get("TransactionParticulars")
        or row.get("transaction_particulars"),
        "PaymentOrderID_Ref": _dm_ref_no(row),
        "Remarks_1": _dm_remarks_1(row),
        "Initiating_channel": _channel_id(row),
    }


def _build_member(
    entry: Dict[str, Any],
    movement_type: str,
    keys: List[Key],
    lot_id: str,
    *,
    split_parent_external_ref: Optional[str] = None,
    payment_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Member payload from the ALREADY-BUILT entry dict — the backend recomputes
    the finacle source_hash from these very fields, guaranteeing hash parity
    with the entry push (and, for a ghost, with the entry the split batch
    created)."""
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
        "split_parent_external_ref": split_parent_external_ref,
        "payment_count": payment_count,
        "keys": [{"key_type": kt, "key_value": kv} for kt, kv in keys],
    }


def _safe_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

def _single_lot_report(
    lot_id: str,
    lot_members: List[Dict[str, Any]],
    *,
    top_keys: int = BB_DEBUG_TOP_KEYS,
    sample: int = BB_DEBUG_SAMPLE,
    sample_keys: int = BB_DEBUG_SAMPLE_KEYS,
) -> Dict[str, Any]:
    """Deep dive on ONE bucket's members — shared by the biggest-bucket report
    and the traced-bucket reports. The net amount shows why a bucket does not
    sum to zero, ``ghost_members`` says how much of it is synthetic, and
    ``sample_members`` are the member dicts EXACTLY as pushed to
    /tasks/finacle-bb/lots/batch."""
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
    ghosts = [m for m in lot_members if m.get("split_parent_external_ref")]

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
        "ghost_members": len(ghosts),
        "synthetic_only": bool(lot_members) and len(ghosts) == len(lot_members),
        "by_type": dict(by_type),
        "amount_by_type": {t: str(a) for t, a in sorted(amount_by_type.items())},
        "net_amount": str(net),
        "value_date_min": value_dates[0] if value_dates else None,
        "value_date_max": value_dates[-1] if value_dates else None,
        "distinct_keys_by_type": {t: len(v) for t, v in sorted(distinct_by_type.items())},
        "top_keys_by_degree": [
            {"key": f"{kt}:{kv}", "members": deg, "by_type": dict(carriers[(kt, kv)])}
            for (kt, kv), deg in top
        ],
        "sample_members": [_sample_member(m) for m in lot_members[:sample]],
    }


def bucket_debug_report(
    members_buf: List[Dict[str, Any]],
    buckets: Dict[str, BucketKey],
    parents: List[Dict[str, Any]],
    *,
    top_lots: int = 5,
    top_keys: int = BB_DEBUG_TOP_KEYS,
    sample: int = BB_DEBUG_SAMPLE,
    sample_keys: int = BB_DEBUG_SAMPLE_KEYS,
) -> Optional[Dict[str, Any]]:
    """End-of-run report (pure — unit-tested without I/O): bucket sizes, the
    split totals, and a deep dive on the BIGGEST bucket of the run.

    Two numbers are worth watching after a deploy: ``parents_with_payment_gap``
    (std.Payment disagrees with what finacle booked) and ``max_shared_key``
    (one payment group claimed by that many movements).
    """
    if not members_buf and not parents:
        return None
    sizes = Counter(m["lot_id"] for m in members_buf)
    report = None
    if sizes:
        biggest_id, _ = sizes.most_common(1)[0]
        big = [m for m in members_buf if m["lot_id"] == biggest_id]
        report = _single_lot_report(
            biggest_id, big, top_keys=top_keys, sample=sample, sample_keys=sample_keys
        )
        key = buckets.get(biggest_id)
        report["bucket"] = key.label() if key else None

    kinds = Counter(key.kind for key in buckets.values())
    return {
        "lots_total": len(sizes),
        "members_total": len(members_buf),
        "ghost_members_total": sum(
            1 for m in members_buf if m.get("split_parent_external_ref")
        ),
        "bucket_kinds": dict(kinds),
        "split_parents": len(parents),
        "parents_with_payment_gap": sum(
            1 for p in parents
            if _safe_decimal(p.get("amount")) != _safe_decimal(p.get("payment_amount"))
        ),
        "parents_sharing_a_key": sum(
            1 for p in parents if (p.get("shared_key_movements") or 1) > 1
        ),
        "max_shared_key": max(
            (p.get("shared_key_movements") or 1 for p in parents), default=0
        ),
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
    buckets: Dict[str, BucketKey],
    traced_keys: List[Key],
    *,
    top_keys: int = BB_DEBUG_TOP_KEYS,
    sample: int = BB_DEBUG_SAMPLE,
    sample_keys: int = BB_DEBUG_SAMPLE_KEYS,
    max_lots: int = 20,
) -> List[Dict[str, Any]]:
    """One report per traced key (env RECO_FINACLE_BB_TRACE_KEYS): every bucket
    that carries it, with a deep dive on the biggest.

    A key now legitimately appears in SEVERAL buckets — that is the whole point
    of pairing, and seeing how a label MessageID spreads over its pacs008 is the
    quickest way to confirm the split is doing its job.
    """
    reports: List[Dict[str, Any]] = []
    members_by_lot: Dict[str, List[Dict[str, Any]]] = {}
    for m in members_buf:
        members_by_lot.setdefault(m["lot_id"], []).append(m)

    for kt, kv in traced_keys:
        label = f"{kt}:{kv}"
        hits = [
            lot_id
            for lot_id, members in members_by_lot.items()
            if any(k["key_type"] == kt and k["key_value"] == kv for m in members for k in m["keys"])
        ]
        if not hits:
            reports.append({"traced_key": label, "found": False,
                            "note": "no member pushed this run carries the key"})
            continue
        hits.sort(key=lambda lid: -len(members_by_lot[lid]))
        biggest = hits[0]
        report = _single_lot_report(
            biggest, members_by_lot[biggest],
            top_keys=top_keys, sample=sample, sample_keys=sample_keys,
        )
        report["traced_key"] = label
        report["found"] = True
        report["lots_carrying_key"] = len(hits)
        report["lots"] = [
            {
                "lot_id": lid,
                "members": len(members_by_lot[lid]),
                "bucket": buckets[lid].label() if lid in buckets else None,
            }
            for lid in hits[:max_lots]
        ]
        reports.append(report)
    return reports


# ---------------------------------------------------------------------------
# std.Payment lookups (batched temp-table joins)
# ---------------------------------------------------------------------------

def _temp_join(lookup_conn, values, *, temp_name: str, join_sql: str) -> List[tuple]:
    """Load distinct values into a temp table and run ONE join against
    std.Payment (single scan instead of thousands of IN queries).

    The index is NON-UNIQUE and built after the load, on purpose. Python
    de-duplicates case-SENSITIVELY while SQL Server's default collation
    (SQL_Latin1_General_CP1_CI_AS) compares case-INSENSITIVELY, so two values
    that are distinct here — 'CNS-MAL-…-o4ATT8ZS' and a variant differing only
    in case — collide on a PRIMARY KEY and abort the whole run. Uniqueness was
    never needed: the index exists to make the join a seek, and a duplicated
    temp row only repeats join output, which the callers' dicts already collapse.
    """
    vals = [v for v in {_clean(v) for v in values} if v]
    if not vals:
        return []
    cursor = lookup_conn.cursor()
    try:
        cursor.execute(f"IF OBJECT_ID('tempdb..{temp_name}') IS NOT NULL DROP TABLE {temp_name}")
        cursor.execute(f"CREATE TABLE {temp_name} (k VARCHAR(128) NOT NULL)")
        cursor.fast_executemany = True
        for i in range(0, len(vals), PO_INSERT_BATCH):
            cursor.executemany(
                f"INSERT INTO {temp_name} (k) VALUES (?)",
                [(v,) for v in vals[i : i + PO_INSERT_BATCH]],
            )
        cursor.execute(f"CREATE CLUSTERED INDEX ix_temp_k ON {temp_name} (k)")
        cursor.execute(join_sql)
        rows = cursor.fetchall()
        cursor.execute(f"IF OBJECT_ID('tempdb..{temp_name}') IS NOT NULL DROP TABLE {temp_name}")
        return rows
    finally:
        cursor.close()


def resolve_sp_payments(lookup_conn, pacs008_ids) -> Dict[str, List[Tuple[str, str, Decimal]]]:
    """pacs008 -> [(MessageID, PaymentNumber, amount)] (SP direct bulks).

    This is the join that makes splitting possible: it is what says how the
    batch's amount divides over its MessageID groups.
    """
    rows = _temp_join(
        lookup_conn,
        pacs008_ids,
        temp_name="#reco_bb_pacs",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.MessageID)) AS msgid, "
            "       LTRIM(RTRIM(p.PaymentNumber)) AS po, "
            f"      p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_bb_pacs t "
            "INNER JOIN std.Payment p ON p.MessageIDPACS008 = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result = _collect_payments(rows)
    logger.info(
        "[ingest_finacle_bb] resolved payments for %d/%d pacs008 ids",
        len(result), len({_clean(p) for p in pacs008_ids if _clean(p)}),
    )
    return result


def resolve_ndgb_payments(lookup_conn, msgids) -> Dict[str, List[Tuple[str, str, Decimal]]]:
    """MessageID -> [(pacs008, PaymentNumber, amount)] (NDGB aggregates)."""
    rows = _temp_join(
        lookup_conn,
        msgids,
        temp_name="#reco_bb_msg",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs, "
            "       LTRIM(RTRIM(p.PaymentNumber)) AS po, "
            f"      p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_bb_msg t "
            "INNER JOIN std.Payment p ON p.MessageID = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result = _collect_payments(rows)
    logger.info(
        "[ingest_finacle_bb] resolved payments for %d/%d NDGB MessageIDs",
        len(result), len({_clean(m) for m in msgids if _clean(m)}),
    )
    return result


def resolve_po_payments(lookup_conn, po_ids) -> Dict[str, List[Tuple[str, str, Decimal]]]:
    """PaymentNumber -> [(MessageID, pacs008, amount)] (NDRJ + SP returns)."""
    rows = _temp_join(
        lookup_conn,
        po_ids,
        temp_name="#reco_bb_po",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.MessageID)) AS msgid, "
            "       LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs, "
            f"      p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_bb_po t "
            "INNER JOIN std.Payment p ON p.PaymentNumber = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result = _collect_payments(rows)
    logger.info(
        "[ingest_finacle_bb] resolved payments for %d/%d PO ids",
        len(result), len({_clean(p) for p in po_ids if _clean(p)}),
    )
    return result


def resolve_return_payments(
    lookup_conn, return_po_ids
) -> Dict[str, List[Tuple[str, str, str, Decimal]]]:
    """Return PaymentNumber -> [(OriginalPo, MessageID, pacs008, amount)] via
    std.[Return] (reserved word — bracketed) then the ORIGINAL payment.
    LEFT JOIN on std.Payment: a Return row whose original payment is not in
    std.Payment yet still yields its OriginalPo."""
    rows = _temp_join(
        lookup_conn,
        return_po_ids,
        temp_name="#reco_bb_ret",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(r.OriginalPo)) AS orig_po, "
            "       LTRIM(RTRIM(p.MessageID)) AS msgid, "
            "       LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs, "
            f"      p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_bb_ret t "
            "INNER JOIN std.[Return] r ON r.PaymentNumber = t.k "
            "LEFT JOIN std.Payment p "
            "  ON p.PaymentNumber = r.OriginalPo AND p.IsCurrent = 1 "
            "WHERE r.IsCurrent = 1"
        ),
    )
    result: Dict[str, Dict[Tuple[str, str, str], Decimal]] = {}
    for ret_po, orig_po, msgid, pacs, amt in rows:
        ret_po = _clean(ret_po)
        if not ret_po:
            continue
        # Keyed by the triple so an SCD2 duplicate cannot count its amount twice.
        result.setdefault(ret_po, {})[
            (_clean(orig_po) or "", _clean(msgid) or "", _clean(pacs) or "")
        ] = _safe_decimal(amt)
    logger.info(
        "[ingest_finacle_bb] resolved returns for %d/%d return PO ids",
        len(result), len({_clean(p) for p in return_po_ids if _clean(p)}),
    )
    return {
        k: [(a, b, c, amt) for (a, b, c), amt in sorted(v.items())]
        for k, v in result.items()
    }


def _collect_payments(rows: Iterable[tuple]) -> Dict[str, List[Tuple[str, str, Decimal]]]:
    """Shared shaping for the three (key, x, y, amount) lookups.

    Deduplicated on (key, x, y): std.Payment can return the same payment twice
    (SCD2 rows sharing IsCurrent = 1), and a double-counted amount would silently
    inflate a ghost — the one error the conservation check cannot catch, since
    the residual would simply flip sign.
    """
    result: Dict[str, Dict[Tuple[str, str], Decimal]] = {}
    for key, first, second, amount in rows:
        key = _clean(key)
        if not key:
            continue
        result.setdefault(key, {})[
            (_clean(first) or "", _clean(second) or "")
        ] = _safe_decimal(amount)
    return {
        k: [(a, b, amt) for (a, b), amt in sorted(v.items())]
        for k, v in result.items()
    }


# ---------------------------------------------------------------------------
# Orchestration (shared by ingest_finacle_bb and orchestrate_ingestion DAGs)
# ---------------------------------------------------------------------------

def _ingest_bb_source(conn, lookup_conn, source: Dict[str, Any], dag_run_id: Optional[str]) -> int:
    """One BB source, in two streaming passes over std.Movement.

    Pass 1 keeps a light descriptor per movement and accumulates the lookup
    inputs; four temp-table joins resolve every payment family at once; pass 2
    re-streams and plans each movement (whole, split, or unresolved). Splits are
    pushed BEFORE the buckets so the real movements are withdrawn before their
    ghosts start counting as members. ``conn`` streams std.Movement;
    ``lookup_conn`` runs the joins (a single MSSQL connection can't have a second
    active command mid-stream).
    """
    flow_code, source_code = source["flow_code"], source["source_code"]
    source_id = source["source_id"]
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
        # How many movements resolve through each aggregate key — see aggregate_key.
        key_counts = count_aggregate_keys(descriptors)
        key_counts.update(count_aggregate_keys(unresolved))
        shared = [(k, n) for k, n in key_counts.items() if n > SHARED_KEY_WARN]
        if shared:
            shared.sort(key=lambda kn: -kn[1])
            logger.warning(
                "[ingest_finacle_bb] %s/%s: %d aggregate key(s) carried by several "
                "movements — each claims the whole payment group, so the split is "
                "weighted; worst: %s",
                flow_code, source_code, len(shared),
                [(f"{kt}:{kv}", n) for (kt, kv), n in shared[:5]],
            )

        # One temp-table join per family resolves everything at once.
        pacs_map = resolve_sp_payments(lookup_conn, inputs.sp_pacs008)
        # The IP aggregates' TransactionRef yields their MessageID, which must be
        # in the msgid_map for the fan-out to reach the bulk's payments — hence
        # this join BEFORE resolve_ndgb_payments, whose input it feeds.
        txnref_map, _ = resolve_reversals(lookup_conn, inputs.instant_txn_refs)
        msgid_map = resolve_ndgb_payments(
            lookup_conn, inputs.ndgb_msgids | {m for m in txnref_map.values() if m}
        )
        po_map = resolve_po_payments(lookup_conn, inputs.po_ids)
        return_map = resolve_return_payments(lookup_conn, inputs.return_po_ids)
        maps = (pacs_map, msgid_map, po_map, return_map)

        members_buf: List[Dict[str, Any]] = []
        parents_buf: List[Dict[str, Any]] = []
        buckets: Dict[str, BucketKey] = {}

        def _plan(record: Dict[str, Any], entry: Dict[str, Any]) -> List[Dict[str, Any]]:
            """Plan one movement and buffer what it contributes → entries to push."""
            resolution = movement_resolution(record, *maps, txnref_map=txnref_map)
            plan = plan_movement(
                entry,
                classify_bb_movement(record) or "?",
                resolution,
                flow_source_id=source_id,
                shared_key_movements=key_counts.get(aggregate_key(record), 1),
            )
            members_buf.extend(plan.members)
            buckets.update(plan.buckets)
            if plan.parent:
                parents_buf.append(plan.parent)
            return plan.entries

        # Pass 2 — re-stream, push the entries of unsplit movements.
        for chunk in iter_movement_chunks(
            conn, accounts=accounts, since=since, chunk_size=BB_CHUNK_SIZE
        ):
            entries, errors = [], []
            for row in chunk:
                try:
                    entry = movement_row_to_entry(row, None)
                except ValueError as exc:
                    errors.append(f"TransactionID={row.get('TransactionID')}: {exc}")
                    continue
                entries.extend(_plan(row, entry))
            finacle_post_batch(run_id, entries, errors)
            total += len(entries)

        # Retry — re-enrich the app's unresolved entries and re-push (upsert in
        # place); the ones that now split leave the live table via the split push.
        for i in range(0, len(unresolved), BB_CHUNK_SIZE):
            batch: List[Dict[str, Any]] = []
            for entry in unresolved[i : i + BB_CHUNK_SIZE]:
                batch.extend(_plan(entry, dict(entry)))
            finacle_post_batch(run_id, batch, [])
            total += len(batch)

        # End-of-run debug, logged BEFORE the pushes so a failed push still
        # leaves the report in the Airflow logs.
        report = bucket_debug_report(members_buf, buckets, parents_buf)
        if report:
            logger.info(
                "[ingest_finacle_bb] %s/%s: BUCKET DEBUG %s",
                flow_code, source_code,
                json.dumps(report, default=str, ensure_ascii=False),
            )
        traced = parse_trace_keys(os.environ.get("RECO_FINACLE_BB_TRACE_KEYS", ""))
        for trace in trace_key_reports(members_buf, buckets, traced):
            logger.info(
                "[ingest_finacle_bb] %s/%s: BUCKET TRACE %s",
                flow_code, source_code,
                json.dumps(trace, default=str, ensure_ascii=False),
            )

        # Splits FIRST: each batch registers its parents, materialises their
        # ghosts and withdraws the real movements in one transaction, so no
        # intermediate state has a movement and its ghosts both counting.
        base = {"flow_code": flow_code, "source_code": source_code}
        ghosts_total = sum(len(p["children"]) for p in parents_buf)
        if parents_buf:
            logger.info(
                "[ingest_finacle_bb] %s/%s: pushing %d split parent(s) / %d ghost(s)",
                flow_code, source_code, len(parents_buf), ghosts_total,
            )
        for batch in split_push_batches(parents_buf):
            finacle_bb_post_split_batch(
                {**base, "run_id": run_id, "parents": batch}
            )

        # Then the buckets + members. Only buckets actually referenced by a
        # member are created (a movement that errored out never leaves an empty
        # lot behind).
        used = {m["lot_id"] for m in members_buf}
        lots_payload = [key.payload(lot_id) for lot_id, key in buckets.items() if lot_id in used]
        finacle_bb_post_lot_batch(
            {**base, "lots": lots_payload, "members": members_buf[:MEMBER_PUSH_BATCH]}
        )
        for i in range(MEMBER_PUSH_BATCH, len(members_buf), MEMBER_PUSH_BATCH):
            finacle_bb_post_lot_batch(
                {**base, "lots": [], "members": members_buf[i : i + MEMBER_PUSH_BATCH]}
            )

        finacle_complete_run(run_id)
        logger.info(
            "[ingest_finacle_bb] %s/%s: run #%s done (%d pushed incl. %d retried, "
            "%d member(s), %d bucket(s), %d split parent(s))",
            flow_code, source_code, run_id, total, len(unresolved),
            len(members_buf), len(lots_payload), len(parents_buf),
        )
    except Exception:
        try:
            finacle_complete_run(run_id, failed=True, error="see Airflow logs")
        except Exception:  # noqa: BLE001
            logger.warning("[ingest_finacle_bb] could not mark run #%s as failed", run_id)
        raise
    return total


def run_finacle_bb_ingestion(dag_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Extract + bucket movements for every active BATCH BOOKING TRUE source."""
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
