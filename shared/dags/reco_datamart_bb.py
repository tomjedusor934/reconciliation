"""Datamart extraction for BATCH BOOKING TRUE finacle sources (bucket splitting).

On a batch-booking flow, N Paysis-side bulk movements (SCTXB/SDDXB/SDXBB) are
settled by M smaller Finacle-side aggregates (NDGB) that INTERLEAVE payments
across the N bulks — one movement no longer maps to one reconciliation key.

The reconciliation unit is the BUCKET: one pacs008 batch carries many payment
orders, those orders are grouped by their ``std.Payment.MessageID``, and each
(pacs008, MessageID) pair is one lot. A bucket's id is
``uuid5(ns, "<flow_source_id>|<kind>|<pacs>|<msgid>|<po>|<ref>")`` — a pure
function of its identity, so the same bucket always yields the same uuid and
this DAG names its lots without reading anything back from the backend. The
components are CASE-FOLDED first: the datamart's collation is case-insensitive,
so its own joins already treat two spellings as one key, and comparing them
byte-for-byte here would split a bucket in two (see ``BucketKey``).

This REPLACES the union-find over {PACS008, MSGID, PO} keys. That clustering was
transitive, and Finacle's MessageIDs are frequently labels rather than
identifiers ('LUXEMBOURG', 'ESCH/ALZETTE', 'ADEM-VIR<ts>'): one shared label
glued unrelated pacs008 together until a single lot held 50 898 movements,
35 pacs008 and 3 488 MessageIDs, and could never sum to zero. Pairing kills the
transitivity: (X, LUXEMBOURG) and (Y, LUXEMBOURG) are simply two buckets.

GHOST MOVEMENTS. A movement whose payments span several buckets belongs to none
of them: an SP bulk is ONE booking for a whole pacs008, and an NDGB carrying a
label MessageID spans every pacs008 that reused the label. Such a movement is
registered as a SPLIT PARENT and withdrawn from ``reconciliation_entry``; ghost
entries stand in for it, one per bucket.

CLAIM GROUPS (2026-08-06 — replaces per-parent ghosts and the prorata regime).
Finacle books one batch as N entries carrying the same aggregate key
('PTEL-003842…' × 184 NDGB) and EVERY one of them resolves the WHOLE payment
group — the datamart offers no per-movement attribution. Emitting one ghost set
per movement duplicated every bucket N times (160 lots each carrying 6
near-identical LUXEMBOURG ghosts, all stuck at the same net), and prorating the
booked amount scattered largest-remainder cents across the lots (~330 lots
pending at ±0,01 in prod). So ghosts are now emitted once per CLAIM GROUP — all
the split movements of a run that resolve their payments through one aggregate
key (``MovementResolution.claim``) — and each ghost is worth its bucket's
payment sum, TO THE CENT. That exactness is what makes a bucket balance: both
sides of a PAIR bucket price their ghosts from the very same std.Payment rows
and cancel; a ghost facing a real movement meets the amount the bank actually
booked for those payments.

The price of exactness is that ``Σ ghosts == Σ booked`` no longer holds per
movement or per group: whatever the accounting and std.Payment disagree on
(charges, FX, an over-fetched label MessageID) is NOT materialised as an entry.
The backend reconciles every group after each run — Σ(parents.amount) vs
Σ(ghosts that exist) — and tags the lots carrying the ghosts of an unbalanced
group (``movement_lot.parent_mismatch``): a matched lot whose parent group does
not add up stays visibly not-fully-validated. That is the second reconciliation;
nothing here needs to invent a RESIDUAL bucket for it any more.

Ghost identity is a pure function of (claim key, bucket) — no parent reference,
no date — so any run re-emitting a group upserts the same rows. The backend
anchors the ghost hashes on the group's CANONICAL parent (oldest by value_date
in ``movement_split``), which keeps them stable when a later run adds parents
to an existing group.

A consequence worth knowing: when BOTH sides of a bucket are ghosts, they are
priced from the same std.Payment groups and cancel by construction — the lot
matches without proving anything. The backend flags those lots ``synthetic_only``
so the distinction stays visible.

Payment resolution per movement type (prefix of ``TransactionParticulars``):
- SCTXB/SDDXB/SDXBB direct (``PREFIX/I|O/...``): ``Remarks_1`` is the pacs008 →
  every payment with that ``MessageIDPACS008``. The one that really splits.
- SCTXB/SDDXB/SDXBB return (``PREFIX/NCC|NCP|RRS/...``): PO id in ``ref_no``
  (fallback: TP segment[3]) → that single payment → the ORIGINAL bulk's bucket.
  The SEGMENT decides, never the prefix: ``Rev of /NCP/O/<po>/…`` — how Finacle
  labels the reversal of a return — resolves exactly like its bulk twin.
- SCTXB/SDDXB/SDXBB single or reversal (``PREFIX/<TransactionRef>/<NAME>``, no
  usable ``Remarks_1``): TP segment[1] is the payment's ``TransactionRef`` → its
  ``MessageID`` → the same fan-out as NDGB. ``PREFIX/RVSL/<ref>/<reason>`` puts
  that ref one segment further right.
- NDGB (the Finacle aggregate): ``Remarks_1`` is the ``MessageID`` → every
  payment under it. Splits when the MessageID is a label spanning pacs008.
- NDRJ (reject of any payment type): PO id in ``ref_no`` = ``paysis##<po_id>``.
- SWIFT/SCRT1/BKRTP instant payments: with a ``ref_no`` they are singles, keyed
  by it (bucket PO, no lookup). WITHOUT one, the movement is the AGGREGATE of a
  bulk-booked IP or a reversal: TP segment[1] is the payment's ``TransactionRef``
  → its ``MessageID`` → the same fan-out as NDGB.
- NDRT (reject-of-return) and SP ``PREFIX/RCC|RCP/…``: ``ref_no`` is the RETURN's
  PaymentNumber in ``std.[Return]`` (reserved word — bracketed) → ``OriginalPo``
  → the original payment's bucket.
- Anything else → reco_id "Not Supported" (never in a lot, retried by re-stream).

A movement that resolves NO payment falls back to the bucket its own fields
name (the pacs008 it quotes, its MessageID, its PO) so it still lands somewhere
and meets its counterpart later; only a movement with no usable field at all
gets ``reco_id = None`` — transient, re-enriched through
``/tasks/finacle/unresolved`` on every run.

THE FALLBACK IS NOT A LICENCE TO BUCKET ON ANYTHING. The SP direct branch used to
read ``Remarks_1`` AS the pacs008 with no validation, so every shape this parser
did not list became a lot keyed on whatever sat there — on a return that is the
counterparty IBAN or a UUID, and it produced 5 652 such lots (90% of the
PACS_ONLY lots on the outward float). ``looks_like_pacs008`` now refuses those
two shapes: the movement falls through to the TransactionRef lookup and, failing
that, stays transient. Refusals are COUNTED and logged per run, so the next
unlisted shape announces itself instead of quietly minting thousands of lots.

THE MSGID FAN-OUT IS TIME-BOUNDED, per key (2026-08-07). A MessageID is
frequently a REUSED LABEL ('LUXEMBOURG'), and ``p.MessageID = :k`` alone drags
in the label's entire multi-year history: 17,96 Md€ of payments claimed by
2,17 Md€ of booked movements, ~19 000 single-ghost lots (historic pacs008,
50-99 M€ treasury payments with no pacs at all), a dashboard showing 20 Md€
pending IN. The proof it is over-fetch and nothing else: the label ghosts that
MATCHED all sit on in-window pacs008, and their matched total ≈ the booked
total. So each MessageID only claims payments whose ``CreatedOn`` falls inside
the window of the movements CARRYING that key this run —
``[min(value_date) − LOOKBACK, max(value_date) + LOOKAHEAD]`` (see
``window_bounds``) — and unresolved retries widen their key's window to cover
themselves. The other lookups (pacs008, PaymentNumber, returns) stay unbounded:
those keys are single-use by construction. A key carried only by undated
movements cannot be bounded and falls back to an open window, loudly.

Entries follow the regular ``/tasks/finacle/runs`` lifecycle. Splits are pushed
to ``/tasks/finacle-bb/splits/*`` FIRST — one claim group per payload item, its
parents and its ghosts together (parents registered, ghosts created, real
movements withdrawn — one transaction) — then the buckets and members to
``/tasks/finacle-bb/lots/*``, so no intermediate state has a movement and its
ghosts both counting.
"""
import hashlib
import json
import logging
import os
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
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
    BULK_REJECT_SEGS,
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
    return_reject_seg,
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

KEY_PACS008 = "PACS008"
KEY_MSGID = "MSGID"
KEY_PO = "PO"

BUCKET_PAIR = "PAIR"
BUCKET_PACS_ONLY = "PACS_ONLY"
BUCKET_MSGID_ONLY = "MSGID_ONLY"
BUCKET_PO = "PO"

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
# Margins of the per-key MSGID fan-out window (see the module docstring and
# ``window_bounds``): payments may be created days before the NDGB that settles
# them is booked, and a batch can settle a little after. Calibrate against
# MIN/MAX(std.Payment.CreatedOn) per pacs008 vs the NDGB's value_date.
MSGID_LOOKBACK_DAYS = int(os.environ.get("RECO_FINACLE_BB_MSGID_LOOKBACK_DAYS", "15"))
MSGID_LOOKAHEAD_DAYS = int(os.environ.get("RECO_FINACLE_BB_MSGID_LOOKAHEAD_DAYS", "5"))
# Payments with a NULL CreatedOn cannot be windowed. Default is STRICT (they are
# excluded from the MSGID fan-out); set to 1 only if the datamart turns out to
# leave CreatedOn empty on legitimate batch payments.
MSGID_INCLUDE_NULL_CREATED = os.environ.get(
    "RECO_FINACLE_BB_MSGID_INCLUDE_NULL_CREATED", "0"
) not in ("0", "", "false", "False")
# The open window handed to a key no dated movement carries — bounds SQL Server's
# DATE accepts, so one join shape serves every key.
OPEN_WINDOW = (date(1900, 1, 1), date(9999, 12, 31))
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
    constraint over these columns would not bite on NULLs).

    Components are UPPERCASED here, and this is the only place that has to know:
    every identity downstream (``bucket_id``, ``group_ghost_ref``,
    ``bucket_keys``, the payload, the grouping in ``partition_payments``) is
    built from a BucketKey.

    Why: each side of a pair takes one component from the movement and the other
    from ``std.Payment`` — an SP bulk reads its pacs008 off ``Remarks_1`` and its
    MessageID off the payment, an NDGB does the reverse. Finacle writes
    ``Remarks_1`` in upper case while the datamart keeps the real casing, and
    SQL Server's collation (SQL_Latin1_General_CP1_CI_AS) is case-INSENSITIVE, so
    the join happily matches what Python then treats as two different strings.
    In prod that split one bucket in two: '2412-20260731-RUMELANGE' (NDGB) and
    '2412-20260731-Rumelange' (SCTXB), +1 573,80 and -1 573,80, never matching.

    ``upper()`` and not ``casefold()``: the collation is CI_**AS** — case
    insensitive but accent SENSITIVE — and ``upper()`` is its exact analogue.
    Normalising cannot merge groups the datamart itself distinguishes: it cannot
    distinguish them either, its own joins are case-insensitive.
    """
    kind: str
    pacs: str = ""
    msgid: str = ""
    po: str = ""
    ref: str = ""

    def __post_init__(self) -> None:
        for name in ("pacs", "msgid", "po", "ref"):
            object.__setattr__(self, name, (getattr(self, name) or "").upper())

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
    bucket its own fields name.

    ``claim`` is the lookup key the payments were fetched through — the CLAIM
    KEY. Every movement resolving the same claim resolves the very same payment
    set (there is one map per family), so the claim names the group a split
    movement belongs to. Uppercased like ``BucketKey`` and for the same reason.
    """
    payments: Tuple[PaymentRef, ...]
    fallback: Optional[BucketKey]
    claim: Optional[Key] = None


def _claim(key_type: str, value: str) -> Key:
    return (key_type, (value or "").upper())


@dataclass
class MovementPlan:
    """What to push for one movement.

    A movement that must SPLIT contributes no entries and no members of its own:
    ``parent`` + ``claim`` register it in its claim group, ``partition`` carries
    the (identical for every member of the group) bucket partition, and the
    group's ghosts are emitted once per claim by ``plan_claim_group``.
    """
    entries: List[Dict[str, Any]] = field(default_factory=list)
    members: List[Dict[str, Any]] = field(default_factory=list)
    buckets: Dict[str, BucketKey] = field(default_factory=dict)
    parent: Optional[Dict[str, Any]] = None
    claim: Optional[Key] = None
    partition: Optional[Dict[BucketKey, PaymentGroup]] = None


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
        # A prefix nobody lists, wearing the return/reject shape: 'Rev of /NCP/O/
        # <po>/…' is how Finacle labels the reversal of a return. Tested LAST, so
        # every known family keeps the branch it already had.
        or return_reject_seg(parts)
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


# Shapes ``Remarks_1`` takes when it is NOT a pacs008 but the counterparty's own
# data. The SP direct branch reads Remarks_1 AS the pacs008 with no validation,
# so every shape this parser does not recognise silently mints a bucket keyed on
# whatever sits there — that is how 4 920 UUIDs and 732 IBANs became lots (90% of
# the PACS_ONLY lots on the outward float). A DENY-list on purpose: legitimate
# values are not all numeric — ``BLK2026188019314`` names the aggregate return
# legs the RCP reattribution tool works on, and must keep its own bucket.
_IBAN_RE = re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{10,}$")
_UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")


def looks_like_pacs008(value: Optional[str]) -> bool:
    """False for the values proven to be counterparty data, never a pacs008."""
    if not value:
        return False
    folded = value.strip().upper()
    return not (_IBAN_RE.match(folded) or _UUID_RE.match(folded))


def sp_direct_pacs008(row: Dict[str, Any]) -> Optional[str]:
    """``Remarks_1`` when it can name a pacs008, else None."""
    pacs = _clean(_dm_remarks_1(row))
    return pacs if looks_like_pacs008(pacs) else None


# A fan-out window: [dmin, dmax] over the value dates of the movements carrying
# one key. [None, None] = the key was only seen on undated movements.
Window = List[Optional[date]]


def _movement_date(row: Dict[str, Any]) -> Optional[date]:
    """The movement's day, from a raw std.Movement row (``ValueDate`` /
    ``TransactionDate``) or an app entry (``value_date`` ISO string)."""
    raw = row.get("ValueDate") or row.get("TransactionDate") or row.get("value_date")
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def _widen(windows: Dict[str, Window], key: str, day: Optional[date]) -> None:
    """Grow ``key``'s window to cover ``day`` (a dateless movement still
    registers the key, with an empty window)."""
    window = windows.setdefault(key, [None, None])
    if day is None:
        return
    if window[0] is None or day < window[0]:
        window[0] = day
    if window[1] is None or day > window[1]:
        window[1] = day


def merge_msgid_windows(
    ndgb_windows: Dict[str, Window],
    txnref_windows: Dict[str, Window],
    txnref_map: Dict[str, Optional[str]],
) -> Dict[str, Window]:
    """The MSGID fan-out windows: the NDGB carriers' windows, widened by the IP
    aggregates whose TransactionRef resolved to the same MessageID — both shapes
    claim the group, so the window must cover both."""
    merged: Dict[str, Window] = {k: list(v) for k, v in ndgb_windows.items()}
    for ref, msgid in txnref_map.items():
        msgid = _clean(msgid)
        if not msgid:
            continue
        window = txnref_windows.get(ref, [None, None])
        _widen(merged, msgid, window[0])
        _widen(merged, msgid, window[1])
        merged.setdefault(msgid, [None, None])
    return merged


def window_bounds(
    window: Optional[Window],
    *,
    lookback_days: int = MSGID_LOOKBACK_DAYS,
    lookahead_days: int = MSGID_LOOKAHEAD_DAYS,
) -> Tuple[date, date]:
    """(dmin, dmax exclusive) the fan-out may use for one key.

    A key carried only by undated movements gets the OPEN window: an unbounded
    fan-out is today's behaviour, silently dropping the key's payments is not.
    """
    if not window or window[0] is None or window[1] is None:
        return OPEN_WINDOW
    return (
        window[0] - timedelta(days=lookback_days),
        window[1] + timedelta(days=lookahead_days + 1),
    )


@dataclass
class BBLookupInputs:
    """Distinct values needing a std.Payment / std.[Return] lookup, per family.

    MessageIDs and IP TransactionRefs carry their WINDOW — the value-date span
    of the movements naming them — because the MSGID fan-out is time-bounded
    (see the module docstring); the single-use families stay plain sets.
    """
    sp_pacs008: Set[str] = field(default_factory=set)  # SP direct Remarks_1
    ndgb_msgids: Dict[str, Window] = field(default_factory=dict)  # NDGB Remarks_1
    po_ids: Set[str] = field(default_factory=set)       # NDRJ + SP returns
    return_po_ids: Set[str] = field(default_factory=set)  # NDRT + SP /RCC rejects
    instant_txn_refs: Dict[str, Window] = field(default_factory=dict)  # IP aggregate TP seg[1]
    # {rejected Remarks_1 shape: how many movements} — a Remarks_1 refused as a
    # pacs008 (see ``looks_like_pacs008``). Logged per run so a NEW unhandled
    # shape shows up instead of quietly creating thousands of lots.
    rejected_remarks: Counter = field(default_factory=Counter)


def collect_lookup_inputs(rows: Iterable[Dict[str, Any]], acc: BBLookupInputs) -> None:
    """Accumulate lookup inputs from raw std.Movement rows OR app entry dicts
    (the ``_tp_parts`` / ``_dm_*`` lowercase fallbacks make both shapes work).

    Unresolved retries flow through here too, so an old movement re-processed
    widens its own key's window enough to reach its payments.
    """
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
                pacs = sp_direct_pacs008(row)
                if pacs:
                    acc.sp_pacs008.add(pacs)
                else:
                    raw = _clean(_dm_remarks_1(row))
                    if raw:
                        acc.rejected_remarks[f"{prefix}/{seg1}"] += 1
                    # No usable pacs008 in Remarks_1: the bulk is a single or a
                    # reversal booked in the bulk flow, and TP segment[1] is the
                    # payment's TransactionRef — the very fallback the INSTANT
                    # branch already makes below.
                    ref = reversal_ref_for(row)
                    if ref:
                        _widen(acc.instant_txn_refs, ref, _movement_date(row))
        elif prefix == NDGB_PREFIX:
            msgid = _clean(_dm_remarks_1(row))
            if msgid:
                _widen(acc.ndgb_msgids, msgid, _movement_date(row))
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
                _widen(acc.instant_txn_refs, ref, _movement_date(row))
        else:
            # Unlisted prefix wearing the return/reject shape ('Rev of /NCP/O/…').
            # Last, so no known family is diverted from the branch it had.
            seg = return_reject_seg(parts)
            if seg:
                po = sp_return_po_id(row, parts)
                if po:
                    target = acc.return_po_ids if seg in BULK_REJECT_SEGS else acc.po_ids
                    target.add(po)


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
    """The lookup key a movement resolves its payments through, case-folded.

    Counting these over a run tells how many movements share one key. Finacle
    books a 20 000-payment SDD batch as 184 separate entries carrying the same
    ``Remarks_1``, and each of them then resolves the WHOLE group — there is no
    key to divide it. The count does not change any behaviour (movements sharing
    a key land in ONE claim group and emit one ghost set regardless), it makes
    the situation visible in the run log instead of silent.

    Folded like ``BucketKey`` and for the same reason: two spellings of one key
    are one key to the datamart, so counting them apart would understate the
    sharing. ``_raw_aggregate_key`` keeps the original for ``casing_conflicts``.
    """
    key = _raw_aggregate_key(row)
    return (key[0], key[1].upper()) if key else None


def _raw_aggregate_key(row: Dict[str, Any]) -> Optional[Key]:
    """``aggregate_key`` without the case folding — the datamart's own spelling.

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
        pacs = sp_direct_pacs008(row)
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
    if return_reject_seg(parts):
        po = sp_return_po_id(row, parts)
        return (KEY_PO, po) if po else None
    return None


def count_aggregate_keys(rows: Iterable[Dict[str, Any]]) -> Counter:
    """{aggregate key: how many movements resolve through it} — folded keys."""
    counts: Counter = Counter()
    for row in rows:
        key = aggregate_key(row)
        if key:
            counts[key] += 1
    return counts


def casing_conflicts(rows: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Aggregate keys the run saw spelled several ways → {FOLDED KEY: spellings}.

    Pure datamart-quality signal: the identity is folded so these no longer split
    a bucket, but knowing they exist explains why two movements that look
    unrelated in the raw data reconcile together.
    """
    spellings: Dict[str, Set[str]] = {}
    for row in rows:
        raw = _raw_aggregate_key(row)
        if raw:
            spellings.setdefault(f"{raw[0]}:{raw[1].upper()}", set()).add(raw[1])
    return {k: sorted(v) for k, v in spellings.items() if len(v) > 1}


def group_ghost_ref(claim: Key, key: BucketKey) -> str:
    """Identity of one ghost: a pure function of (claim key, bucket).

    Deliberately free of any parent reference and any date, so every run that
    re-emits a group names the same ghosts and the upsert lands on the same
    rows regardless of which movements happened to be in the run. The claim
    value is kept readable up front (it is what an operator searches for); the
    two digests disambiguate truncated values and distinct buckets. Bounded to
    fit ``external_ref`` VARCHAR(128).
    """
    kt, kv = claim
    claim_tag = hashlib.sha1(f"{kt}|{kv}".encode("utf-8")).hexdigest()[:8]
    bucket_raw = f"{key.kind}|{key.pacs}|{key.msgid}|{key.po}|{key.ref}"
    bucket_tag = hashlib.sha1(bucket_raw.encode("utf-8")).hexdigest()[:10]
    return f"KEY:{kv[:88]}~{claim_tag}~{bucket_tag}"


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
            # Reject of a return (SP side, PREFIX/RCC|RCP/…): same std.[Return]
            # indirection as NDRT.
            return _return_resolution(sp_return_po_id(row, parts), return_map)
        if seg1 in BULK_RETURN_SEGS:
            # Return of one payment (NCC/NCP/RRS): it settles inside the ORIGINAL
            # bulk's bucket, which its PaymentNumber names.
            return _po_resolution(sp_return_po_id(row, parts), po_map)
        # Direct (I/O): Remarks_1 carries the pacs008 — when it really is one.
        pacs = sp_direct_pacs008(row)
        if pacs:
            payments = tuple(
                PaymentRef(pacs=pacs, msgid=msgid or "", po=po or "", amount=amount)
                for msgid, po, amount in pacs_map.get(pacs, [])
            )
            return MovementResolution(
                payments, BucketKey(BUCKET_PACS_ONLY, pacs=pacs), _claim(KEY_PACS008, pacs)
            )
        # No pacs008 to be had — an SP single (SCTXB/<TransactionRef>/<NAME>) or a
        # reversal booked in the bulk flow. TP segment[1] is the payment's
        # TransactionRef, exactly as for the IP aggregates below; without it the
        # movement stays transient and is retried.
        return _msgid_resolution(
            _clean((txnref_map or {}).get(reversal_ref_for(row) or "")), msgid_map
        )

    if prefix == NDGB_PREFIX:
        return _msgid_resolution(_clean(_dm_remarks_1(row)), msgid_map)

    if prefix == NDRJ_PREFIX:
        return _po_resolution(ndrj_po_id(row), po_map)

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
        # through their PO. The claim is the resolved MessageID, NOT the
        # TransactionRef: an IP aggregate and an NDGB resolving the same
        # MessageID claim the same payment group and must fall in ONE group.
        # A miss (no TransactionRef, or not in std.Payment yet) stays transient.
        return _msgid_resolution(
            _clean((txnref_map or {}).get(reversal_ref_for(row) or "")), msgid_map
        )

    # An unlisted prefix wearing the return/reject shape — 'Rev of /NCP/O/<po>/…',
    # how Finacle labels the reversal of a return. Tested LAST, so every known
    # family keeps the branch it already had (a BKRTP/NCP stays an IP keyed by its
    # own ref_no, above).
    seg = return_reject_seg(parts)
    if seg in BULK_REJECT_SEGS:
        return _return_resolution(sp_return_po_id(row, parts), return_map)
    if seg in BULK_RETURN_SEGS:
        return _po_resolution(sp_return_po_id(row, parts), po_map)

    return None


def _po_resolution(
    po: Optional[str], po_map: Dict[str, List[Tuple[str, str, Decimal]]]
) -> MovementResolution:
    """Payments of a movement naming ONE PaymentNumber (bulk return, NDRJ reject,
    reversal of a return): it settles inside the ORIGINAL payment's bucket, which
    that PaymentNumber names. The PO bucket is the fallback for when std.Payment
    does not know it yet."""
    if not po:
        return MovementResolution((), None)
    payments = tuple(
        PaymentRef(pacs=pacs or "", msgid=msgid or "", po=po, amount=amount)
        for msgid, pacs, amount in po_map.get(po, [])
    )
    return MovementResolution(payments, BucketKey(BUCKET_PO, po=po), _claim(KEY_PO, po))


def _msgid_resolution(
    msgid: Optional[str], msgid_map: Dict[str, List[Tuple[str, str, Decimal]]]
) -> MovementResolution:
    """Payments under one MessageID — the NDGB fan-out, shared by every aggregate
    shape that reaches its MessageID through a TransactionRef. No MessageID (not
    in std.Payment yet) is transient, not 'Not Supported'."""
    if not msgid:
        return MovementResolution((), None)
    payments = tuple(
        PaymentRef(pacs=pacs or "", msgid=msgid, po=po or "", amount=amount)
        for pacs, po, amount in msgid_map.get(msgid, [])
    )
    return MovementResolution(
        payments, BucketKey(BUCKET_MSGID_ONLY, msgid=msgid), _claim(KEY_MSGID, msgid)
    )


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
    return MovementResolution(payments, BucketKey(BUCKET_PO, po=po), _claim(KEY_PO, po))


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
        # Uppercased like the identity components above: a key that navigates to
        # a bucket must be spelled the way the bucket is.
        keys.extend((KEY_PO, po.upper()) for po in group.pos)
    return sorted(set(keys))


# ---------------------------------------------------------------------------
# Splitting
# ---------------------------------------------------------------------------

def group_slices(
    partition: Dict[BucketKey, PaymentGroup], *, sign: Decimal
) -> List[Tuple[BucketKey, Decimal, PaymentGroup]]:
    """Value a claim group's ghosts: each slice IS its bucket's payment sum, to
    the cent, signed like the group's booked total (std.Payment stores amounts
    unsigned — whether the group settles or receives is how finacle booked it).

    A slice worth 0 is never emitted: it would add a PENDING entry that settles
    nothing, and a bucket holding only zeros would net to zero and "match" on
    thin air.
    """
    ordered = sorted(partition.items(), key=lambda kv: (-abs(kv[1].amount), kv[0].label()))
    return [
        (key, sign * abs(group.amount), group)
        for key, group in ordered
        if group.amount
    ]


def plan_movement(
    entry: Dict[str, Any],
    movement_type: str,
    resolution: Optional[MovementResolution],
    *,
    flow_source_id: int,
) -> MovementPlan:
    """Everything one movement contributes: entries, members, buckets — or its
    registration as a split parent.

    Three shapes come out of here:
    * not a BB movement, or nothing resolved → the entry alone, with
      ``reco_id`` set to 'Not Supported' or None (retried next run);
    * one bucket carrying value → the real movement, whole, with the bucket as
      its reco_id;
    * several buckets with a non-zero payment sum → a SPLIT PARENT. The movement
      contributes NO entry and NO member of its own: ``parent`` + ``claim``
      register it in its claim group and the group's ghosts are emitted once per
      claim by ``plan_claim_group`` — N movements sharing a key never duplicate
      a bucket's ghost N times.
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

    def _whole_into(key: BucketKey, group: PaymentGroup) -> MovementPlan:
        """The real movement, undivided, in one bucket — no parent, no ghost."""
        lot_id = bucket_id(flow_source_id, key)
        plan.buckets[lot_id] = key
        plan.entries.append({**entry, "reco_id": lot_id})
        plan.members.append(
            _build_member(entry, movement_type, bucket_keys(key, group), lot_id)
        )
        return plan

    if len(groups) == 1:
        return _whole_into(*next(iter(groups.items())))

    nonzero = {key: group for key, group in groups.items() if group.amount}
    if len(nonzero) <= 1:
        # Everything of value landed in one place (or nowhere). Splitting would
        # withdraw the real movement and stand in for it with a single ghost —
        # or with NOTHING, losing the amount outright. Keep the movement itself,
        # whole, in the bucket that carries the value; when every sum is zero,
        # the heaviest named bucket (deterministic tie-break by label).
        target = nonzero or groups
        return _whole_into(
            *max(target.items(), key=lambda kv: (abs(kv[1].amount), kv[0].label()))
        )

    total = _safe_decimal(entry.get("amount"))
    sign = Decimal(-1) if total < 0 else Decimal(1)
    payment_amount = sign * sum((abs(g.amount) for g in groups.values()), Decimal("0"))
    # ``claim`` is set whenever payments resolved (they came out of a keyed
    # lookup); the SELF fallback only guards an unforeseen shape.
    plan.claim = resolution.claim or _claim("SELF", entry.get("external_ref") or "?")
    plan.partition = groups
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
        # Overwritten at group-emission time with the real group size.
        "shared_key_movements": 1,
    }
    return plan


def plan_claim_group(
    claim: Key,
    partition: Dict[BucketKey, PaymentGroup],
    parents: List[Dict[str, Any]],
    *,
    flow_source_id: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], Dict[str, BucketKey]]:
    """One claim group's push → (group payload, lot members, buckets touched).

    ``parents`` must be ordered ``(value_date, external_ref)``: the first one is
    the RUN's canonical parent, whose account/dates/type the ghosts borrow. The
    backend re-anchors them on the group's STORED canonical when the group
    already exists, so this choice only matters for a brand-new group.

    Every parent of the partition resolved the same payments, so the partition
    is any of theirs; the ghosts are its buckets' exact sums, signed by the
    group's booked total. ``Σ ghosts`` is NOT expected to equal ``Σ parents`` —
    that gap is the second reconciliation's job (parent_mismatch tag), not a
    movement to invent.

    ``shared_key_movements`` is stamped on every parent here: only the group
    knows its own size.
    """
    total = sum((_safe_decimal(p.get("amount")) for p in parents), Decimal("0"))
    sign = Decimal(-1) if total < 0 else Decimal(1)
    canonical = parents[0]
    for parent in parents:
        parent["shared_key_movements"] = len(parents)

    members: List[Dict[str, Any]] = []
    buckets: Dict[str, BucketKey] = {}
    children: List[Dict[str, Any]] = []
    for key, amount, group in group_slices(partition, sign=sign):
        lot_id = bucket_id(flow_source_id, key)
        buckets[lot_id] = key
        ghost_ref = group_ghost_ref(claim, key)
        direction = "debit" if amount < 0 else "credit"
        ghost_entry = {
            "external_ref": ghost_ref,
            "account": canonical.get("account"),
            "currency": canonical.get("currency"),
            "amount": str(amount),
            "direction": direction,
            "value_date": canonical.get("value_date"),
            "operation_date": canonical.get("operation_date"),
            "transaction_particulars": canonical.get("transaction_particulars"),
            "ref_no": canonical.get("ref_no"),
            "remarks_1": canonical.get("remarks_1"),
        }
        members.append(
            _build_member(
                ghost_entry,
                canonical.get("movement_type") or "?",
                bucket_keys(key, group),
                lot_id,
                claim=claim,
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

    group_payload = {
        "claim_key_type": claim[0],
        "claim_key_value": claim[1],
        "account": canonical.get("account"),
        "currency": canonical.get("currency"),
        "value_date": canonical.get("value_date"),
        "operation_date": canonical.get("operation_date"),
        "event_type": canonical.get("event_type"),
        "parents": parents,
        "children": children,
    }
    return group_payload, members, buckets


def split_push_batches(
    groups: List[Dict[str, Any]], *, max_children: int = SPLIT_CHILD_BATCH
) -> Iterable[List[Dict[str, Any]]]:
    """Batch claim groups into pushes bounded by their GHOST count.

    A group must travel WHOLE — its parents and its ghosts in one payload: the
    backend resolves the group's canonical parent from what it is given plus
    ``movement_split``, and reaps, for every group it is given, the ghosts
    absent from the payload. Splitting one group over two calls would make the
    second delete what the first created. So a group whose own ghost count
    exceeds the budget is pushed alone rather than cut — going over budget is
    survivable, losing ghosts is not.
    """
    batch: List[Dict[str, Any]] = []
    count = 0
    for group in groups:
        n = len(group.get("children") or [])
        if batch and count + n > max_children:
            yield batch
            batch, count = [], 0
        batch.append(group)
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
    resolutions. ``ValueDate`` feeds the per-key fan-out windows
    (``collect_lookup_inputs`` → ``_movement_date``).
    """
    return {
        "TransactionParticulars": row.get("TransactionParticulars")
        or row.get("transaction_particulars"),
        "PaymentOrderID_Ref": _dm_ref_no(row),
        "Remarks_1": _dm_remarks_1(row),
        "Initiating_channel": _channel_id(row),
        "ValueDate": row.get("ValueDate") or row.get("TransactionDate")
        or row.get("value_date"),
    }


def _build_member(
    entry: Dict[str, Any],
    movement_type: str,
    keys: List[Key],
    lot_id: str,
    *,
    claim: Optional[Key] = None,
    payment_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Member payload from the ALREADY-BUILT entry dict — the backend recomputes
    the finacle source_hash from these very fields, guaranteeing hash parity
    with the entry push.

    ``claim`` marks a GHOST member: the backend resolves the claim group's
    canonical parent from ``movement_split`` and re-anchors the hash on the
    canonical's account/dates, exactly like the split push did for the ghost
    entry — that is what keeps entry and member on one source_hash even when a
    later run re-emits the group off a different parent.
    """
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
        "claim_key_type": claim[0] if claim else None,
        "claim_key_value": claim[1] if claim else None,
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
    ghosts = [m for m in lot_members if m.get("claim_key_value")]

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
    groups: List[Dict[str, Any]],
    *,
    casing: Optional[Dict[str, List[str]]] = None,
    top_lots: int = 5,
    top_deltas: int = 5,
    top_keys: int = BB_DEBUG_TOP_KEYS,
    sample: int = BB_DEBUG_SAMPLE,
    sample_keys: int = BB_DEBUG_SAMPLE_KEYS,
) -> Optional[Dict[str, Any]]:
    """End-of-run report (pure — unit-tested without I/O): bucket sizes, the
    claim-group totals, and a deep dive on the BIGGEST bucket of the run.

    ``groups`` are the claim-group payloads about to be pushed. The number worth
    watching after a deploy is ``top_group_deltas``: Σ(parents) − Σ(ghosts) per
    group — exactly what the backend's second reconciliation will tag lots for.
    """
    if not members_buf and not groups:
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

    parents = [p for g in groups for p in (g.get("parents") or [])]
    deltas = []
    for g in groups:
        parent_total = sum(
            (_safe_decimal(p.get("amount")) for p in g.get("parents") or []), Decimal("0")
        )
        child_total = sum(
            (_safe_decimal(c.get("amount")) for c in g.get("children") or []), Decimal("0")
        )
        deltas.append(
            {
                "claim": f"{g.get('claim_key_type')}:{g.get('claim_key_value')}",
                "parents": len(g.get("parents") or []),
                "children": len(g.get("children") or []),
                "delta": str(parent_total - child_total),
            }
        )
    deltas.sort(key=lambda d: -abs(Decimal(d["delta"])))

    kinds = Counter(key.kind for key in buckets.values())
    single = {lid for lid, n in sizes.items() if n == 1}
    single_ghosts = sum(
        1 for m in members_buf
        if m["lot_id"] in single and m.get("claim_key_value")
    )
    return {
        "lots_total": len(sizes),
        "members_total": len(members_buf),
        "ghost_members_total": sum(1 for m in members_buf if m.get("claim_key_value")),
        # Buckets this run fed exactly ONE member. A real single waiting for its
        # counterpart is normal; thousands of single GHOSTS mean a fan-out is
        # claiming payments whose counterpart can never arrive (the 2026-08
        # label over-fetch showed up as ~19 000 of these).
        "single_member_buckets": len(single),
        "single_member_ghost_buckets": single_ghosts,
        "bucket_kinds": dict(kinds),
        "claim_groups": len(groups),
        "split_parents": len(parents),
        "parents_with_payment_gap": sum(
            1 for p in parents
            if _safe_decimal(p.get("amount")) != _safe_decimal(p.get("payment_amount"))
        ),
        "groups_sharing_a_key": sum(
            1 for g in groups if len(g.get("parents") or []) > 1
        ),
        "max_shared_key": max(
            (len(g.get("parents") or []) for g in groups), default=0
        ),
        "groups_with_delta": sum(1 for d in deltas if Decimal(d["delta"]) != 0),
        "top_group_deltas": deltas[:top_deltas],
        # Aggregate keys the datamart spelled several ways. Folded into one
        # bucket each — this counts how often that mattered.
        "casing_conflicts": len(casing or {}),
        "top_lots": [{"lot_id": lid, "members": n} for lid, n in sizes.most_common(top_lots)],
        "biggest_lot": report,
    }


def parse_trace_keys(raw: str) -> List[Key]:
    """'PACS008:26070…,PO:123' → [(type, value), …]. Values may contain ':'
    (MSGIDs with timestamps): only the FIRST colon splits.

    The value is uppercased like the stored keys, so tracing a key copied from
    the datamart in its original casing still finds it.
    """
    keys: List[Key] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        kt, kv = part.split(":", 1)
        if kt.strip() and kv.strip():
            keys.append((kt.strip().upper(), kv.strip().upper()))
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


def resolve_ndgb_payments(
    lookup_conn, windows: Dict[str, Window]
) -> Dict[str, List[Tuple[str, str, Decimal]]]:
    """MessageID -> [(pacs008, PaymentNumber, amount)], bounded PER KEY to the
    window of the movements carrying it (NDGB Remarks_1 + resolved IP refs).

    A MessageID is frequently a reused label, and an unbounded join drags in its
    whole multi-year history — 17,96 Md€ claimed for 2,17 Md€ booked in prod.
    So the temp table carries (key, dmin, dmax) and the join only accepts
    payments whose ``CreatedOn`` falls inside the key's window. NULL CreatedOn
    is excluded unless RECO_FINACLE_BB_MSGID_INCLUDE_NULL_CREATED is set: a
    payment that cannot be dated cannot be attributed to a period either.

    No PRIMARY KEY on the temp table, same reason as ``_temp_join`` (the
    datamart's collation is case-insensitive; Python dedup is not).
    """
    rows_in: List[Tuple[str, date, date]] = []
    seen: Set[str] = set()
    open_keys = 0
    for msgid, window in windows.items():
        key = _clean(msgid)
        if not key or key in seen:
            continue
        seen.add(key)
        dmin, dmax = window_bounds(window)
        if (dmin, dmax) == OPEN_WINDOW:
            open_keys += 1
        rows_in.append((key, dmin, dmax))
    if open_keys:
        logger.warning(
            "[ingest_finacle_bb] %d MessageID key(s) carried only by undated "
            "movements — fanned out with an OPEN window",
            open_keys,
        )
    if not rows_in:
        return {}

    null_clause = " OR p.CreatedOn IS NULL" if MSGID_INCLUDE_NULL_CREATED else ""
    cursor = lookup_conn.cursor()
    try:
        cursor.execute("IF OBJECT_ID('tempdb..#reco_bb_msg') IS NOT NULL DROP TABLE #reco_bb_msg")
        cursor.execute(
            "CREATE TABLE #reco_bb_msg "
            "(k VARCHAR(128) NOT NULL, dmin DATE NOT NULL, dmax DATE NOT NULL)"
        )
        cursor.fast_executemany = True
        for i in range(0, len(rows_in), PO_INSERT_BATCH):
            cursor.executemany(
                "INSERT INTO #reco_bb_msg (k, dmin, dmax) VALUES (?, ?, ?)",
                rows_in[i : i + PO_INSERT_BATCH],
            )
        cursor.execute("CREATE CLUSTERED INDEX ix_temp_k ON #reco_bb_msg (k)")
        cursor.execute(
            "SELECT t.k, LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs, "
            "       LTRIM(RTRIM(p.PaymentNumber)) AS po, "
            f"      p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_bb_msg t "
            "INNER JOIN std.Payment p ON p.MessageID = t.k "
            "WHERE p.IsCurrent = 1 "
            f"  AND ((p.CreatedOn >= t.dmin AND p.CreatedOn < t.dmax){null_clause})"
        )
        rows = cursor.fetchall()
        cursor.execute("IF OBJECT_ID('tempdb..#reco_bb_msg') IS NOT NULL DROP TABLE #reco_bb_msg")
    finally:
        cursor.close()

    result = _collect_payments(rows)
    logger.info(
        "[ingest_finacle_bb] resolved payments for %d/%d windowed MessageIDs",
        len(result), len(rows_in),
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
        conflicts = casing_conflicts(list(descriptors) + list(unresolved))
        if conflicts:
            logger.warning(
                "[ingest_finacle_bb] %s/%s: %d aggregate key(s) spelled several "
                "ways by the datamart — folded to one bucket each; e.g. %s",
                flow_code, source_code, len(conflicts),
                dict(sorted(conflicts.items())[:3]),
            )
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
        if inputs.rejected_remarks:
            # A Remarks_1 refused as a pacs008 (IBAN / UUID — counterparty data).
            # Loud on purpose: an unhandled TP shape used to become one lot per
            # movement in silence, and only a lot count gave it away.
            logger.warning(
                "[ingest_finacle_bb] %s/%s: %d movement(s) whose Remarks_1 is not "
                "a pacs008 — no bucket minted from it, retried next run; by shape: %s",
                flow_code, source_code, sum(inputs.rejected_remarks.values()),
                dict(inputs.rejected_remarks.most_common(5)),
            )

        # One temp-table join per family resolves everything at once.
        pacs_map = resolve_sp_payments(lookup_conn, inputs.sp_pacs008)
        # The IP aggregates' TransactionRef yields their MessageID, which must be
        # in the msgid_map for the fan-out to reach the bulk's payments — hence
        # this join BEFORE resolve_ndgb_payments, whose input it feeds. Their
        # windows are merged in: both shapes claim the group, so the fan-out
        # window must cover both (see merge_msgid_windows).
        txnref_map, _ = resolve_reversals(lookup_conn, list(inputs.instant_txn_refs))
        msgid_windows = merge_msgid_windows(
            inputs.ndgb_msgids, inputs.instant_txn_refs, txnref_map
        )
        msgid_map = resolve_ndgb_payments(lookup_conn, msgid_windows)
        po_map = resolve_po_payments(lookup_conn, inputs.po_ids)
        return_map = resolve_return_payments(lookup_conn, inputs.return_po_ids)
        maps = (pacs_map, msgid_map, po_map, return_map)

        members_buf: List[Dict[str, Any]] = []
        buckets: Dict[str, BucketKey] = {}
        # claim -> {"partition": …, "parents": {identity: payload}} — filled by
        # _plan, emitted ONCE per claim after both passes (plan_claim_group).
        split_state: Dict[Key, Dict[str, Any]] = {}

        def _plan(record: Dict[str, Any], entry: Dict[str, Any]) -> List[Dict[str, Any]]:
            """Plan one movement and buffer what it contributes → entries to push."""
            resolution = movement_resolution(record, *maps, txnref_map=txnref_map)
            plan = plan_movement(
                entry,
                classify_bb_movement(record) or "?",
                resolution,
                flow_source_id=source_id,
            )
            members_buf.extend(plan.members)
            buckets.update(plan.buckets)
            if plan.parent is not None and plan.claim is not None:
                state = split_state.setdefault(
                    plan.claim, {"partition": plan.partition, "parents": {}}
                )
                # An overlap re-stream and the unresolved retry can both see one
                # movement — keyed on its identity so it registers once.
                identity = (
                    plan.parent.get("external_ref"),
                    plan.parent.get("account"),
                    str(plan.parent.get("value_date"))[:10],
                    str(plan.parent.get("operation_date"))[:10],
                )
                state["parents"][identity] = plan.parent
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

        # Emit each claim group ONCE: its ghosts are its buckets' exact payment
        # sums, however many movements claim the key. Deterministic order so a
        # retried run pushes identical payloads.
        groups_buf: List[Dict[str, Any]] = []
        for claim in sorted(split_state):
            state = split_state[claim]
            parents = sorted(
                state["parents"].values(),
                key=lambda p: (str(p.get("value_date"))[:10], str(p.get("external_ref"))),
            )
            group_payload, group_members, group_buckets = plan_claim_group(
                claim, state["partition"], parents, flow_source_id=source_id
            )
            members_buf.extend(group_members)
            buckets.update(group_buckets)
            groups_buf.append(group_payload)

        # End-of-run debug, logged BEFORE the pushes so a failed push still
        # leaves the report in the Airflow logs.
        report = bucket_debug_report(members_buf, buckets, groups_buf, casing=conflicts)
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

        # Splits FIRST: each batch registers its groups' parents, materialises
        # the group ghosts and withdraws the real movements in one transaction,
        # so no intermediate state has a movement and its ghosts both counting.
        base = {"flow_code": flow_code, "source_code": source_code}
        parents_total = sum(len(g["parents"]) for g in groups_buf)
        ghosts_total = sum(len(g["children"]) for g in groups_buf)
        if groups_buf:
            logger.info(
                "[ingest_finacle_bb] %s/%s: pushing %d claim group(s) — "
                "%d split parent(s) / %d ghost(s)",
                flow_code, source_code, len(groups_buf), parents_total, ghosts_total,
            )
        for batch in split_push_batches(groups_buf):
            finacle_bb_post_split_batch(
                {**base, "run_id": run_id, "groups": batch}
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
            "%d member(s), %d bucket(s), %d claim group(s) / %d split parent(s))",
            flow_code, source_code, run_id, total, len(unresolved),
            len(members_buf), len(lots_payload), len(groups_buf), parents_total,
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
