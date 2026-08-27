"""WERO ingestion — a PAYMENT reconciliation, not an accounting one.

Every other datamart flow reconciles ``std.Movement`` (a booked accounting leg)
against a counterpart file. WERO reconciles three payment repositories against
each other, and never reads ``std.Movement`` at all:

    the datamart WERO table  <->  std.Payment  <->  std.[Return]

joined on the **end-to-end reference** (WERO's ``OriginatorReference``, Finacle's
end-to-end column), exactly as the standalone prototype did
(``wero_reconciliation_process.py``: ``F_E2E = W_ORG_REF``,
``initgmodule IN ('WERO','WEROEMC')``, UUIDs stored with or without dashes).

How it reuses the existing engine
---------------------------------
The app's matching engine knows one rule: group live PENDING entries by
``(flow_id, reco_id, currency)`` and match the groups whose ``SUM(amount) = 0``.
So instead of computing the reconciliation here (the prototype's pandas outer
merge), we push **one entry per leg**, all legs of a payment sharing the same
``reco_id``, with:

    WERO side  ->  credit  (+X)
    Finacle side -> debit  (-X)

and Postgres does the join. Consequences, directly readable in the existing
views and dashboards:

    pending credit          -> a WERO row with no Finacle counterpart
    pending debit           -> a WERO payment in Finacle with no WERO row
    2 pending, sum != 0     -> amount discrepancy
    group matched           -> émargé automatically

The business direction (``TransactionDirection``) is not lost — it goes to
``remarks_1`` and ``payload_raw``. The sign here is a matching device, not an
accounting statement.

A return and its WERO reversal form their **own** group, under ``<e2e>#RET``, so
a return that is not mirrored does not drag the already-reconciled original pair
back to pending.

Nothing is joined in this module: three independent streaming queries, no temp
table, no pandas, memory bounded by the chunk.

Configuration
-------------
Two datamart identifiers are still unconfirmed (the WERO table name, and
std.Payment's end-to-end column), so **every** identifier this module
interpolates comes from the source's ``parser_config`` (JSONB, editable from the
UI) with the defaults below. Each one is validated as a bare SQL identifier at
load time, like ``PAYMENT_AMOUNT_COL`` in reco_datamart.

SCD2
----
The three tables are historised (``IsCurrent`` / ``StartDate`` / ``EndDate``).
``IsCurrent = 1`` does not guarantee uniqueness, but no dedup is needed here:
two current rows of the same payment produce the same ``external_ref``, hence
the same ``source_hash``, and the backend upsert keeps one.
"""
import logging
import os
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterator, List, Optional, Tuple

from reco_common import (
    finacle_complete_run,
    finacle_post_batch,
    finacle_post_payment_status_batch,
    finacle_start_run,
    task_list_finacle_sources,
)
from reco_datamart import (
    CHUNK_SIZE,
    DATAMART_CONN_ID,
    PAYMENT_AMOUNT_COL,
    PAYMENT_INIT_MODULE_COLUMN,
    _clean,
    _compute_since,
    _json_safe,
    get_pyodbc_conn,
)

logger = logging.getLogger(__name__)

# The parser_type this DAG self-selects on. reco_datamart skips anything that is
# not "finacle_db" and reco_datamart_bb keeps only its own value, so a WERO
# source is never picked up twice.
PARSER_TYPE_WERO = "wero"

# Suffix isolating the return pair (Finacle return + WERO reversal) in its own
# reconciliation group.
RET_SUFFIX = "#RET"

# reco.reconciliation_entry.reco_id is VARCHAR(128); leave room for the suffix.
_MAX_RECO_ID = 128
_MAX_RECO_BASE = _MAX_RECO_ID - len(RET_SUFFIX)

PS_PUSH_BATCH = int(os.environ.get("RECO_WERO_PS_PUSH_BATCH", "2000"))

DEFAULT_CONFIG: Dict[str, Any] = {
    # -- WERO table -----------------------------------------------------------
    "wero_table": "std.Wero",
    "wero_ref_column": "OriginatorReference",
    "wero_id_column": "CaptureIDMoneyTransferID",
    # Business timestamp (varchar in the datamart) used as the entry's date.
    "wero_date_column": "SettlementRelatedTimestamp",
    # Incremental filter. Deliberately NOT wero_date_column: that one is a
    # varchar, and comparing it to a datetime parameter forces an implicit
    # conversion that both kills the index and blows up on a malformed value.
    # StartDate is a real DATE (SCD2 row-version start).
    "wero_watermark_column": "StartDate",
    "wero_amount_column": "TransactionAmount",
    "wero_currency_column": "Currency",
    "wero_direction_column": "TransactionDirection",
    "wero_status_column": "SettlementStatus",
    # SettlementStatus values marking a reversal. Confirmed 2026-08-26: the
    # column only ever holds Accepted / Failed / Rejected / Settled — there is
    # NO reversal status. So this stays empty, no WERO reversal leg is emitted,
    # and a Finacle return stays visibly unmatched under #RET: a truthful
    # signal rather than a pairing we guessed at. Query C of
    # docs/wero-datamart-diagnostics.sql is what settles this.
    "wero_reversal_statuses": [],
    # Whitelist of SettlementStatus values worth reconciling. Empty (the
    # default) = every status, i.e. no filtering at all.
    # Why it exists: 67% of the acceptance rows are Failed/Rejected, and a WERO
    # transaction that failed most likely never became a Finacle payment — each
    # one would then sit in pending credit forever. But acceptance tolerates a
    # high failure rate by nature, so nothing is filtered until production has
    # been measured: run query A of docs/wero-datamart-diagnostics.sql, which
    # gives the match rate PER status, then set this from the UI.
    "wero_settlement_statuses": [],
    # -- std.Payment / std.[Return] -------------------------------------------
    # Confirmed 2026-08-26 on production: std.Payment.EndToEndId (varchar)
    # exists — the datamart's equivalent of the ODS endtoendidentification.
    "payment_e2e_column": "EndToEndId",
    "payment_init_modules": ["WERO", "WEROEMC"],
    "payment_date_column": "CreatedOn",
    # None -> the entry falls back to default_currency.
    # Left None on purpose: std.Payment carries TWO currency families
    # (SettlementCurrency / SettlementCcy) and we do not know yet which one is
    # populated. Currency is part of the match group key, so a wrong value
    # would silently break every match — query D settles it.
    "payment_currency_column": None,
    # std.[Return].CreatedOn / SettlementDate exist (confirmed 2026-08-26) but
    # are not known to be POPULATED, and an empty date turns every return into
    # a row error. Until query D says otherwise a return is dated and filtered
    # by its ORIGINAL payment — which also means a return booked long after its
    # payment falls outside the watermark window. Setting this fixes that.
    "return_date_column": None,
    # Same reasoning: std.[Return].ReturnSettlementAmount exists, but an empty
    # amount raises in parse_amount, so the amount keeps coming from the
    # original payment (what reco_datamart_bb.resolve_return_payments does).
    "return_amount_column": None,
    # Confirmed 2026-08-26 AND risk-free: Status only feeds
    # transaction_particulars (display), never the match key. A return now
    # shows its own status instead of its original payment's.
    "return_status_column": "Status",
    "returns_enabled": True,
    "default_currency": "EUR",
}

# Bare identifier, optionally schema-qualified. Interpolated into SQL, never
# bound — same guard as RECO_FINACLE_PAYMENT_AMOUNT_COL (reco_datamart.py).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

_IDENT_KEYS = (
    "wero_table",
    "wero_ref_column",
    "wero_id_column",
    "wero_date_column",
    "wero_watermark_column",
    "wero_amount_column",
    "wero_currency_column",
    "wero_direction_column",
    "wero_status_column",
    "payment_e2e_column",
    "payment_date_column",
    "payment_currency_column",
    "return_date_column",
    "return_amount_column",
    "return_status_column",
)

_UUID_HEX = set("0123456789ABCDEF")


class WeroConfigError(ValueError):
    """A parser_config value that cannot be safely interpolated into SQL."""


def load_config(parser_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Merge the source's parser_config over the defaults and validate it.

    Every identifier is checked here rather than at the call site: these values
    reach SQL by interpolation (a column name cannot be a bind parameter), so
    this function is the only thing standing between the UI's JSON field and the
    datamart. List values (init modules, reversal statuses) are bound, not
    interpolated, and so are only normalised.
    """
    cfg = dict(DEFAULT_CONFIG)
    cfg.update({k: v for k, v in (parser_config or {}).items() if k in DEFAULT_CONFIG})

    for key in _IDENT_KEYS:
        value = cfg.get(key)
        if value is None or value == "":
            cfg[key] = None
            continue
        value = str(value).strip()
        if not _IDENT_RE.match(value):
            raise WeroConfigError(
                f"parser_config[{key!r}] must be a bare SQL identifier "
                f"(optionally schema-qualified), got {value!r}"
            )
        cfg[key] = value

    for key in ("wero_table", "wero_ref_column", "wero_watermark_column",
                "wero_amount_column", "payment_e2e_column", "payment_date_column"):
        if not cfg.get(key):
            raise WeroConfigError(f"parser_config[{key!r}] is required")

    modules = [m for m in (_clean(m) for m in cfg.get("payment_init_modules") or []) if m]
    if not modules:
        raise WeroConfigError("parser_config['payment_init_modules'] cannot be empty")
    cfg["payment_init_modules"] = modules

    for key in ("wero_reversal_statuses", "wero_settlement_statuses"):
        cfg[key] = {s.upper() for s in (_clean(s) for s in cfg.get(key) or []) if s}
    cfg["returns_enabled"] = bool(cfg.get("returns_enabled", True))
    cfg["default_currency"] = (_clean(cfg.get("default_currency")) or "EUR")[:8]
    return cfg


# ---------------------------------------------------------------------------
# Normalisation — one function, called on all three sides
# ---------------------------------------------------------------------------

def normalize_ref(value: Any) -> Optional[str]:
    """Canonical form of an end-to-end reference.

    Two normalisations, both load-bearing:

    * ``upper()`` — MSSQL's collation is CI_AS (case-insensitive), Python is
      not. Comparing raw is exactly the bug that split 'RUMELANGE' from
      'Rumelange' into two lots that could never match (reco_datamart_bb).
    * dashes dropped **only** when the result is a 32-hex-digit UUID — WERO
      stores the transfer id with dashes, Finacle sometimes without. Stripping
      dashes unconditionally would fold unrelated structured references.
    """
    ref = _clean(value)
    if ref is None:
        return None
    ref = ref.upper()
    stripped = ref.replace("-", "")
    if len(stripped) == 32 and all(c in _UUID_HEX for c in stripped):
        return stripped
    return ref[:_MAX_RECO_BASE]


def return_reco_id(base: Optional[str]) -> Optional[str]:
    """The reco_id of the return pair for a payment's normalised reference."""
    if not base:
        return None
    return f"{base[:_MAX_RECO_BASE]}{RET_SUFFIX}"


def parse_amount(value: Any) -> Decimal:
    """Strict varchar -> Decimal (WERO stores TransactionAmount as varchar).

    Strict on purpose, unlike reco_datamart_bb._safe_decimal which defaults to
    zero: a leg silently worth 0 balances on its own under HAVING SUM = 0 and
    would be auto-matched on thin air. An unparsable or zero amount is a row
    error instead — the run ends PARTIAL and the row is reported.
    """
    raw = _clean(value)
    if raw is None:
        raise ValueError("amount is empty")
    text = raw.replace(" ", "").replace(" ", "")
    if "," in text and "." not in text:  # 125,00 -> 125.00
        text = text.replace(",", ".")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"unparsable amount: {raw!r}")
    if amount == 0:
        raise ValueError("amount is zero (would match on thin air)")
    return amount


_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
)


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Best-effort varchar/date -> naive datetime. None when unusable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None) if value.tzinfo else value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    raw = _clean(value)
    if raw is None:
        return None
    text = raw.replace("Z", "").replace("z", "")
    if "T" in text:
        text = text.replace("T", " ")
    if "+" in text[10:]:  # drop an offset; the whole chain keeps wall clock
        text = text[: 10 + text[10:].index("+")]
    text = text.strip()
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    # A datamart varchar often carries more precision than the format expects
    # (milliseconds, a trailing timezone name); try the trimmed prefixes too.
    for candidate in (text, text[:19], text[:16], text[:10]):
        for fmt in _TS_FORMATS:
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    return None


def operation_type(init_module: Any) -> str:
    """P2P (WERO) vs EMC (WEROEMC) — the prototype's distinction, from the one
    column that carries it in the datamart."""
    module = (_clean(init_module) or "").upper()
    if module == "WEROEMC":
        return "EMC"
    if module == "WERO":
        return "P2P"
    return module or "UNKNOWN"


# ---------------------------------------------------------------------------
# Queries — three independent streams, no join in this module
# ---------------------------------------------------------------------------

def wero_status_params(cfg: Dict[str, Any]) -> List[str]:
    """The SettlementStatus values bound by build_wero_query, in SQL order.

    Sorted, and derived by the SAME function the query builder uses, so the
    placeholders and the parameters can never drift apart. Empty when no
    whitelist is configured (or when there is no status column to filter on),
    in which case the query carries no IN clause at all.
    """
    statuses = cfg.get("wero_settlement_statuses") or ()
    if not statuses or not cfg.get("wero_status_column"):
        return []
    return sorted(statuses)


def build_wero_query(cfg: Dict[str, Any]) -> str:
    """Incremental read of the WERO table.

    The status whitelist is applied HERE rather than when mapping the row:
    skipping a row in wero_row_to_entry would turn every filtered row into a
    run error, and would stream the rows only to throw them away.
    """
    statuses = wero_status_params(cfg)
    status_filter = ""
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        # UPPER() both sides: the values are normalised uppercase at load time,
        # and MSSQL's collation is case-insensitive anyway — this keeps the
        # comparison explicit rather than collation-dependent.
        status_filter = f" AND UPPER({cfg['wero_status_column']}) IN ({placeholders})"
    return (
        f"SELECT * FROM {cfg['wero_table']} "
        "WHERE IsCurrent = 1 "
        f"AND {cfg['wero_watermark_column']} >= ?"
        f"{status_filter}"
    )


def build_payment_query(cfg: Dict[str, Any]) -> str:
    """WERO payments booked in Finacle. The InitModule filter is what makes a
    Finacle orphan detectable at all — the prototype never looked for them
    (INCLUDE_FINACLE_ORPHANS = False)."""
    placeholders = ",".join("?" * len(cfg["payment_init_modules"]))
    currency = (
        f"p.{cfg['payment_currency_column']} AS ccy"
        if cfg.get("payment_currency_column")
        else "CAST(NULL AS VARCHAR(8)) AS ccy"
    )
    return (
        "SELECT LTRIM(RTRIM(p.PaymentNumber)) AS po_id, "
        f"       p.{cfg['payment_e2e_column']} AS e2e_ref, "
        f"       p.{PAYMENT_AMOUNT_COL} AS amount, "
        "       LTRIM(RTRIM(p.Status)) AS status, "
        f"       p.{PAYMENT_INIT_MODULE_COLUMN} AS init_module, "
        f"       p.{cfg['payment_date_column']} AS payment_date, "
        "       LTRIM(RTRIM(p.MessageID)) AS message_id, "
        "       LTRIM(RTRIM(p.MessageIDPACS008)) AS pacs008, "
        f"       {currency} "
        "FROM std.Payment p "
        "WHERE p.IsCurrent = 1 "
        f"  AND p.{PAYMENT_INIT_MODULE_COLUMN} IN ({placeholders}) "
        f"  AND p.{cfg['payment_date_column']} >= ?"
    )


def build_return_query(cfg: Dict[str, Any]) -> str:
    """Returns of WERO payments. ``[Return]`` is bracketed — reserved word.

    Amount, status and date default to the ORIGINAL payment's: those are the
    only columns of the pair we have confirmed, and it is what
    reco_datamart_bb.resolve_return_payments already does. Configure the
    return_* keys to read them off the return itself.
    """
    placeholders = ",".join("?" * len(cfg["payment_init_modules"]))
    date_expr = (
        f"r.{cfg['return_date_column']}"
        if cfg.get("return_date_column")
        else f"p.{cfg['payment_date_column']}"
    )
    amount_expr = (
        f"r.{cfg['return_amount_column']}"
        if cfg.get("return_amount_column")
        else f"p.{PAYMENT_AMOUNT_COL}"
    )
    status_expr = (
        f"r.{cfg['return_status_column']}"
        if cfg.get("return_status_column")
        else "p.Status"
    )
    currency = (
        f"p.{cfg['payment_currency_column']} AS ccy"
        if cfg.get("payment_currency_column")
        else "CAST(NULL AS VARCHAR(8)) AS ccy"
    )
    return (
        "SELECT LTRIM(RTRIM(r.PaymentNumber)) AS po_id, "
        "       LTRIM(RTRIM(r.OriginalPo)) AS original_po, "
        # Display only (it rides in payload_raw), but a reject motive is
        # exactly what an operator wants on an unmatched return.
        "       LTRIM(RTRIM(r.ReturnReasonCode)) AS return_reason, "
        f"       p.{cfg['payment_e2e_column']} AS e2e_ref, "
        f"       {amount_expr} AS amount, "
        f"       LTRIM(RTRIM({status_expr})) AS status, "
        f"       p.{PAYMENT_INIT_MODULE_COLUMN} AS init_module, "
        f"       {date_expr} AS return_date, "
        f"       {currency} "
        "FROM std.[Return] r "
        "INNER JOIN std.Payment p "
        "  ON p.PaymentNumber = r.OriginalPo AND p.IsCurrent = 1 "
        "WHERE r.IsCurrent = 1 "
        f"  AND p.{PAYMENT_INIT_MODULE_COLUMN} IN ({placeholders}) "
        f"  AND {date_expr} >= ?"
    )


def iter_chunks(
    conn, sql: str, params: List[Any], chunk_size: int = CHUNK_SIZE
) -> Iterator[List[Dict[str, Any]]]:
    """Stream a result set as dicts, chunk by chunk (fetchmany pattern)."""
    cursor = conn.cursor()
    try:
        cursor.execute(sql, params)
        columns = [d[0] for d in cursor.description]
        while True:
            rows = cursor.fetchmany(chunk_size)
            if not rows:
                break
            yield [dict(zip(columns, row)) for row in rows]
    finally:
        cursor.close()


# ---------------------------------------------------------------------------
# Row -> entry
# ---------------------------------------------------------------------------

def _entry(
    *,
    reco_id: Optional[str],
    external_ref: str,
    event_type: str,
    amount: Decimal,
    currency: str,
    when: datetime,
    ref_no: Optional[str],
    remarks_1: Optional[str],
    particulars: Optional[str],
    transaction_id: Optional[str],
    row: Dict[str, Any],
) -> Dict[str, Any]:
    """Common shape of a WERO entry (the /tasks/finacle/runs/{id}/batch contract).

    ``operation_date`` mirrors ``value_date`` on purpose: the backend hashes
    ``operation_date or value_date`` date-only, so both must be stable across
    runs or a re-read would mint a duplicate instead of upserting.
    """
    return {
        "reco_id": reco_id,
        "account": None,  # WERO has no GL account — the perimeter is InitModule
        "currency": currency,
        "amount": str(amount),
        "value_date": when.isoformat(),
        "operation_date": when.isoformat(),
        "direction": "credit" if amount > 0 else "debit",
        "event_type": event_type[:32],
        "external_ref": external_ref[:128],
        "transaction_particulars": (particulars or None),
        "ref_no": (ref_no or None),
        "remarks_1": (remarks_1 or None),
        "transaction_id": (transaction_id or None),
        "payload_raw": _json_safe(row),
    }


def wero_row_to_entry(row: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """One WERO row -> one CREDIT entry (+X). Raises ValueError for a row that
    cannot be mapped; the caller reports it as a row error."""
    base = normalize_ref(row.get(cfg["wero_ref_column"]))
    status = _clean(row.get(cfg["wero_status_column"])) if cfg.get("wero_status_column") else None
    is_reversal = bool(status) and status.upper() in cfg["wero_reversal_statuses"]

    # A row with no end-to-end reference is pushed with reco_id NULL: it is
    # excluded from auto-matching and stays visible as pending, which is the
    # signal wanted. Dropping it would hide it from the operational view.
    reco_id = return_reco_id(base) if is_reversal else base

    amount = parse_amount(row.get(cfg["wero_amount_column"]))
    when = parse_timestamp(row.get(cfg["wero_date_column"])) or parse_timestamp(
        row.get(cfg["wero_watermark_column"])
    )
    if when is None:
        raise ValueError(
            f"no usable date in {cfg['wero_date_column']} / {cfg['wero_watermark_column']}"
        )

    transfer_id = _clean(row.get(cfg["wero_id_column"])) if cfg.get("wero_id_column") else None
    # Identity: the transfer id when present, else the reference — never the
    # SCD2 surrogate key, which changes on every row version.
    identity = transfer_id or _clean(row.get(cfg["wero_ref_column"]))
    if not identity:
        raise ValueError("neither transfer id nor originator reference")

    direction = _clean(row.get(cfg["wero_direction_column"])) if cfg.get("wero_direction_column") else None
    prefix = "WRVS" if is_reversal else "WERO"
    return _entry(
        reco_id=reco_id,
        external_ref=f"{prefix}:{identity}",
        event_type="WERO_RVSL" if is_reversal else "WERO",
        amount=abs(amount),  # WERO side is always the credit leg
        currency=(_clean(row.get(cfg["wero_currency_column"])) or cfg["default_currency"])[:8],
        when=when,
        ref_no=transfer_id,
        remarks_1="/".join(p for p in ("WERO", direction) if p)[:255],
        particulars=status,
        transaction_id=_clean(row.get(cfg["wero_ref_column"])),
        row=row,
    )


def payment_row_to_entry(row: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """One std.Payment row -> one DEBIT entry (-X)."""
    po_id = _clean(row.get("po_id"))
    if not po_id:
        raise ValueError("PaymentNumber is empty")
    amount = parse_amount(row.get("amount"))
    when = parse_timestamp(row.get("payment_date"))
    if when is None:
        raise ValueError(f"no usable date in {cfg['payment_date_column']}")
    op_type = operation_type(row.get("init_module"))
    return _entry(
        reco_id=normalize_ref(row.get("e2e_ref")),
        external_ref=f"PAY:{po_id}",
        event_type="PAYMENT",
        amount=-abs(amount),
        currency=(_clean(row.get("ccy")) or cfg["default_currency"])[:8],
        when=when,
        ref_no=po_id,
        remarks_1=op_type,
        particulars=_clean(row.get("status")),
        transaction_id=_clean(row.get("e2e_ref")),
        row=row,
    )


def return_row_to_entry(row: Dict[str, Any], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """One std.[Return] row -> one DEBIT entry (-X) under ``<e2e>#RET``."""
    po_id = _clean(row.get("po_id"))
    if not po_id:
        raise ValueError("return PaymentNumber is empty")
    amount = parse_amount(row.get("amount"))
    when = parse_timestamp(row.get("return_date"))
    if when is None:
        raise ValueError("no usable return date")
    op_type = operation_type(row.get("init_module"))
    return _entry(
        reco_id=return_reco_id(normalize_ref(row.get("e2e_ref"))),
        external_ref=f"RET:{po_id}",
        event_type="RETURN",
        amount=-abs(amount),
        currency=(_clean(row.get("ccy")) or cfg["default_currency"])[:8],
        when=when,
        ref_no=po_id,
        remarks_1="/".join(p for p in (op_type, "RETURN") if p)[:255],
        particulars=_clean(row.get("status")),
        transaction_id=_clean(row.get("original_po")),
        row=row,
    )


def payment_status_row(entry: Dict[str, Any], row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The (reco_id, po_id, status, amount) tuple feeding the operational view's
    payment-status column. Free here: the Finacle leg IS std.Payment.

    ``payment_timestamp`` is naive by contract — the whole chain keeps the
    datamart wall clock so the hour shown is the hour filtered on.
    """
    reco_id = entry.get("reco_id")
    po_id = _clean(entry.get("ref_no"))
    if not reco_id or not po_id:
        return None
    when = parse_timestamp(row.get("payment_date") or row.get("return_date"))
    amount = _clean(row.get("amount"))
    return {
        "reco_id": reco_id[:128],
        "po_id": po_id[:64],
        "status": (entry.get("transaction_particulars") or None),
        "amount": amount,
        "payment_timestamp": when.isoformat() if when else None,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

class _StatusPusher:
    """Accumulates payment-status rows, deduped on (reco_id, po_id), and flushes
    them in batches — the same dedup reco_payment_status does, for the same
    reason: a reconciliation group's payment set is pushed once, not per leg."""

    def __init__(self, flow_code: str) -> None:
        self.flow_code = flow_code
        self.seen: set = set()
        self.pending: List[Dict[str, Any]] = []
        self.pushed = 0

    def add(self, row: Optional[Dict[str, Any]]) -> None:
        if not row:
            return
        key = (row["reco_id"], row["po_id"])
        if key in self.seen:
            return
        self.seen.add(key)
        self.pending.append(row)
        if len(self.pending) >= PS_PUSH_BATCH:
            self.flush()

    def flush(self) -> None:
        if not self.pending:
            return
        finacle_post_payment_status_batch(
            {"flow_code": self.flow_code, "rows": self.pending}
        )
        self.pushed += len(self.pending)
        self.pending = []


def _push(run_id: int, entries: List[Dict[str, Any]], errors: List[str]) -> int:
    if not entries and not errors:
        return 0
    finacle_post_batch(run_id, entries, errors)
    return len(entries)


def _stream_leg(
    conn,
    *,
    run_id: int,
    sql: str,
    params: List[Any],
    mapper,
    cfg: Dict[str, Any],
    label: str,
    key_column: str,
    status_pusher: Optional[_StatusPusher] = None,
) -> Tuple[int, int]:
    """Stream one leg, map + push chunk by chunk. Returns (pushed, errors)."""
    pushed = errored = 0
    for chunk in iter_chunks(conn, sql, params):
        entries: List[Dict[str, Any]] = []
        errors: List[str] = []
        for row in chunk:
            try:
                entry = mapper(row, cfg)
            except ValueError as exc:
                # Every error is reported: the backend counts them into rows_ko
                # (and truncates only the stored text), so capping here would
                # silently under-report the run.
                errored += 1
                errors.append(f"{label} {row.get(key_column)!r}: {exc}")
                continue
            entries.append(entry)
            if status_pusher is not None:
                status_pusher.add(payment_status_row(entry, row))
        pushed += _push(run_id, entries, errors)
    logger.info("[ingest_wero] %s: %d entries pushed, %d row error(s)", label, pushed, errored)
    return pushed, errored


def _ingest_source(conn, source: Dict[str, Any], dag_run_id: Optional[str]) -> int:
    """One WERO source: three streaming passes, one ingestion run.

    There is no unresolved-retry loop here, unlike reco_datamart. That loop
    exists because a finacle reco_id depends on lookup maps that fill in over
    time; a WERO reco_id is read straight off the row, so re-deriving it from
    the stored snapshot could only ever return the same answer. A leg with no
    usable reference is pushed with reco_id NULL and stays visibly pending.
    """
    flow_code, source_code = source["flow_code"], source["source_code"]
    cfg = load_config(source.get("parser_config"))
    since = _compute_since(source.get("last_success_at"), source.get("backfill_since"))
    modules = cfg["payment_init_modules"]
    logger.info(
        "[ingest_wero] %s/%s: extracting since %s (table=%s, e2e=%s, modules=%s)",
        flow_code, source_code, since.isoformat(),
        cfg["wero_table"], cfg["payment_e2e_column"], modules,
    )

    run_id = finacle_start_run(flow_code, source_code, dag_run_id)
    total = 0
    try:
        status_pusher = _StatusPusher(flow_code)

        # (a) WERO side — the credit legs.
        pushed, _ = _stream_leg(
            conn, run_id=run_id,
            sql=build_wero_query(cfg), params=[since, *wero_status_params(cfg)],
            mapper=wero_row_to_entry, cfg=cfg,
            label="wero", key_column=cfg["wero_ref_column"],
        )
        total += pushed

        # (b) std.Payment side — the debit legs, and the Finacle orphans.
        pushed, _ = _stream_leg(
            conn, run_id=run_id,
            sql=build_payment_query(cfg), params=[*modules, since],
            mapper=payment_row_to_entry, cfg=cfg,
            label="payment", key_column="po_id",
            status_pusher=status_pusher,
        )
        total += pushed

        # (c) std.[Return] side — the debit legs of the return pair.
        if cfg["returns_enabled"]:
            pushed, _ = _stream_leg(
                conn, run_id=run_id,
                sql=build_return_query(cfg), params=[*modules, since],
                mapper=return_row_to_entry, cfg=cfg,
                label="return", key_column="po_id",
                status_pusher=status_pusher,
            )
            total += pushed

        status_pusher.flush()
        logger.info("[ingest_wero] %s/%s: %d payment status row(s) pushed",
                    flow_code, source_code, status_pusher.pushed)

        finacle_complete_run(run_id)
        logger.info("[ingest_wero] %s/%s: run #%s done (%d entries pushed)",
                    flow_code, source_code, run_id, total)
    except Exception:
        try:
            finacle_complete_run(run_id, failed=True, error="see Airflow logs")
        except Exception:  # noqa: BLE001
            logger.warning("[ingest_wero] could not mark run #%s as failed", run_id)
        raise
    return total


def run_wero_ingestion(dag_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Extract the WERO legs for every active WERO source and push them."""
    summary: Dict[str, Any] = {"ingested": [], "skipped": [], "errors": []}
    sources = [
        s for s in task_list_finacle_sources().get("sources", [])
        if s.get("parser_type") == PARSER_TYPE_WERO
    ]
    if not sources:
        logger.info("[ingest_wero] No active WERO source — nothing to do.")
        return summary

    # Local import: keeps this module importable (tests, DAG parsing) without
    # the MSSQL provider installed.
    from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook

    hook = MsSqlHook(mssql_conn_id=DATAMART_CONN_ID)
    conn = get_pyodbc_conn(hook)
    try:
        for source in sources:
            label = f"{source['flow_code']}/{source['source_code']}"
            try:
                _ingest_source(conn, source, dag_run_id)
                summary["ingested"].append(label)
            except Exception as exc:  # noqa: BLE001
                logger.error("[ingest_wero] %s failed: %s", label, exc, exc_info=True)
                summary["errors"].append({"source": label, "error": str(exc)})
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass

    if summary["errors"]:
        raise RuntimeError(f"wero ingestion finished with errors: {summary['errors']}")
    return summary
