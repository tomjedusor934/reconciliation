"""Datamart (MSSQL) extraction helpers for the Finacle ingestion DAG.

The DAG connects to the datamart through the Airflow connection
``RECO_DATAMART_CONN_ID`` (default ``datamart``) — same pattern as the
datamart ETL (`MsSqlHook` + raw pyodbc, chunked `fetchmany` reads). For each
finacle_db source it pulls ``std.Movement`` rows for the source's reference
accounts, **computes the reconciliation key itself** from
``TransactionParticulars`` (see ``compute_reco_id`` + ``example/flow_schema.txt``,
hitting ``std.Payment`` for bulk returns), and pushes the entries to the backend
in batches. Movements whose key can't be resolved yet are still ingested and
re-enriched on the next run (the backend returns them via /tasks/finacle/unresolved).
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import logging
import os
from typing import Any, Dict, Iterator, List, Optional, Tuple

from reco_common import (
    finacle_complete_run,
    finacle_get_bulk_keys,
    finacle_list_unresolved,
    finacle_post_batch,
    finacle_post_bulk_keys,
    finacle_start_run,
    task_list_finacle_sources,
)

logger = logging.getLogger(__name__)

DATAMART_CONN_ID = os.environ.get("RECO_DATAMART_CONN_ID", "datamart")
CHUNK_SIZE = int(os.environ.get("RECO_FINACLE_CHUNK_SIZE", "5000"))
OVERLAP_DAYS = int(os.environ.get("RECO_FINACLE_OVERLAP_DAYS", "2"))
# First-ever run of a source: full backfill so nothing is lost (the per-source
# watermark guarantees resume-where-it-stopped for every subsequent run). A
# configurable floor avoids scanning the whole table when bounded history suffices.
BACKFILL_SINCE = os.environ.get("RECO_FINACLE_BACKFILL_SINCE", "2026-06-29").strip()
PO_INSERT_BATCH = 10000  # rows per executemany when filling the #reco_po temp table
_MAX_REF_LEN = 64        # the lookup temp tables are VARCHAR(64) — longer keys would error

# reco_id classification from TransactionParticulars (see example/flow_schema.txt).
INSTANT_PREFIXES = {"SWIFT", "SCRT1", "BKRTP"}       # ref_no filled -> instant payment
BULK_PREFIXES = {"SCTXB", "SDDXB", "SDXBB"}  # no ref_no -> bulk payment
BULK_DIRECT_SEGS = {"I", "O"}                # PREFIX/I|O/...        -> remarks_1 (BLK)
BULK_RETURN_SEGS = {"NCC", "NCP"}            # PREFIX/NCC|NCP/I|O/PO_ID/... -> std.Payment
BULK_REJECT_SEGS = {"RCC"}                   # PREFIX/RCC/... reject -> std.[Return]
NDRT_PREFIX = "NDRT"                         # reject-of-return -> std.[Return]
UNRESOLVED_RECO_ID = "Not Supported"         # known prefix, unhandled schema
# MOSEL / Webripost movements have no '/'-structured TransactionParticulars
# (empty or plain text) and are identified by Initiating_channel → reco_id in ref_no.
CHANNEL_REF_NO = {"Z6", "Z9"}                # Z6 = MOSEL (ATM), Z9 = Webripost

# An instant payment is normally 1 PaymentNumber = 1 MessageID (1-1), which is why
# ref_no alone can serve as reco_id. Payments coming from *multiline* break that:
# N instant payments share one MessageID and are booked in BULK, so the counterpart
# is a single bulk line while our N movements stay keyed on N distinct PO ids —
# the group can never balance (see find_balanced_groups). Several PO ids on the same
# MessageID is therefore the general bulk signal; InitModule catches the rest.
MULTILINE_INIT_MODULES = {"MLTLIP"}          # std.Payment.InitModule -> bulk even with 1 PO
PAYMENT_INIT_MODULE_COLUMN = "InitModule"    # datamart column name — unconfirmed, one line to fix
# off | log (dry-run: compute + log, write nothing) | on
BULK_REGROUP_MODE = os.environ.get("RECO_FINACLE_BULK_REGROUP", "on").strip().lower()


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_pyodbc_conn(hook):
    """Native pyodbc connection from an Airflow MsSqlHook connection.

    Tries ODBC Driver 18 then 17 (mirrors pyodbc_helpers.py from the datamart project).
    """
    import pyodbc

    conn_cfg = hook.get_connection(hook.mssql_conn_id)

    available = pyodbc.drivers()
    driver = None
    for candidate in ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"]:
        if candidate in available:
            driver = candidate
            break
    if driver is None:
        raise RuntimeError(
            f"No supported MSSQL ODBC driver found. Available drivers: {available}. "
            "Install 'ODBC Driver 18 for SQL Server' or 'ODBC Driver 17 for SQL Server'."
        )

    connection_string = (
        f"DRIVER={{{driver}}};"
        f"SERVER={conn_cfg.host},{conn_cfg.port or 1433};"
        f"DATABASE={conn_cfg.schema};"
        f"UID={conn_cfg.login};"
        f"PWD={conn_cfg.password};"
        "TrustServerCertificate=yes;"
        "Encrypt=yes;"
    )
    return pyodbc.connect(connection_string)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def build_movement_query(nb_accounts: int) -> str:
    """Incremental extraction of current movements (reco_id is computed here, so
    the datamart's ReconciliationID column is ignored)."""
    placeholders = ",".join("?" * nb_accounts)
    return (
        "SELECT * FROM std.Movement "
        "WHERE IsCurrent = 1 "
        f"AND AccountNumber IN ({placeholders}) "
        "AND TransactionDate >= ?"
    )


def iter_movement_chunks(
    conn,
    *,
    accounts: List[str],
    since: datetime,
    chunk_size: int = CHUNK_SIZE,
) -> Iterator[List[Dict[str, Any]]]:
    """Stream std.Movement rows as dicts, chunk by chunk (fetchmany pattern)."""
    cursor = conn.cursor()
    try:
        cursor.execute(build_movement_query(len(accounts)), [*accounts, since])
        columns = [d[0] for d in cursor.description]
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                break
            yield [dict(zip(columns, row)) for row in rows]
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Mapping std.Movement -> reconciliation entry (validated mapping)
# ---------------------------------------------------------------------------

def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _to_iso_datetime(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).isoformat()
    raise ValueError(f"not a date: {value!r}")


def _clean(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    return s or None

def _dm_ref_no(row: Dict[str, Any]) -> Any:
    """ref_no lives in std.Movement column ``PaymentOrderID_Ref`` (the sample
    extract called it ``ref_no`` — kept as a fallback)."""
    val = row.get("PaymentOrderID_Ref")
    return row.get("ref_no") if val is None else val


def _dm_remarks_1(row: Dict[str, Any]) -> Any:
    """remarks_1 lives in std.Movement column ``Remarks_1`` (fallback ``remarks_1``)."""
    val = row.get("Remarks_1")
    return row.get("remarks_1") if val is None else val


def _channel_id(row: Dict[str, Any]) -> Optional[str]:
    """Initiating_channel (uppercased). Reads the raw column, then falls back to
    payload_raw so it also works on re-pushed app entries (which carry the full
    original row under ``payload_raw``)."""
    # if transaction particlars has something in then return none
    # if _clean(row.get("TransactionParticulars")):
    #     return None
    val = _clean(row.get("Initiating_channel"))
    if val is None:
        payload = row.get("payload_raw")
        if isinstance(payload, dict):
            val = _clean(payload.get("Initiating_channel"))
    return val.upper() if val else None

# ---------------------------------------------------------------------------
# reco_id computation (flow_schema-driven; std.Payment for bulk returns)
# ---------------------------------------------------------------------------

def _tp_parts(row: Dict[str, Any]) -> List[str]:
    """Split TransactionParticulars into its '/'-separated segments.

    The datamart column is ``TransactionParticulars`` (CamelCase); the lowercase
    fallback keeps this working if ever fed a normalized/app entry.
    """
    tp = _clean(row.get("TransactionParticulars")) or _clean(row.get("transaction_particulars"))

    if not tp:
        return []

    if "##" in tp:
        return tp.split("##")

    return tp.split("/")


def payment_po_id_for(record: Dict[str, Any]) -> Optional[str]:
    """PO_ID needing a std.Payment lookup (bulk RETURN), else None."""
    parts = _tp_parts(record)
    if (
        len(parts) >= 4
        and parts[0].strip().upper() in BULK_PREFIXES
        and parts[1].strip().upper() in BULK_RETURN_SEGS
    ):
        return _clean(parts[3])
    return None


def instant_po_id_for(record: Dict[str, Any]) -> Optional[str]:
    """PO_ID of a normal instant payment (the ref_no used as reco_id), else None.

    Exactly the condition of the INSTANT_PREFIXES branch in ``compute_reco_id``:
    instant prefix WITH a ref_no. These PO ids seed ``resolve_bulk_groups``, which
    checks whether they in fact belong to a bulk MessageID. MOSEL/Webripost (Z6/Z9)
    also key on ref_no but are not payments of this circuit — excluded.
    """
    if _channel_id(record) in CHANNEL_REF_NO:
        return None
    parts = _tp_parts(record)
    if not parts or parts[0].strip().upper() not in INSTANT_PREFIXES:
        return None
    return _clean(_dm_ref_no(record))


def return_po_id_for(record: Dict[str, Any]) -> Optional[str]:
    """Return-PaymentNumber needing a std.[Return] lookup (NDRT reject-of-return
    or bulk ``PREFIX/RCC/…`` reject shape), else None. ``ref_no`` carries the
    RETURN's PaymentNumber (``##`` last-segment tolerated); TP segment[3] is
    the fallback for the /RCC shape, mirroring the NCC/NCP convention."""
    parts = _tp_parts(record)
    if not parts:
        return None
    prefix = parts[0].strip().upper()
    seg1 = parts[1].strip().upper() if len(parts) > 1 else ""
    if prefix != NDRT_PREFIX and not (prefix in BULK_PREFIXES and seg1 in BULK_REJECT_SEGS):
        return None
    ref = _clean(_dm_ref_no(record))
    if ref:
        return _clean(ref.split("##")[-1]) if "##" in ref else ref
    if prefix in BULK_PREFIXES and len(parts) >= 4:
        return _clean(parts[3])
    return None


def reversal_ref_for(record: Dict[str, Any]) -> Optional[str]:
    """TransactionRef needing a std.Payment lookup as a REVERSAL, else None.

    A reversal hides inside an existing flow (instant or bulk) but carries no
    ref_no / remarks_1, so the normal classification yields nothing. Its
    TransactionParticulars is ``PREFIX/<TransactionRef>/<NAME>`` — the lookup key
    is segment[1] (e.g. BKRTP/BKRTP000305424/POST TELECOM -> BKRTP000305424).

    Candidate = a TP-bearing movement that is NOT a normal instant payment
    (instant prefix WITH a ref_no), NOT a bulk direct, NOT a bulk-return shape.
    A bulk-return segment at parts[1] (NCC/NCP) is excluded *for any prefix* —
    including a BKRTP IP that wears the bulk-return format
    (BKRTP/NCC|NCP/I|O/PO_ID/...), which stays keyed by ref_no, never a reversal.
    The filter is deliberately loose otherwise (no length/charset rule): the    std.Payment.TransactionRef JOIN is the real discriminator. Kept symmetric
    with the branches in ``compute_reco_id`` that consult ``reversal_map``.
    """
    if _channel_id(record) in CHANNEL_REF_NO:            # MOSEL/Webripost: never a reversal
        return None
    parts = _tp_parts(record)
    if len(parts) <= 1:
        return None
    prefix = parts[0].strip().upper()
    seg1 = parts[1].strip().upper()
    if seg1 in BULK_RETURN_SEGS or seg1 in BULK_REJECT_SEGS:
        return None                                      # bulk-return/reject shape (any prefix, incl. BKRTP IP) — not a reversal
    if prefix == NDRT_PREFIX:
        return None                                      # reject-of-return — resolved via std.[Return]
    if prefix in BULK_PREFIXES and seg1 in BULK_DIRECT_SEGS:
        return None                                      # bulk direct — not a reversal
    if prefix in INSTANT_PREFIXES and _clean(_dm_ref_no(record)):
        return None                                      # normal instant payment (has ref_no)
    return _clean(parts[1])


def compute_reco_id(
    row: Dict[str, Any],
    payment_map: Dict[str, Optional[str]],
    reversal_map: Dict[str, Optional[str]],
    return_map: Optional[Dict[str, Optional[str]]] = None,
    bulk_key_map: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """Reconciliation key for one raw std.Movement row.

    Returns the key, the 'Not Supported' sentinel (known prefix, unhandled
    schema), or None (transient — e.g. bulk payment not yet in std.Payment).
    ``payment_map`` maps PO_ID -> MessageID/PACS008 (or None), prefilled by
    ``resolve_bulk_returns`` for all bulk-return PO_IDs of the run.
    ``reversal_map`` maps TransactionRef -> MessageID/PACS008, prefilled by
    ``resolve_reversals``; it is consulted only where the normal path yields
    nothing — a reversal carries neither ref_no nor remarks_1 (see
    ``reversal_ref_for``), so it acts as a gap-filler, never an override.
    ``bulk_key_map`` maps PO_ID -> MessageID for the PO ids that turned out to
    belong to a BULK booking (``resolve_bulk_groups`` + the map persisted by the
    previous runs). It is the one map that *overrides* a resolved key: without it
    the N members of a multiline bulk stay on N distinct PO ids and can never
    balance against the single bulk counterpart line.
    """
    # No '/'-structured TransactionParticulars (empty, or plain text such as
    # "Extourne de virement"): MOSEL/Webripost movements, keyed by
    # Initiating_channel (Z6/Z9) exclusive to this case → reco_id is the PO number in ref_no.
    if _channel_id(row) in CHANNEL_REF_NO:
        return _clean(_dm_ref_no(row))

    parts = _tp_parts(row)
    if not parts or len(parts) <= 1:
        return UNRESOLVED_RECO_ID
    prefix = parts[0].strip().upper()
    return_map = return_map or {}

    if prefix == NDRT_PREFIX:                             # reject-of-return -> std.[Return] -> original payment
        po = return_po_id_for(row)
        return return_map.get(po) if po else None

    if prefix in INSTANT_PREFIXES:                        # SWIFT/SCRT1/BKRTP -> PaymentOrderID_Ref
        ref = _clean(_dm_ref_no(row))
        if ref:
            # Bulk-booked instant payment (multiline & co) → the bulk's MessageID
            # groups all its members; a plain 1-1 instant keeps its own PO id.
            return (bulk_key_map or {}).get(ref, ref)
        # BKRTP can wear the bulk-return shape (BKRTP/NCC|NCP/I|O/PO_ID/...): it is
        # still an IP keyed by ref_no — parts[1] is a return segment, NOT a reversal
        # TransactionRef, so never attempt a reversal lookup for it.
        if parts[1].strip().upper() in BULK_RETURN_SEGS:
            return ref
        # Instant prefix, no ref_no, not the return shape → a reversal hiding in the
        # IP flow, resolved by the reversal_map fallthrough below.

    if prefix in BULK_PREFIXES:
        seg1 = parts[1].strip().upper() if len(parts) > 1 else ""
        if seg1 in BULK_DIRECT_SEGS:                      # SCTXB/I/BLK... -> Remarks_1
            return _clean(_dm_remarks_1(row))
        if seg1 in BULK_REJECT_SEGS:                      # SCTXB/RCC/... -> std.[Return] -> original payment
            po = return_po_id_for(row)
            return return_map.get(po) if po else None
        if seg1 in BULK_RETURN_SEGS and len(parts) >= 4:  # SCTXB/NCP/I/PO_ID/... -> std.Payment
            return payment_map.get(_clean(parts[3]) or "")
        # Bulk prefix, neither direct nor return → a reversal hiding in the bulk
        # flow (miss stays "Not Supported", exactly as before).
        return reversal_map.get(_clean(parts[1]) or "") or UNRESOLVED_RECO_ID

    # GL transfers, unknown patterns → still try a reversal lookup before giving up.
    return reversal_map.get(_clean(parts[1]) or "") or UNRESOLVED_RECO_ID


def resolve_bulk_returns(lookup_conn, po_ids) -> Dict[str, Optional[str]]:
    """Resolve all bulk-return PO_IDs in ONE pass: load them into a temp table and
    INNER JOIN std.Payment once (single scan), instead of hundreds of `IN` queries.

    Returns {PaymentNumber: first non-empty of (MessageID, MessageIDPACS008)}.
    PO_IDs with no current payment are simply absent → reco stays None (transient).
    """
    result: Dict[str, Optional[str]] = {}
    po_list = [p for p in {_clean(p) for p in po_ids} if p]
    if not po_list:
        logger.info("[resolve_bulk_returns] no bulk-return PO_IDs to resolve")
        return result

    cursor = lookup_conn.cursor()
    try:
        cursor.execute("IF OBJECT_ID('tempdb..#reco_po') IS NOT NULL DROP TABLE #reco_po")
        cursor.execute("CREATE TABLE #reco_po (po VARCHAR(64) PRIMARY KEY)")
        cursor.fast_executemany = True
        for i in range(0, len(po_list), PO_INSERT_BATCH):
            cursor.executemany(
                "INSERT INTO #reco_po (po) VALUES (?)",
                [(p,) for p in po_list[i:i + PO_INSERT_BATCH]],
            )
        cursor.execute(
            """
            SELECT t.po,
                   COALESCE(NULLIF(LTRIM(RTRIM(p.MessageIDPACS008)), ''),
                            NULLIF(LTRIM(RTRIM(p.MessageID)), '')) AS reco,
                    p.*
            FROM #reco_po t
            INNER JOIN std.Payment p ON p.PaymentNumber = t.po
            WHERE p.IsCurrent = 1
            """
        )
        for po_number, reco, *rest in cursor.fetchall():
            #logger.info("[resolve_bulk_returns] po=%s, reco=%s, rest=%s", po_number, reco, rest)
            po = _clean(po_number)
            if po is not None:
                result[po] = _clean(reco)
        cursor.execute("IF OBJECT_ID('tempdb..#reco_po') IS NOT NULL DROP TABLE #reco_po")
        logger.info("[ingest_finacle] resolved %d/%d bulk-return PO_IDs via std.Payment join",
                    len(result), len(po_list))
    finally:
        cursor.close()
    return result


def resolve_return_recos(lookup_conn, return_po_ids) -> Dict[str, Optional[str]]:
    """Resolve NDRT / PREFIX/RCC return-PaymentNumbers in ONE pass:
    std.[Return] (reserved word — bracketed, IsCurrent=1) gives OriginalPo,
    the ORIGINAL payment gives the reco. Returns
    {return PaymentNumber: first non-empty of (MessageIDPACS008, MessageID)}
    — the mirror of ``resolve_bulk_returns`` with the extra indirection.
    Missing Return/Payment rows are simply absent → reco stays None (transient).
    """
    result: Dict[str, Optional[str]] = {}
    po_list = [p for p in {_clean(p) for p in return_po_ids} if p]
    if not po_list:
        logger.info("[resolve_return_recos] no return PO_IDs to resolve")
        return result

    cursor = lookup_conn.cursor()
    try:
        cursor.execute("IF OBJECT_ID('tempdb..#reco_retpo') IS NOT NULL DROP TABLE #reco_retpo")
        cursor.execute("CREATE TABLE #reco_retpo (po VARCHAR(64) PRIMARY KEY)")
        cursor.fast_executemany = True
        for i in range(0, len(po_list), PO_INSERT_BATCH):
            cursor.executemany(
                "INSERT INTO #reco_retpo (po) VALUES (?)",
                [(p,) for p in po_list[i:i + PO_INSERT_BATCH]],
            )
        cursor.execute(
            """
            SELECT t.po,
                   COALESCE(NULLIF(LTRIM(RTRIM(p.MessageIDPACS008)), ''),
                            NULLIF(LTRIM(RTRIM(p.MessageID)), '')) AS reco
            FROM #reco_retpo t
            INNER JOIN std.[Return] r ON r.PaymentNumber = t.po
            INNER JOIN std.Payment p ON p.PaymentNumber = r.OriginalPo
            WHERE r.IsCurrent = 1 AND p.IsCurrent = 1
            """
        )
        for po_number, reco in cursor.fetchall():
            po = _clean(po_number)
            if po is not None:
                result[po] = _clean(reco)
        cursor.execute("IF OBJECT_ID('tempdb..#reco_retpo') IS NOT NULL DROP TABLE #reco_retpo")
        logger.info("[ingest_finacle] resolved %d/%d return PO_IDs via std.[Return] join",
                    len(result), len(po_list))
    finally:
        cursor.close()
    return result


def resolve_reversals(lookup_conn, refs) -> Tuple[Dict[str, Optional[str]], set]:
    """Resolve all reversal TransactionRefs in ONE temp-table JOIN against
    std.Payment (single scan) — the mirror of ``resolve_bulk_returns`` but keyed
    on ``std.Payment.TransactionRef`` instead of ``PaymentNumber``.

    Returns ``({TransactionRef: first non-empty of (MessageID, MessageIDPACS008)},
    {MessageID of the payments whose InitModule is multiline})``. The second set
    seeds ``resolve_bulk_groups``: a reversal is how a bulk-booked instant payment
    usually surfaces, and its MessageID names the whole bulk.
    Refs with no current payment are simply absent → not a reversal (or
    transient) → caller keeps the movement unresolved for retry.
    """
    result: Dict[str, Optional[str]] = {}
    multiline_msg_ids: set = set()
    cleaned = {_clean(r) for r in refs}
    too_long = {r for r in cleaned if r and len(r) > _MAX_REF_LEN}
    if too_long:
        logger.warning("[resolve_reversals] skipping %d ref(s) longer than %d chars (temp table limit)",
                       len(too_long), _MAX_REF_LEN)
    ref_list = [r for r in cleaned - too_long if r]
    if not ref_list:
        logger.info("[resolve_reversals] no reversal refs to resolve")
        return result, multiline_msg_ids

    cursor = lookup_conn.cursor()
    try:
        cursor.execute("IF OBJECT_ID('tempdb..#reco_ref') IS NOT NULL DROP TABLE #reco_ref")
        cursor.execute("CREATE TABLE #reco_ref (ref VARCHAR(64) PRIMARY KEY)")
        cursor.fast_executemany = True
        for i in range(0, len(ref_list), PO_INSERT_BATCH):
            cursor.executemany(
                "INSERT INTO #reco_ref (ref) VALUES (?)",
                [(r,) for r in ref_list[i:i + PO_INSERT_BATCH]],
            )
        cursor.execute(
            f"""
            SELECT t.ref,
                   COALESCE(NULLIF(LTRIM(RTRIM(p.MessageID)), ''),
                            NULLIF(LTRIM(RTRIM(p.MessageIDPACS008)), '')) AS reco,
                   NULLIF(LTRIM(RTRIM(p.MessageID)), '') AS msg_id,
                   p.{PAYMENT_INIT_MODULE_COLUMN} AS init_module
            FROM #reco_ref t
            INNER JOIN std.Payment p ON p.TransactionRef = t.ref
            WHERE p.IsCurrent = 1
            """
        )
        for ref, reco, msg_id, init_module in cursor.fetchall():
            r = _clean(ref)
            if r is not None:
                result[r] = _clean(reco)
            module = (_clean(init_module) or "").upper()
            msg = _clean(msg_id)
            if msg and module in MULTILINE_INIT_MODULES:
                multiline_msg_ids.add(msg)
        cursor.execute("IF OBJECT_ID('tempdb..#reco_ref') IS NOT NULL DROP TABLE #reco_ref")
        logger.info("[ingest_finacle] resolved %d/%d reversal refs via std.Payment join "
                    "(%d multiline MessageID(s))",
                    len(result), len(ref_list), len(multiline_msg_ids))
    finally:
        cursor.close()
    return result, multiline_msg_ids


def bulk_groups_from_rows(rows) -> Tuple[Dict[str, str], Dict[str, str]]:
    """``[(MessageID, PaymentNumber, InitModule)]`` ->
    ``({po_id: reco_id}, {po_id: InitModule})``.

    A MessageID names a BULK booking when it carries >= 2 distinct PaymentNumbers,
    or when one of its payments has a known multiline InitModule (a bulk of one is
    indistinguishable from a normal 1-1 otherwise). Plain 1-1 instant payments —
    the overwhelming majority — produce no entry at all: they keep their own PO id
    as reco_id and this map stays small. The second dict is traceability only
    (which module booked the payment), stored alongside the key.
    """
    by_msg: Dict[str, set] = {}
    modules: Dict[str, str] = {}
    multiline: set = set()
    for msg_id, po_id, init_module in rows:
        msg, po = _clean(msg_id), _clean(po_id)
        if not msg or not po:
            continue
        by_msg.setdefault(msg, set()).add(po)
        module = _clean(init_module)
        if module:
            modules[po] = module.upper()
            if module.upper() in MULTILINE_INIT_MODULES:
                multiline.add(msg)
    mapping = {
        po: msg
        for msg, pos in by_msg.items()
        if len(pos) >= 2 or msg in multiline
        for po in pos
    }
    return mapping, {po: m for po, m in modules.items() if po in mapping}


def resolve_bulk_groups(lookup_conn, po_ids, msg_ids=None) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Map the PO ids that belong to a BULK booking to their group key (MessageID).

    ONE query does both halves: the instant PO ids of the run seed the set of
    MessageIDs, then std.Payment is joined back on those MessageIDs to get the
    FULL membership of each group. The fan-out is what makes the detection
    correct — the other members of a bulk have not necessarily moved inside the
    run's window, so cardinality cannot be read off the run's PO ids alone.
    ``msg_ids`` seeds the same set directly (multiline reversals, whose payment
    is already known to belong to a bulk).
    """
    result: Dict[str, str] = {}
    modules: Dict[str, str] = {}
    po_list = [p for p in {_clean(p) for p in po_ids} if p and len(p) <= _MAX_REF_LEN]
    msg_list = [m for m in {_clean(m) for m in (msg_ids or ())} if m]
    if not po_list and not msg_list:
        logger.info("[resolve_bulk_groups] no instant PO_ID / MessageID to scan")
        return result, modules

    cursor = lookup_conn.cursor()
    try:
        for tmp, col, size, values in (
            ("#reco_bpo", "po", 64, po_list),
            ("#reco_bmsg", "msg", 128, msg_list),
        ):
            cursor.execute(f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp}")
            cursor.execute(f"CREATE TABLE {tmp} ({col} VARCHAR({size}) PRIMARY KEY)")
            cursor.fast_executemany = True
            for i in range(0, len(values), PO_INSERT_BATCH):
                cursor.executemany(
                    f"INSERT INTO {tmp} ({col}) VALUES (?)",
                    [(v,) for v in values[i:i + PO_INSERT_BATCH]],
                )
        cursor.execute(
            f"""
            SELECT p2.MessageID, p2.PaymentNumber, p2.{PAYMENT_INIT_MODULE_COLUMN}
            FROM std.Payment p2
            INNER JOIN (
                SELECT DISTINCT msgid FROM (
                    SELECT NULLIF(LTRIM(RTRIM(p.MessageID)), '') AS msgid
                    FROM #reco_bpo t
                    INNER JOIN std.Payment p ON p.PaymentNumber = t.po
                    WHERE p.IsCurrent = 1
                    UNION
                    SELECT NULLIF(LTRIM(RTRIM(m.msg)), '') FROM #reco_bmsg m
                ) u WHERE msgid IS NOT NULL
            ) g ON p2.MessageID = g.msgid
            WHERE p2.IsCurrent = 1
            """
        )
        result, modules = bulk_groups_from_rows(cursor.fetchall())
        for tmp in ("#reco_bpo", "#reco_bmsg"):
            cursor.execute(f"IF OBJECT_ID('tempdb..{tmp}') IS NOT NULL DROP TABLE {tmp}")
        logger.info(
            "[ingest_finacle] bulk scan: %d PO_ID(s) + %d seed MessageID(s) -> "
            "%d PO_ID(s) regrouped into %d bulk(s)",
            len(po_list), len(msg_list), len(result), len(set(result.values())),
        )
    finally:
        cursor.close()
    return result, modules


def movement_row_to_entry(row: Dict[str, Any], reco_id: Optional[str]) -> Dict[str, Any]:
    """Map one std.Movement row + precomputed reco_id to a JSON-safe entry dict.

    Amounts are signed from PartTransactionType (D -> negative / debit,
    C -> positive / credit) so auto-matching (sum=0 per reco_id) works.
    Raises ValueError for rows that cannot be mapped (reported as row errors).
    reco_id may be None / 'Not Supported' (kept for retry).
    """
    direction_code = str(row.get("PartTransactionType") or "").strip().upper()
    if direction_code not in ("D", "C"):
        raise ValueError(f"unexpected PartTransactionType: {row.get('PartTransactionType')!r}")

    raw_amount = row.get("TransactionAmount")
    if raw_amount is None:
        raise ValueError("TransactionAmount is NULL")
    amount = Decimal(str(raw_amount))
    if direction_code == "D":
        amount = -abs(amount)
        direction = "debit"
    else:
        amount = abs(amount)
        direction = "credit"

    value_date = row.get("ValueDate") or row.get("TransactionDate")
    if value_date is None:
        raise ValueError("both ValueDate and TransactionDate are NULL")

    # Disambiguate multi-leg transactions: two legs of the same TransactionID
    # would otherwise share an identity. external_ref is the finacle source_hash key.
    external_ref = _clean(row.get("TransactionID"))
    serial = row.get("Part_Tran_Srl_No")
    try:
        if external_ref and serial is not None and int(serial) > 1:
            external_ref = f"{external_ref}#{int(serial)}"
    except (TypeError, ValueError):
        pass

    if not external_ref:
        # Some movements (e.g. internal GL entries) carry no TransactionID; fall back
        # to the datamart row PK so distinct movements don't collapse onto the same
        # finacle source_hash (which the per-batch upsert would otherwise drop).
        mid = _clean(row.get("MovementID"))
        external_ref = f"MID:{mid}" if mid else None

    operation_date = row.get("TransactionDate")

    return {
        "reco_id": reco_id,
        "account": str(row.get("AccountNumber") or "") or None,
        "currency": str(row.get("TxnCurrency") or "EUR"),
        "amount": str(amount),
        "value_date": _to_iso_datetime(value_date),
        "operation_date": _to_iso_datetime(operation_date) if operation_date is not None else None,
        "direction": direction,
        "event_type": str(row.get("TransactionType") or "") or None,
        "external_ref": external_ref,
        "transaction_particulars": _clean(row.get("TransactionParticulars")),
        "ref_no": _clean(_dm_ref_no(row)),
        "remarks_1": _clean(_dm_remarks_1(row)),
        "payload_raw": _json_safe(row),
    }


# ---------------------------------------------------------------------------
# Orchestration (shared by ingest_finacle and orchestrate_ingestion DAGs)
# ---------------------------------------------------------------------------

def _compute_since(last_success_at: Optional[str], backfill_since: Optional[str] = None) -> datetime:
    """Watermark with overlap margin; source_hash dedup absorbs re-reads.

    No watermark (first-ever run) → backfill floor. The floor is taken from the
    backend (``backfill_since``, set in the app .env and served via the sources
    API) first, then the DAG's own ``RECO_FINACLE_BACKFILL_SINCE`` env, then a
    1900 catch-all. Returned naive (UTC) because pyodbc/MSSQL rejects tz-aware params.
    """
    if last_success_at:
        watermark = datetime.fromisoformat(last_success_at)
        if watermark.tzinfo is not None:
            watermark = watermark.astimezone(timezone.utc).replace(tzinfo=None)
        #logger.info("[ingest_finacle] last_success_at=%s -> watermark=%s", last_success_at, watermark.isoformat())
        return watermark - timedelta(days=OVERLAP_DAYS)
    floor = (backfill_since or "").strip() or BACKFILL_SINCE
    if floor:
        dt = datetime.fromisoformat(floor)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        #logger.info("[ingest_finacle] no last_success_at, using backfill_since=%s -> watermark=%s", floor, dt.isoformat())
        return dt
    return datetime(1900, 1, 1)


def _ingest_source(conn, lookup_conn, source: Dict[str, Any], dag_run_id: Optional[str]) -> int:
    """One finacle source, in two streaming passes over std.Movement.

    Pass 1 collects the bulk-return PO_IDs (from fresh movements AND the app's
    still-unresolved entries); one temp-table JOIN resolves them all against
    std.Payment; pass 2 re-streams the movements, assigns each its reco_id and
    pushes; then the unresolved entries are re-enriched and re-pushed (the backend
    upsert updates reco_id in place). Two passes keep memory bounded at 100k–200k
    rows/run (we never hold the whole movement list). ``conn`` streams std.Movement;
    ``lookup_conn`` runs the temp-table join (a single MSSQL connection can't have a
    second active command mid-stream).
    """
    flow_code, source_code = source["flow_code"], source["source_code"]
    accounts = source.get("accounts") or []
    since = _compute_since(source.get("last_success_at"), source.get("backfill_since"))
    logger.info(
        "[ingest_finacle] %s/%s: extracting %d account(s) since %s",
        flow_code, source_code, len(accounts), since.isoformat(),
    )
    run_id = finacle_start_run(flow_code, source_code, dag_run_id)
    total = 0
    try:
        # Entries the app still couldn't resolve (reco_id NULL / "Not Supported").
        unresolved = finacle_list_unresolved(flow_code, source_code).get("entries", [])

        # Pass 1 — collect distinct bulk-return PO_IDs, reversal refs AND instant
        # PO_IDs (fresh movements + retries) in a single sweep.
        po_ids: set = set()
        reversal_refs: set = set()
        return_po_ids: set = set()
        instant_po_ids: set = set()
        for chunk in iter_movement_chunks(conn, accounts=accounts, since=since):
            for row in chunk:
                po = payment_po_id_for(row)
                if po:
                    po_ids.add(po)
                ref = reversal_ref_for(row)
                if ref:
                    reversal_refs.add(ref)
                ret = return_po_id_for(row)
                if ret:
                    return_po_ids.add(ret)
                ipo = instant_po_id_for(row)
                if ipo:
                    instant_po_ids.add(ipo)
        for entry in unresolved:
            po = payment_po_id_for(entry)
            if po:
                po_ids.add(po)
            ref = reversal_ref_for(entry)
            if ref:
                reversal_refs.add(ref)
            ret = return_po_id_for(entry)
            if ret:
                return_po_ids.add(ret)
            ipo = instant_po_id_for(entry)
            if ipo:
                instant_po_ids.add(ipo)

        # One JOIN each resolves every bulk-return PO_ID / reversal ref /
        # NDRT-RCC return PO at once.
        payment_map = resolve_bulk_returns(lookup_conn, po_ids)
        reversal_map, multiline_msg_ids = resolve_reversals(lookup_conn, reversal_refs)
        return_map = resolve_return_recos(lookup_conn, return_po_ids)

        # Bulk-booked instant payments: the map persisted by previous runs (so late
        # movements of a known bulk are keyed right away) + this run's scan.
        bulk_key_map: Dict[str, str] = {}
        fresh_bulk_keys: Dict[str, str] = {}
        fresh_init_modules: Dict[str, str] = {}
        if BULK_REGROUP_MODE in ("on", "log"):
            fresh_bulk_keys, fresh_init_modules = resolve_bulk_groups(
                lookup_conn, instant_po_ids, multiline_msg_ids
            )
            if BULK_REGROUP_MODE == "on":  # 'log' is a dry-run: scan + log, key nothing
                bulk_key_map = dict(finacle_get_bulk_keys(flow_code, source_code))
                bulk_key_map.update(fresh_bulk_keys)

        # Pass 2 — re-stream fresh movements, compute reco_id, push per chunk.
        for chunk in iter_movement_chunks(conn, accounts=accounts, since=since):
            entries, errors = [], []
            for row in chunk:
                try:
                    reco_id = compute_reco_id(
                        row, payment_map, reversal_map, return_map, bulk_key_map
                    )
                    entries.append(movement_row_to_entry(row, reco_id))
                except ValueError as exc:
                    errors.append(f"TransactionID={row.get('TransactionID')}: {exc}")
            finacle_post_batch(run_id, entries, errors)
            total += len(entries)

        # Retry — re-enrich the app's unresolved entries and re-push (upsert in place).
        for i in range(0, len(unresolved), CHUNK_SIZE):
            batch = []
            for entry in unresolved[i:i + CHUNK_SIZE]:
                out = dict(entry)
                out["reco_id"] = compute_reco_id(
                    entry, payment_map, reversal_map, return_map, bulk_key_map
                )
                batch.append(out)
            finacle_post_batch(run_id, batch, [])
            total += len(batch)

        # Every movement of the run is now in the app: persist this run's bulk keys
        # and re-key in place the entries already stored under a member PO id —
        # BOTH sides of the flow (the MT940 leg resolves to the same PaymentNumber,
        # cf. mt940_extract.resolve_mt940_reco_ids), else the group can't balance.
        if fresh_bulk_keys:
            if BULK_REGROUP_MODE == "on":
                data = finacle_post_bulk_keys(
                    flow_code, source_code, fresh_bulk_keys, fresh_init_modules
                ).get("data") or {}
                logger.info("[ingest_finacle] %s/%s: bulk regroup -> %s keys upserted, "
                            "%s entries re-keyed",
                            flow_code, source_code,
                            data.get("keys_upserted"), data.get("entries_updated"))
            else:
                logger.info("[ingest_finacle] %s/%s: bulk regroup DRY-RUN (mode=%s) — "
                            "%d PO_ID(s) in %d bulk(s) would be re-keyed, sample=%s",
                            flow_code, source_code, BULK_REGROUP_MODE,
                            len(fresh_bulk_keys), len(set(fresh_bulk_keys.values())),
                            dict(list(fresh_bulk_keys.items())[:5]))

        finacle_complete_run(run_id)
        logger.info("[ingest_finacle] %s/%s: run #%s done (%d pushed incl. %d unresolved retried)",
                    flow_code, source_code, run_id, total, len(unresolved))
    except Exception:
        try:
            finacle_complete_run(run_id, failed=True, error="see Airflow logs")
        except Exception:  # noqa: BLE001
            logger.warning("[ingest_finacle] could not mark run #%s as failed", run_id)
        raise
    return total


def run_finacle_ingestion(dag_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Extract + enrich movements for every active finacle source, push to backend."""
    sources = task_list_finacle_sources().get("sources", [])
    summary: Dict[str, Any] = {"ingested": [], "skipped": [], "errors": []}
    if not sources:
        logger.info("[ingest_finacle] No active finacle source — nothing to do.")
        return summary

    # Local import: keeps this module importable (tests, DAG parsing) without
    # the MSSQL provider installed.
    from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook

    hook = MsSqlHook(mssql_conn_id=DATAMART_CONN_ID)
    # Two connections: one streams std.Movement, the other runs std.Payment lookups
    # (a single MSSQL connection can't have a second active command mid-stream).
    conn = get_pyodbc_conn(hook)
    lookup_conn = get_pyodbc_conn(hook)
    try:
        for source in sources:
            label = f"{source['flow_code']}/{source['source_code']}"
            # Batch-booking sources are handled by reco_datamart_bb (lot
            # clustering); None-tolerant so an older backend without the
            # parser_type field keeps the legacy behavior.
            if source.get("parser_type") not in (None, "finacle_db"):
                logger.info(
                    "[ingest_finacle] %s uses parser_type=%s — left to its dedicated DAG.",
                    label, source.get("parser_type"),
                )
                summary["skipped"].append(label)
                continue
            if not (source.get("accounts") or []):
                logger.warning("[ingest_finacle] %s has no reference account — skipped.", label)
                summary["skipped"].append(label)
                continue
            try:
                _ingest_source(conn, lookup_conn, source, dag_run_id)
                summary["ingested"].append(label)
            except Exception as exc:  # noqa: BLE001
                logger.error("[ingest_finacle] %s failed: %s", label, exc, exc_info=True)
                summary["errors"].append({"source": label, "error": str(exc)})
    finally:
        for c in (conn, lookup_conn):
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    if summary["errors"]:
        raise RuntimeError(f"finacle ingestion finished with errors: {summary['errors']}")
    return summary
