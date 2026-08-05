"""MT940 extraction for the orchestrator DAG (mirrors the 3_1_0 NOSTRO IP DAG).

The backend hands the raw MT940 file content to the DAG (files can be large and
the DAG — unlike the backend — reaches the datamart directly). For each
``extract_via_dag`` MT940 source the DAG:

  1. lists the inbox files and, per file, opens a RUNNING run + pulls the content
     from the backend (``/tasks/mt940/*``);
  2. parses the SWIFT :61:/:86: transactions; each tx carries TWO candidate
     references: the ``+21`` sub-field of ``:86:`` — the PRIMARY lookup key when
     non-empty (e.g. ``IPAb01150c7f6fc4b3…``, the only one that matches
     std.Payment on IPB lines) — and the ``:61:`` continuation line (identical
     to the ``+00`` sub-field), primary only when ``+21`` is empty (NML…),
     otherwise the resolution FALLBACK;
  3. resolves each primary key against ``std.Payment.TransactionRef`` (the same
     join the reversal path uses — see ``reco_datamart.resolve_reversals``) to
     the payment's po_id, the very key the finacle IP leg uses as reco_id
     (``PaymentOrderID_Ref`` on std.Movement); keys the first pass missed are
     retried ONCE with their ``:61:`` fallback in a second batched pass
     (``resolve_with_fallback`` — file-time only, the unresolved retry of step 5
     stays single-key);
  4. pushes the entries back to the backend in batches and completes the run
     (the backend moves the file to processed/error);
  5. retries the source's previously-pushed unresolved entries WITHOUT re-parsing
     any file: the backend returns their pending keys (``/tasks/mt940/unresolved``),
     the same TransactionRef join resolves them, and the backend fills reco_id in
     place (``/tasks/mt940/resolve``) — mirror of the reco_datamart unresolved
     retry, minus the re-push (the MT940 source_hash includes the amount, whose
     representation may change through a DB round-trip, so re-pushing could
     silently duplicate rows).

The PRIMARY key (+21 when present, else the :61: ref — see ``_lookup_keys``) is
pushed in ``remarks_1`` and ``ref_no`` is left EMPTY on purpose: ref_no is the
classic-IP key slot (po_id), and ref_no-keyed retry/sweep logic
(``reco_datamart.compute_reco_id`` ref_no fallback, backend
``resolve_flow_references``) must ignore MT940 entries.

References the datamart can't resolve are pushed with ``reco_id=None`` — payment
not in std.Payment yet, or possibly rejected (dedicated handling is a next
step); step 5 picks them up on every later run.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from reco_common import (
    mt940_complete_run,
    mt940_list_inbox,
    mt940_list_sources,
    mt940_list_unresolved,
    mt940_open_run,
    mt940_post_batch,
    mt940_post_resolved,
)
from reco_datamart import (
    CHUNK_SIZE,
    DATAMART_CONN_ID,
    PO_INSERT_BATCH,
    _clean,
    _json_safe,
    get_pyodbc_conn,
)

logger = logging.getLogger(__name__)

# :61: transaction line (SWIFT). Mirrors backend MT940Parser._RE_TAG_61, except
# the owner ref (SWIFT 16x) may contain single '/' (e.g. OVI/I/0113/UQcPZ);
# only '//' introduces the bank ref.
_RE_TAG_61 = re.compile(
    r"^(?P<val_date>\d{6})"
    r"(?P<entry_date>\d{4})?"
    r"(?P<dc>[DCRdcr]{1,2})"
    r"(?P<funds>[A-Z])?"
    r"(?P<amount>[\d,]+)"
    r"(?P<tx_type>[A-Z]\w{3})"
    r"(?P<ref>(?:[^\s/]|/(?!/)){0,16})"
    r"(?://(?P<bank_ref>[^\s]{0,16}))?"
    r"(?:\s+(?P<extra>.+))?"
    r"$"
)
_RE_SUBFIELD = re.compile(r"\+(\d{2})")
_RE_BALANCE = re.compile(r"[DC]\d{6}([A-Z]{3})")

# TODO: confirmer le nom exact sur le datamart (schéma std.Payment non confirmé ;
# PaymentOrderID_Ref = convention std.Movement). Seul endroit à corriger. for payment it's PaymentNumber.
PAYMENT_PO_ID_COLUMN = "PaymentNumber"


# ---------------------------------------------------------------------------
# Parsing (ported from backend MT940Parser — the validated implementation)
# ---------------------------------------------------------------------------

def _split_messages(text: str) -> List[str]:
    messages: List[str] = []
    for part in re.split(r"(?=\{1:F01)", text):
        part = part.strip()
        if part and ":61:" in part:
            messages.append(part)
    return messages


def _extract_tags(message: str) -> Dict[str, Any]:
    tags: Dict[str, Any] = {}
    transactions: List[Tuple[str, str]] = []

    block4 = re.search(r"\{4:\s*\n?(.*?)(?:-\}|$)", message, re.DOTALL)
    content = block4.group(1) if block4 else message

    tag_pattern = re.compile(r"^:(\d{2}[A-Z]?):(.*)$", re.MULTILINE)
    positions = [(m.start(), m.group(1)) for m in tag_pattern.finditer(content)]

    current_61: Optional[str] = None
    for i, (pos, code) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(content)
        full_value = content[pos + len(f":{code}:"):end].strip()
        if code == "61":
            current_61 = full_value
        elif code == "86":
            if current_61 is not None:
                transactions.append((current_61, full_value))
                current_61 = None
        else:
            tags[code] = full_value
            current_61 = None
    if current_61 is not None:
        transactions.append((current_61, ""))

    tags["_transactions"] = transactions
    return tags


def _parse_tag86(tag86: str) -> Dict[str, str]:
    if not tag86:
        return {}
    text = re.sub(r"\n\s*", "", tag86)
    matches = list(_RE_SUBFIELD.finditer(text))
    if not matches:
        return {"_raw": text}
    subfields: Dict[str, str] = {}
    gvc = text[: matches[0].start()].strip()
    if gvc:
        subfields["_gvc"] = gvc
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        subfields[m.group(1)] = text[start:end].strip()
    return subfields


def _currency_from_balance(tags: Dict[str, Any]) -> Optional[str]:
    for code in ("60F", "60M", "62F", "62M"):
        m = _RE_BALANCE.match(tags.get(code, ""))
        if m:
            return m.group(1)
    return None


def _parse_date(yymmdd: str) -> datetime:
    if len(yymmdd) != 6:
        raise ValueError(f"invalid date '{yymmdd}' (expected YYMMDD)")
    return datetime(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]), tzinfo=timezone.utc)


def _parse_amount(raw: str) -> Decimal:
    s = (raw or "").strip().replace(",", ".")
    if not s:
        raise ValueError("empty amount")
    try:
        return Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"cannot parse amount '{raw}'") from exc


def extract_transactions(
    content: str,
    *,
    file_name: str,
    default_currency: str = "EUR",
    account_override: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Parse MT940 file content into raw transaction records (reco_id not yet
    resolved). Returns (records, errors)."""
    records: List[Dict[str, Any]] = []
    errors: List[str] = []

    print(f"[ingest_mt940] {file_name}: parsing content ({len(content)} bytes)")
    for msg_idx, msg in enumerate(_split_messages(content), start=1):
        if msg_idx % 10 == 0:
            print(f"[ingest_mt940] {file_name}: parsed {msg_idx} messages so far")
        try:
            tags = _extract_tags(msg)
        except Exception as exc:  # noqa: BLE001
            print(f"[ingest_mt940] {file_name}: failed to extract tags for message {msg_idx}: {exc}")
            errors.append(f"message {msg_idx}: failed to extract tags: {exc}")
            continue

        account = account_override or (tags.get("25", "") or "").strip() or None
        statement_ref = (tags.get("20", "") or "").strip()
        currency = _currency_from_balance(tags) or default_currency

        for tx_idx, (tag61, tag86) in enumerate(tags.get("_transactions", []), start=1):
            try:
                lines_61 = tag61.split("\n", 1)
                main_line = lines_61[0].strip()
                customer_ref = lines_61[1].strip() if len(lines_61) > 1 else ""
                m = _RE_TAG_61.match(main_line)
                if not m:
                    raise ValueError(f"cannot parse :61: line: '{main_line[:80]}'")

                value_date = _parse_date(m.group("val_date"))
                entry_date_str = m.group("entry_date") or ""
                operation_date = value_date
                if entry_date_str:
                    try:
                        operation_date = datetime(
                            value_date.year, int(entry_date_str[:2]), int(entry_date_str[2:4]),
                            tzinfo=timezone.utc,
                        )
                    except (ValueError, IndexError):
                        operation_date = value_date

                dc_mark = m.group("dc").upper()
                tx_type = m.group("tx_type") or ""
                amount = abs(_parse_amount(m.group("amount")))

                subfields = _parse_tag86(tag86) if tag86 else {}
                # Reconciliation reference = the :61: continuation line (customer
                # ref), identical to the :86: +00 sub-field which serves as the
                # fallback; the +21 message id is NOT the key (often empty).
                ref_00 = (subfields.get("00") or "").strip() or None
                transaction_ref = customer_ref or ref_00
                if customer_ref and ref_00 and customer_ref != ref_00:
                    print(f"[ingest_mt940] {file_name}: msg {msg_idx}, tx {tx_idx}: "
                          f":61: continuation '{customer_ref}' != :86: +00 '{ref_00}' — keeping :61:")

                records.append({
                    "transaction_ref": transaction_ref,
                    "ref_21": (subfields.get("21") or "").strip() or None,
                    "account": account,
                    "currency": currency,
                    "amount": amount,            # absolute; sign applied later
                    "dc_mark": dc_mark,
                    "value_date": value_date,
                    "operation_date": operation_date,
                    "tx_type": tx_type or None,
                    "file_name": file_name,
                    "msg_idx": msg_idx,
                    "tx_idx": tx_idx,
                    "statement_ref": statement_ref,
                    "customer_ref": customer_ref or None,
                    "owner_ref": m.group("ref") or None,
                    "bank_ref": m.group("bank_ref") or None,
                    "tag86_subfields": subfields,
                })
            except Exception as exc:  # noqa: BLE001
                print(f"[ingest_mt940] {file_name}: failed to parse message {msg_idx}, tx {tx_idx}: {exc}")
                errors.append(f"msg {msg_idx}, tx {tx_idx}: {exc}")

    return records, errors


# ---------------------------------------------------------------------------
# reco_id resolution against the datamart (temp-table JOINs, two passes max)
# ---------------------------------------------------------------------------

# The temp table column is VARCHAR(64); exotic +21 values can exceed it.
_MAX_REF_LEN = 64


def _lookup_keys(record: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    """(primary, fallback) lookup keys of one parsed record: the +21 is the
    PRIMARY key when non-empty (on IPB lines it is the only one matching
    std.Payment), the :61: ref its file-time fallback; when +21 is empty the
    :61: ref is primary with no fallback. Single definition of the rule —
    remarks_1 stores the primary key."""
    ref_21 = _clean(record.get("ref_21"))
    transaction_ref = _clean(record.get("transaction_ref"))
    if ref_21:
        return ref_21, (transaction_ref if transaction_ref != ref_21 else None)
    return transaction_ref, None


def resolve_with_fallback(lookup_conn, pairs) -> Dict[str, Optional[str]]:
    """Resolve (primary, fallback) key pairs in at most TWO batched joins:
    pass 1 queries every primary key; the missed ones with a distinct fallback
    are retried once in pass 2 ('empty the temp table of the found ones and
    retry with the rest'). Returns {primary: po_id}, hits from either pass
    re-anchored on the primary key (= what remarks_1 stores)."""
    fallback_by_primary: Dict[str, Optional[str]] = {}
    for primary, fallback in pairs:
        p = _clean(primary)
        if p:
            fallback_by_primary.setdefault(p, _clean(fallback))

    result = resolve_mt940_reco_ids(lookup_conn, fallback_by_primary.keys())

    missed = {
        p: f for p, f in fallback_by_primary.items()
        if not result.get(p) and f and f != p
    }
    if missed:
        fallback_map = resolve_mt940_reco_ids(lookup_conn, missed.values())
        rescued = 0
        for primary, fallback in missed.items():
            po_id = fallback_map.get(fallback)
            if po_id:
                result[primary] = po_id
                rescued += 1
        logger.info("[ingest_mt940] fallback pass rescued %d/%d missed key(s) via the :61: ref",
                    rescued, len(missed))
    return result


def resolve_mt940_reco_ids(lookup_conn, refs) -> Dict[str, Optional[str]]:
    """Map each :61: TransactionRef to its canonical reco_id (the payment's
    po_id) in ONE temp-table JOIN — the mirror of
    ``reco_datamart.resolve_reversals``, which already keys std.Payment on
    ``TransactionRef``.

    Returns {transaction_ref: po_id}. The po_id (``PAYMENT_PO_ID_COLUMN``) is
    the very key the finacle IP leg uses as reco_id (``PaymentOrderID_Ref`` on
    std.Movement), so both sides of the flow match on it. Refs with no current
    payment are simply absent → reco_id stays None (the backend sweep retries
    them).
    """
    result: Dict[str, Optional[str]] = {}
    cleaned = {_clean(r) for r in refs}
    too_long = {r for r in cleaned if r and len(r) > _MAX_REF_LEN}
    if too_long:
        logger.warning("[ingest_mt940] skipping %d ref(s) longer than %d chars (temp table limit)",
                       len(too_long), _MAX_REF_LEN)
    ref_list = [r for r in cleaned - too_long if r]
    if not ref_list:
        return result

    cursor = lookup_conn.cursor()
    try:
        cursor.execute("IF OBJECT_ID('tempdb..#mt940_ref') IS NOT NULL DROP TABLE #mt940_ref")
        # Non-unique index, built after the load — see reco_datamart's
        # _NON_UNIQUE_TEMP_INDEX for why a PRIMARY KEY here aborts whole runs.
        cursor.execute("CREATE TABLE #mt940_ref (ref VARCHAR(64) NOT NULL)")
        cursor.fast_executemany = True
        for i in range(0, len(ref_list), PO_INSERT_BATCH):
            cursor.executemany(
                "INSERT INTO #mt940_ref (ref) VALUES (?)",
                [(r,) for r in ref_list[i:i + PO_INSERT_BATCH]],
            )
        cursor.execute("CREATE CLUSTERED INDEX ix_temp_k ON #mt940_ref (ref)")
        cursor.execute(
            f"""
            SELECT t.ref, p.{PAYMENT_PO_ID_COLUMN}
            FROM #mt940_ref t
            INNER JOIN std.Payment p ON p.TransactionRef = t.ref
            WHERE p.IsCurrent = 1
            """
        )
        for ref, po_id in cursor.fetchall():
            r = _clean(ref)
            if r is not None:
                result[r] = _clean(po_id)
        cursor.execute("IF OBJECT_ID('tempdb..#mt940_ref') IS NOT NULL DROP TABLE #mt940_ref")
        logger.info("[ingest_mt940] resolved %d/%d TransactionRefs via std.Payment join",
                    len(result), len(ref_list))
    finally:
        cursor.close()
    return result


def record_to_entry(record: Dict[str, Any], reco_map: Dict[str, Optional[str]]) -> Dict[str, Any]:
    """Map one parsed record + the resolved reco_id map to a JSON-safe entry dict
    (mirror of ``reco_datamart.movement_row_to_entry``).

    Amount is signed from the D/C mark (D -> negative/debit, C -> positive/credit)
    so auto-matching (sum=0 per reco_id) works against the finacle mirror leg.
    """
    dc = (record.get("dc_mark") or "").upper()
    is_debit = dc in ("D", "RD")
    amount = -abs(record["amount"]) if is_debit else abs(record["amount"])
    direction = "debit" if is_debit else "credit"

    primary_key, _ = _lookup_keys(record)
    # TODO(next step): aucun std.Payment pour cette clé (ni via le fallback
    # :61: de resolve_with_fallback) → paiement possiblement rejeté/annulé ;
    # traitement dédié à venir. Pour l'instant reco_id=None (retry mono-clé
    # via _retry_unresolved à chaque run du DAG).
    reco_id = reco_map.get(primary_key or "")

    # Stable per-line identity (reco_id-independent source_hash on the backend):
    # same file re-pushed -> same external_ref -> dedup no-op.
    external_ref = f"{record['file_name']}#{record['msg_idx']}#{record['tx_idx']}"

    value_date = record["value_date"]
    operation_date = record.get("operation_date") or value_date

    return {
        "reco_id": reco_id,
        "account": record.get("account"),
        "currency": record.get("currency") or "EUR",
        "amount": str(amount),
        "value_date": value_date.isoformat(),
        "operation_date": operation_date.isoformat() if operation_date else None,
        "direction": direction,
        "event_type": record.get("tx_type"),
        "external_ref": external_ref,
        "file_name": record.get("file_name"),
        # ref_no is the classic-IP key slot (po_id): left EMPTY on purpose so
        # ref_no-keyed retry/sweep logic ignores MT940 entries. remarks_1 holds
        # the PRIMARY lookup key (+21 when present, else the :61: ref) — also
        # what _retry_unresolved re-resolves later (single-key, no fallback).
        "ref_no": None,
        "remarks_1": primary_key,
        "payload_raw": _json_safe({
            "statement_ref": record.get("statement_ref"),
            "dc_mark": dc,
            "owner_ref": record.get("owner_ref"),
            "bank_ref": record.get("bank_ref"),
            "customer_ref": record.get("customer_ref"),
            "tx_type": record.get("tx_type"),
            "transaction_ref": record.get("transaction_ref"),
            "ref_21": record.get("ref_21"),
            "tag86_subfields": record.get("tag86_subfields"),
            "_msg_idx": record.get("msg_idx"),
            "_tx_idx": record.get("tx_idx"),
        }),
    }


def _retry_unresolved(lookup_conn, *, flow_code: str, source_code: str) -> Dict[str, int]:
    """Retry the source's previously-pushed unresolved entries (reco_id NULL)
    without re-parsing any file: their PRIMARY keys live in remarks_1, so the
    backend hands them back, the same std.Payment TransactionRef join resolves
    them (single-key — the :61: fallback only happens at file time), and the
    backend UPDATEs reco_id in place. Mirror of the reco_datamart unresolved
    retry, minus the re-push (the MT940 source_hash includes the amount, whose
    representation may change through a DB round-trip → re-pushing could
    silently duplicate rows). Returns {} when there is nothing to retry.
    """
    keys = mt940_list_unresolved(flow_code, source_code).get("keys", [])
    if not keys:
        return {}
    reco_map = resolve_mt940_reco_ids(lookup_conn, keys)
    # TODO(next step): les clés qui ne résolvent toujours pas ici = paiement
    # possiblement rejeté/annulé ; traitement dédié à venir.
    resolved = {k: v for k, v in reco_map.items() if v}
    updated = 0
    if resolved:
        resp = mt940_post_resolved(flow_code, source_code, resolved)
        updated = int((resp.get("data") or {}).get("updated", 0))
    counts = {"keys": len(keys), "resolved": len(resolved), "updated": updated}
    print(f"[ingest_mt940] {flow_code}/{source_code}: unresolved retry -> {counts}")
    return counts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _process_file(
    lookup_conn,
    *,
    flow_code: str,
    source_code: str,
    file_name: str,
    parser_config: Dict[str, Any],
) -> int:
    """Open the run, parse + resolve + push the file's transactions, complete the run."""
    opened = mt940_open_run(flow_code, source_code, file_name)
    run_id, content = opened["run_id"], opened["content"]
    print(f"[ingest_mt940] {flow_code}/{source_code}:{file_name} opened run #{run_id} ({len(content)} bytes)")
    try:
        records, errors = extract_transactions(
            content,
            file_name=file_name,
            default_currency=parser_config.get("default_currency", "EUR"),
            account_override=parser_config.get("account_override"),
        )
        reco_map = resolve_with_fallback(lookup_conn, (_lookup_keys(r) for r in records))
        print(f"[ingest_mt940] {flow_code}/{source_code}:{file_name} parsed {len(records)} tx, "
              f"{len(errors)} parse errors, resolved {len(reco_map)} reco_id(s)")

        total = 0
        for i in range(0, len(records), CHUNK_SIZE):
            batch = [record_to_entry(r, reco_map) for r in records[i:i + CHUNK_SIZE]]
            mt940_post_batch(run_id, batch, errors if i == 0 else [])
            total += len(batch)

        mt940_complete_run(run_id, file_name)
        logger.info("[ingest_mt940] %s/%s: %s -> %d entries pushed (%d parse errors)",
                    flow_code, source_code, file_name, total, len(errors))
        return total
    except Exception as exc:  # noqa: BLE001
        try:
            mt940_complete_run(run_id, file_name, failed=True, error=str(exc)[:500])
        except Exception:  # noqa: BLE001
            logger.warning("[ingest_mt940] could not mark run #%s as failed", run_id)
        raise


def run_mt940_ingestion(dag_run_id: Optional[str] = None) -> Dict[str, Any]:
    """Extract + resolve + push every inbox file of every active MT940 source,
    then retry each source's previously-pushed unresolved keys."""
    sources = mt940_list_sources().get("sources", [])
    summary: Dict[str, Any] = {"ingested": [], "skipped": [], "errors": [], "retried": {}}
    if not sources:
        logger.info("[ingest_mt940] No active MT940 source — nothing to do.")
        return summary

    from airflow.providers.microsoft.mssql.hooks.mssql import MsSqlHook

    hook = MsSqlHook(mssql_conn_id=DATAMART_CONN_ID)
    lookup_conn = get_pyodbc_conn(hook)
    try:
        for source in sources:
            flow_code, source_code = source["flow_code"], source["source_code"]
            label = f"{flow_code}/{source_code}"
            parser_config = source.get("parser_config") or {}
            files = mt940_list_inbox(flow_code, source_code).get("files", [])
            print(f"[ingest_mt940] {label}: {len(files)} inbox file(s) found")
            if not files:
                logger.info("[ingest_mt940] %s: no inbox file.", label)
                summary["skipped"].append(label)
            for file_name in files:
                try:
                    _process_file(
                        lookup_conn,
                        flow_code=flow_code,
                        source_code=source_code,
                        file_name=file_name,
                        parser_config=parser_config,
                    )
                    summary["ingested"].append(f"{label}:{file_name}")
                except Exception as exc:  # noqa: BLE001
                    logger.error("[ingest_mt940] %s:%s failed: %s", label, file_name, exc, exc_info=True)
                    summary["errors"].append({"source": label, "file": file_name, "error": str(exc)})
            # Retry pass — runs even when the inbox is empty: entries pushed on
            # earlier runs resolve as soon as their payment lands in std.Payment.
            try:
                counts = _retry_unresolved(lookup_conn, flow_code=flow_code, source_code=source_code)
                if counts:
                    summary["retried"][label] = counts
            except Exception as exc:  # noqa: BLE001
                logger.error("[ingest_mt940] %s: unresolved retry failed: %s", label, exc, exc_info=True)
                summary["errors"].append({"source": label, "file": None, "error": f"retry: {exc}"})
    finally:
        try:
            lookup_conn.close()
        except Exception:  # noqa: BLE001
            pass

    if summary["errors"]:
        raise RuntimeError(f"MT940 ingestion finished with errors: {summary['errors']}")
    return summary
