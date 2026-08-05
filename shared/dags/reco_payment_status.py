"""Payment statuses (std.Payment.Status) for the app's PENDING movements.

The perimeter is the APP, not the datamart window: every run fetches the
still-PENDING finacle entries from the backend (paginated), resolves the
payments each movement covers, and upserts DISTINCT ``(reco_id, po_id, status,
amount)`` rows — so a payment pending for longer than any backfill window stays covered,
and statuses stop being tracked once the movement is reconciled. First launch:
flip ``PS_FULL_SYNC`` below to also seed the already-matched movements once.

PO resolution per movement type (TP prefix, mirrors the reco_id extractors):
- SCTXB/SDDXB/SDXBB direct (``PREFIX/I|O/…``): ``Remarks_1`` = the bulk message
  id → every ``std.Payment`` row via ``MessageIDPACS008`` (pacs008 bulks),
  falling back to ``MessageID`` (BLK bulk ids that only live there — no reliable
  typographic rule, so we always try pacs008 first then MessageID).
- SCTXB/SDDXB/SDXBB return (``PREFIX/NCC|NCP/…``): its own PO (``ref_no``,
  fallback TP segment[3]).
- SCTXB/SDDXB/SDXBB reject (``PREFIX/RCC/…``) and NDRT (reject-of-return):
  ``ref_no`` = RETURN PaymentNumber → ``std.[Return]`` (IsCurrent=1) →
  ``OriginalPo`` → the ORIGINAL payment's Status (po_id stored = OriginalPo).
- NDGB: ``Remarks_1`` = MessageID → every payment with that ``MessageID``.
- NDRJ: PO in ``ref_no`` (``paysis##<po>``).
- SWIFT/SCRT1/BKRTP singles: PO = ``ref_no``.
- No structured TP + channel Z6/Z9 (MOSEL/Webripost): PO = ``ref_no``.
"""
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from reco_common import (
    finacle_get_payment_status_movements,
    finacle_post_payment_status_batch,
    task_list_finacle_sources,
)
from reco_datamart import (
    BULK_PREFIXES,
    BULK_REJECT_SEGS,
    BULK_RETURN_SEGS,
    CHANNEL_REF_NO,
    DATAMART_CONN_ID,
    INSTANT_PREFIXES,
    NDRT_PREFIX,
    PAYMENT_AMOUNT_COL,
    _channel_id,
    _clean,
    _dm_ref_no,
    _dm_remarks_1,
    _tp_parts,
    get_pyodbc_conn,
)
from reco_datamart_bb import (
    NDGB_PREFIX,
    NDRJ_PREFIX,
    _temp_join,
    ndrj_po_id,
    sp_return_po_id,
)

logger = logging.getLogger(__name__)

# First-launch switch: True → sync EVERY entry of the flow (live + émargement)
# once, so already-matched movements get their Payments column too; put it
# back to False (pending-only) afterwards. Env override for Airflow:
# RECO_FINACLE_PS_FULL_SYNC=1.
PS_FULL_SYNC = os.environ.get("RECO_FINACLE_PS_FULL_SYNC", "").strip().lower() in (
    "1", "true", "yes",
)

PS_FETCH_BATCH = int(os.environ.get("RECO_FINACLE_PS_FETCH_BATCH", "2000"))
PS_PUSH_BATCH = int(os.environ.get("RECO_FINACLE_PS_PUSH_BATCH", "2000"))

# reco_id sentinel for movements whose schema isn't handled yet (mirrors
# backend UNRESOLVED_RECO_ID). Such movements — and NULL/empty reco_id — are not
# a real reconciliation group, so their payments are never keyed/pushed.
UNRESOLVED_RECO_ID = "Not Supported"

# Finacle datamart parsers whose movements carry std.Payment references.
_PS_PARSER_TYPES = (None, "finacle_db", "finacle_batch_booking_true")

# po_id -> (status or None, amount str or None); pacs/msgid/return maps hold
# [(po_id, status, amount), …]. amount is stringified (Decimal-safe over JSON,
# same convention as movement "amount"); the backend coerces it to Decimal.
PoStatus = Tuple[str, Optional[str], Optional[str]]


def _to_amount(value: Any) -> Optional[str]:
    """std.Payment amount -> clean string for the wire (or None)."""
    if value is None:
        return None
    s = _clean(str(value))
    return s or None


def _flatten_pos(
    grouped: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]]
) -> Dict[str, List[PoStatus]]:
    """{key: {po: (status, amount)}} -> {key: [(po, status, amount), …]}
    (sorted by po; po values are the unique PaymentNumbers of the group)."""
    return {
        key: sorted(
            ((po, sa[0], sa[1]) for po, sa in per_po.items()),
            key=lambda t: t[0],
        )
        for key, per_po in grouped.items()
    }


# ---------------------------------------------------------------------------
# Pure functions (unit-tested without any I/O)
# ---------------------------------------------------------------------------

def to_movement_shape(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Backend movement -> the dict shape the shared helpers read. The
    lowercase entry fields are already handled by ``_tp_parts``/``_dm_*``;
    only the channel needs remapping (``_channel_id`` reads
    ``Initiating_channel``/payload_raw, never the lowercase key)."""
    row = dict(entry)
    if entry.get("initiating_channel") is not None:
        row["Initiating_channel"] = entry["initiating_channel"]
    return row


def collect_ps_lookup_inputs(
    rows: Iterable[Dict[str, Any]],
    pacs_ids: set,
    msgids: set,
    po_ids: set,
    return_po_ids: set,
) -> None:
    """Accumulate the std.Payment / std.[Return] lookup inputs of one batch."""
    for row in rows:
        parts = _tp_parts(row)
        if not parts:
            if _channel_id(row) in CHANNEL_REF_NO:
                po = _clean(_dm_ref_no(row))
                if po:
                    po_ids.add(po)
            continue
        prefix = parts[0].strip().upper()
        seg1 = parts[1].strip().upper() if len(parts) > 1 else ""
        if prefix in BULK_PREFIXES:
            if seg1 in BULK_REJECT_SEGS:
                po = sp_return_po_id(row, parts)
                if po:
                    return_po_ids.add(po)
            elif seg1 in BULK_RETURN_SEGS:
                po = sp_return_po_id(row, parts)
                if po:
                    po_ids.add(po)
            else:
                # Direct bulk: remarks_1 is the bulk message id — usually a
                # pacs008 (MessageIDPACS008), but some carry a MessageID-only
                # bulk id (BLK…). Feed BOTH resolvers; movement_payment_pos
                # tries pacs008 first, then MessageID.
                bulk_id = _clean(_dm_remarks_1(row))
                if bulk_id:
                    pacs_ids.add(bulk_id)
                    msgids.add(bulk_id)
        elif prefix == NDGB_PREFIX:
            msgid = _clean(_dm_remarks_1(row))
            if msgid:
                msgids.add(msgid)
        elif prefix == NDRJ_PREFIX:
            po = ndrj_po_id(row)
            if po:
                po_ids.add(po)
        elif prefix == NDRT_PREFIX:
            po = ndrj_po_id(row)
            if po:
                return_po_ids.add(po)
        elif prefix in INSTANT_PREFIXES:
            po = _clean(_dm_ref_no(row))
            if po:
                po_ids.add(po)


def _single_po(po: str, po_status: Dict[str, Tuple[Optional[str], Optional[str]]]) -> List[PoStatus]:
    """One (po_id, status, amount) row for a single-payment movement. A PO
    extracted from the movement but absent from std.Payment still yields a row
    (status/amount None → shown as '?')."""
    st, amt = po_status.get(po, (None, None))
    return [(po, st, amt)]


def movement_payment_pos(
    row: Dict[str, Any],
    pacs_map: Dict[str, List[PoStatus]],
    msgid_map: Dict[str, List[PoStatus]],
    po_status: Dict[str, Optional[str]],
    return_status: Optional[Dict[str, List[PoStatus]]] = None,
) -> List[PoStatus]:
    """(po_id, status) pairs of one movement; [] when it carries no payment
    reference. A PO extracted from the movement but absent from std.Payment
    is still returned (status None → shown as '?'); NDRT//RCC rows resolve
    to the ORIGINAL payment (po_id = OriginalPo) and yield [] until their
    std.[Return] row exists."""
    parts = _tp_parts(row)
    if not parts:
        if _channel_id(row) in CHANNEL_REF_NO:
            po = _clean(_dm_ref_no(row))
            return _single_po(po, po_status) if po else []
        return []
    prefix = parts[0].strip().upper()
    seg1 = parts[1].strip().upper() if len(parts) > 1 else ""
    if prefix in BULK_PREFIXES:
        if seg1 in BULK_REJECT_SEGS:
            po = sp_return_po_id(row, parts)
            return list((return_status or {}).get(po, [])) if po else []
        if seg1 in BULK_RETURN_SEGS:
            po = sp_return_po_id(row, parts)
            return _single_po(po, po_status) if po else []
        bulk_id = _clean(_dm_remarks_1(row))
        if not bulk_id:
            return []
        # pacs008 (MessageIDPACS008) first, fallback to MessageID (BLK bulk ids).
        return list(pacs_map.get(bulk_id) or msgid_map.get(bulk_id) or [])
    if prefix == NDGB_PREFIX:
        msgid = _clean(_dm_remarks_1(row))
        return list(msgid_map.get(msgid, [])) if msgid else []
    if prefix == NDRJ_PREFIX:
        po = ndrj_po_id(row)
        return _single_po(po, po_status) if po else []
    if prefix == NDRT_PREFIX:
        po = ndrj_po_id(row)
        return list((return_status or {}).get(po, [])) if po else []
    if prefix in INSTANT_PREFIXES:
        po = _clean(_dm_ref_no(row))
        return _single_po(po, po_status) if po else []
    return []


def build_status_rows(reco_id: str, pos: List[PoStatus]) -> List[Dict[str, Any]]:
    """Wire rows for POST /tasks/finacle/payment-status/batch: one
    (reco_id, po_id, status, amount) per payment. Keyed by the movement's
    reconciliation group (the lot uuid for batch bookings), so the whole group's
    payments are stored once instead of per member entry."""
    return [
        {"reco_id": reco_id, "po_id": po, "status": status, "amount": amount}
        for po, status, amount in pos
    ]


# ---------------------------------------------------------------------------
# std.Payment / std.[Return] lookups (batched temp-table joins)
# ---------------------------------------------------------------------------

def resolve_pacs_payment_statuses(lookup_conn, pacs_ids) -> Dict[str, List[PoStatus]]:
    """pacs008 -> [(PaymentNumber, Status, Amount)] for every payment of the bulk."""
    rows = _temp_join(
        lookup_conn,
        pacs_ids,
        temp_name="#reco_ps_pacs",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.PaymentNumber)) AS po, "
            "       LTRIM(RTRIM(p.Status)) AS st, "
            f"       p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_ps_pacs t "
            "INNER JOIN std.Payment p ON p.MessageIDPACS008 = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]] = {}
    for pacs, po, st, amt in rows:
        pacs, po = _clean(pacs), _clean(po)
        if pacs and po:
            result.setdefault(pacs, {})[po] = (_clean(st), _to_amount(amt))
    logger.info(
        "[sync_payment_status] resolved payments for %d/%d pacs008 ids",
        len(result), len({_clean(p) for p in pacs_ids if _clean(p)}),
    )
    return _flatten_pos(result)


def resolve_msgid_payment_statuses(lookup_conn, msgids) -> Dict[str, List[PoStatus]]:
    """MessageID -> [(PaymentNumber, Status, Amount)] (NDGB aggregates)."""
    rows = _temp_join(
        lookup_conn,
        msgids,
        temp_name="#reco_ps_msg",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.PaymentNumber)) AS po, "
            "       LTRIM(RTRIM(p.Status)) AS st, "
            f"       p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_ps_msg t "
            "INNER JOIN std.Payment p ON p.MessageID = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]] = {}
    for msgid, po, st, amt in rows:
        msgid, po = _clean(msgid), _clean(po)
        if msgid and po:
            result.setdefault(msgid, {})[po] = (_clean(st), _to_amount(amt))
    logger.info(
        "[sync_payment_status] resolved payments for %d/%d NDGB MessageIDs",
        len(result), len({_clean(m) for m in msgids if _clean(m)}),
    )
    return _flatten_pos(result)


def resolve_po_statuses(lookup_conn, po_ids) -> Dict[str, Tuple[Optional[str], Optional[str]]]:
    """PaymentNumber -> (Status, Amount) (singles, returns, rejects)."""
    rows = _temp_join(
        lookup_conn,
        po_ids,
        temp_name="#reco_ps_po",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(p.Status)) AS st, "
            f"       p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_ps_po t "
            "INNER JOIN std.Payment p ON p.PaymentNumber = t.k "
            "WHERE p.IsCurrent = 1"
        ),
    )
    result: Dict[str, Tuple[Optional[str], Optional[str]]] = {}
    for po, st, amt in rows:
        po = _clean(po)
        if po:
            result[po] = (_clean(st), _to_amount(amt))
    logger.info(
        "[sync_payment_status] resolved status for %d/%d PO ids",
        len(result), len({_clean(p) for p in po_ids if _clean(p)}),
    )
    return result


def resolve_return_statuses(lookup_conn, return_po_ids) -> Dict[str, List[PoStatus]]:
    """Return PaymentNumber -> [(OriginalPo, Status of the ORIGINAL payment)]
    via std.[Return] (reserved word — bracketed, IsCurrent=1). LEFT JOIN on
    std.Payment: a known OriginalPo with no payment row yet still yields a
    (po_id, None) row ('?' in the UI)."""
    rows = _temp_join(
        lookup_conn,
        return_po_ids,
        temp_name="#reco_ps_ret",
        join_sql=(
            "SELECT t.k, LTRIM(RTRIM(r.OriginalPo)) AS orig_po, "
            "       LTRIM(RTRIM(p.Status)) AS st, "
            f"       p.{PAYMENT_AMOUNT_COL} AS amt "
            "FROM #reco_ps_ret t "
            "INNER JOIN std.[Return] r ON r.PaymentNumber = t.k "
            "LEFT JOIN std.Payment p "
            "  ON p.PaymentNumber = r.OriginalPo AND p.IsCurrent = 1 "
            "WHERE r.IsCurrent = 1"
        ),
    )
    result: Dict[str, Dict[str, Tuple[Optional[str], Optional[str]]]] = {}
    for ret_po, orig_po, st, amt in rows:
        ret_po, orig_po = _clean(ret_po), _clean(orig_po)
        if ret_po and orig_po:
            result.setdefault(ret_po, {})[orig_po] = (_clean(st), _to_amount(amt))
    logger.info(
        "[sync_payment_status] resolved originals for %d/%d return PO ids",
        len(result), len({_clean(p) for p in return_po_ids if _clean(p)}),
    )
    return _flatten_pos(result)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _fetch_flow_movements(flow_code: str, scope: str) -> List[Dict[str, Any]]:
    """All movements of the flow needing a sync (paginated backend calls)."""
    movements: List[Dict[str, Any]] = []
    skip = 0
    while True:
        batch = finacle_get_payment_status_movements(
            flow_code, scope=scope, skip=skip, limit=PS_FETCH_BATCH
        ).get("movements", [])
        movements.extend(to_movement_shape(m) for m in batch)
        if len(batch) < PS_FETCH_BATCH:
            return movements
        skip += PS_FETCH_BATCH


def _sync_flow(lookup_conn, flow_code: str, scope: str) -> Dict[str, int]:
    """Sync one flow: fetch its app movements, resolve every payment family in
    four temp-table joins, push DISTINCT (reco_id, po_id, status, amount) rows."""
    movements = _fetch_flow_movements(flow_code, scope)
    logger.info(
        "[sync_payment_status] %s: %d movement(s) to sync (scope=%s)",
        flow_code, len(movements), scope,
    )
    if not movements:
        return {"movements": 0, "rows": 0, "skipped": 0}

    pacs_ids: set = set()
    msgids: set = set()
    po_ids: set = set()
    return_po_ids: set = set()
    collect_ps_lookup_inputs(movements, pacs_ids, msgids, po_ids, return_po_ids)

    pacs_map = resolve_pacs_payment_statuses(lookup_conn, pacs_ids)
    msgid_map = resolve_msgid_payment_statuses(lookup_conn, msgids)
    po_status = resolve_po_statuses(lookup_conn, po_ids)
    return_status = resolve_return_statuses(lookup_conn, return_po_ids)

    rows_buf: List[Dict[str, Any]] = []
    pushed = skipped_rows = with_payments = 0
    # (reco_id, po_id) already queued this run — collapses the batch-booking
    # fan-out (many movements share a reco_id and resolve the same payments) so
    # we push the payment set of a reconciliation group ONCE, not per member.
    seen: set = set()

    def _flush() -> None:
        nonlocal pushed, skipped_rows, rows_buf
        for i in range(0, len(rows_buf), PS_PUSH_BATCH):
            result = finacle_post_payment_status_batch(
                {"flow_code": flow_code, "rows": rows_buf[i : i + PS_PUSH_BATCH]}
            )
            data = result.get("data") or {}
            pushed += (
                (data.get("inserted") or 0)
                + (data.get("updated") or 0)
                + (data.get("unchanged") or 0)
            )
            skipped_rows += data.get("skipped") or 0
        rows_buf = []

    for movement in movements:
        reco_id = _clean(movement.get("reco_id"))
        if not reco_id or reco_id == UNRESOLVED_RECO_ID:
            continue  # no real reconciliation group to key the payments on
        pos = movement_payment_pos(movement, pacs_map, msgid_map, po_status, return_status)
        if not pos:
            continue
        with_payments += 1
        for row in build_status_rows(reco_id, pos):
            key = (row["reco_id"], row["po_id"])
            if key in seen:
                continue
            seen.add(key)
            rows_buf.append(row)
        if len(rows_buf) >= PS_PUSH_BATCH:
            _flush()
    _flush()

    logger.info(
        "[sync_payment_status] %s: %d/%d movement(s) with payments -> %d distinct "
        "status row(s) synced (%d skipped)",
        flow_code, with_payments, len(movements), pushed, skipped_rows,
    )
    return {"movements": with_payments, "rows": pushed, "skipped": skipped_rows}


def run_payment_status_sync(dag_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Sync payment statuses for every flow with a finacle datamart source."""
    flow_codes: List[str] = []
    for s in task_list_finacle_sources().get("sources", []):
        if s.get("parser_type") in _PS_PARSER_TYPES and s["flow_code"] not in flow_codes:
            flow_codes.append(s["flow_code"])
    summary: Dict[str, Any] = {"synced": [], "errors": []}
    if not flow_codes:
        logger.info("[sync_payment_status] No active finacle source — nothing to do.")
        return summary

    scope = "all" if PS_FULL_SYNC else "pending"
    if PS_FULL_SYNC:
        logger.warning(
            "[sync_payment_status] FULL SYNC enabled — syncing every entry "
            "(live + émargement) of %d flow(s)", len(flow_codes),
        )

    # Local import: keeps this module importable (tests, DAG parsing) without
    # the MSSQL provider installed. One connection: lookups only, no streaming.
    from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook

    hook = MsSqlHook(mssql_conn_id=DATAMART_CONN_ID)
    lookup_conn = get_pyodbc_conn(hook)
    try:
        for flow_code in flow_codes:
            try:
                _sync_flow(lookup_conn, flow_code, scope)
                summary["synced"].append(flow_code)
            except Exception as exc:  # noqa: BLE001
                logger.error("[sync_payment_status] %s failed: %s", flow_code, exc, exc_info=True)
                summary["errors"].append({"flow": flow_code, "error": str(exc)})
    finally:
        try:
            lookup_conn.close()
        except Exception:  # noqa: BLE001
            pass

    if summary["errors"]:
        raise RuntimeError(f"payment status sync finished with errors: {summary['errors']}")
    return summary
