"""Unit tests for the WERO payment reconciliation (pure functions).

Imports shared/dags/reco_wero.py directly (module-level deps: stdlib + requests
via reco_common) — no DB, no pyodbc, no airflow, no app.* import.

The load-bearing test here is `test_wero_and_payment_legs_net_to_zero`: WERO is
reconciled by the *existing* engine, whose only rule is
``GROUP BY (flow_id, reco_id, currency) HAVING SUM(amount) = 0``. If the two
legs of a payment do not share a reco_id and a currency, and do not sum to
exactly zero, nothing will ever match.
"""
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

# CI and dev checkouts have shared/dags next to backend/; the backend docker
# container mounts only ./backend → skip there instead of failing collection.
DAGS_DIR = Path(__file__).resolve().parents[2] / "shared" / "dags"
if not DAGS_DIR.is_dir():
    pytest.skip(
        "shared/dags not mounted in this environment (backend-only container)",
        allow_module_level=True,
    )
sys.path.insert(0, str(DAGS_DIR))

from reco_wero import (  # noqa: E402
    DEFAULT_CONFIG,
    RET_SUFFIX,
    WeroConfigError,
    build_payment_query,
    build_return_query,
    build_wero_query,
    load_config,
    normalize_ref,
    wero_status_params,
    operation_type,
    parse_amount,
    parse_timestamp,
    payment_row_to_entry,
    payment_status_row,
    return_reco_id,
    return_row_to_entry,
    wero_row_to_entry,
)

CFG = load_config(None)

E2E = "019edb2f-1c12-72d3-9345-91a6317de646"
E2E_NORM = "019EDB2F1C1272D3934591A6317DE646"


def _wero_row(**over):
    row = {
        "OriginatorReference": E2E,
        "CaptureIDMoneyTransferID": "019edb2f-1c12-72d3-9345-91a6317de646",
        "SettlementRelatedTimestamp": "2026-08-20 11:04:31",
        "StartDate": date(2026, 8, 20),
        "TransactionAmount": "125.00",
        "Currency": "EUR",
        "TransactionDirection": "IN",
        "SettlementStatus": "SETTLED",
        "IsCurrent": True,
    }
    row.update(over)
    return row


def _payment_row(**over):
    row = {
        "po_id": "PO123456",
        "e2e_ref": E2E_NORM,
        "amount": Decimal("125.00"),
        "status": "PDNG",
        "init_module": "WERO",
        "payment_date": datetime(2026, 8, 20, 11, 5, 0),
        "message_id": "MSG1",
        "pacs008": "PACS1",
        "ccy": None,
    }
    row.update(over)
    return row


def _return_row(**over):
    row = {
        "po_id": "PO999",
        "original_po": "PO123456",
        "e2e_ref": E2E_NORM,
        "amount": Decimal("125.00"),
        "status": "RJCT",
        "init_module": "WEROEMC",
        "return_date": datetime(2026, 8, 22, 9, 0, 0),
        "ccy": None,
    }
    row.update(over)
    return row


# ---------------------------------------------------------------------------
# normalize_ref — the single normalisation used on all three sides
# ---------------------------------------------------------------------------

def test_uuid_with_and_without_dashes_give_the_same_key():
    assert normalize_ref(E2E) == normalize_ref(E2E.replace("-", "")) == E2E_NORM


def test_case_is_folded_because_mssql_collation_is_case_insensitive():
    # The RUMELANGE/Rumelange bug: MSSQL matches them, Python would not.
    assert normalize_ref("Rumelange-2412") == normalize_ref("RUMELANGE-2412")


def test_non_uuid_reference_keeps_its_dashes():
    assert normalize_ref("2412-20260731-RUMELANGE") == "2412-20260731-RUMELANGE"


def test_blank_reference_is_none():
    assert normalize_ref(None) is None
    assert normalize_ref("   ") is None


def test_reco_id_fits_the_column_even_with_the_return_suffix():
    long_ref = "X" * 400
    assert len(return_reco_id(normalize_ref(long_ref))) <= 128


# ---------------------------------------------------------------------------
# parse_amount — strict on purpose
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("125.00", Decimal("125.00")),
    ("125,00", Decimal("125.00")),
    (" 1 250.55 ", Decimal("1250.55")),
    (Decimal("-7.5"), Decimal("-7.5")),
])
def test_parse_amount_accepts_datamart_varchars(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "abc", "0", "0.00"])
def test_parse_amount_rejects_unusable_amounts(raw):
    # A leg silently worth 0 balances on its own under HAVING SUM = 0 and would
    # be auto-matched on thin air — it must be a row error instead.
    with pytest.raises(ValueError):
        parse_amount(raw)


# ---------------------------------------------------------------------------
# parse_timestamp
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2026-08-20 11:04:31", datetime(2026, 8, 20, 11, 4, 31)),
    ("2026-08-20T11:04:31Z", datetime(2026, 8, 20, 11, 4, 31)),
    ("2026-08-20", datetime(2026, 8, 20, 0, 0)),
    ("20/08/2026", datetime(2026, 8, 20, 0, 0)),
    (date(2026, 8, 20), datetime(2026, 8, 20, 0, 0)),
])
def test_parse_timestamp_handles_datamart_shapes(raw, expected):
    assert parse_timestamp(raw) == expected


def test_parse_timestamp_drops_the_offset_and_keeps_wall_clock():
    assert parse_timestamp("2026-08-20T11:04:31+02:00") == datetime(2026, 8, 20, 11, 4, 31)


def test_parse_timestamp_returns_none_when_unusable():
    assert parse_timestamp("not a date") is None
    assert parse_timestamp(None) is None


def test_operation_type_splits_p2p_from_emc():
    assert operation_type("WERO") == "P2P"
    assert operation_type("WEROEMC") == "EMC"
    assert operation_type(None) == "UNKNOWN"


# ---------------------------------------------------------------------------
# The convention the whole design rests on
# ---------------------------------------------------------------------------

def test_wero_and_payment_legs_net_to_zero():
    wero = wero_row_to_entry(_wero_row(), CFG)
    payment = payment_row_to_entry(_payment_row(), CFG)

    assert wero["reco_id"] == payment["reco_id"] == E2E_NORM
    assert wero["currency"] == payment["currency"] == "EUR"
    assert Decimal(wero["amount"]) + Decimal(payment["amount"]) == Decimal("0")


def test_wero_side_is_credit_and_finacle_side_is_debit():
    wero = wero_row_to_entry(_wero_row(), CFG)
    payment = payment_row_to_entry(_payment_row(), CFG)
    assert wero["direction"] == "credit" and Decimal(wero["amount"]) > 0
    assert payment["direction"] == "debit" and Decimal(payment["amount"]) < 0


def test_outgoing_wero_row_is_still_a_credit_leg():
    # The sign is a matching device, not an accounting statement: the business
    # direction lives in remarks_1, not in the sign.
    wero = wero_row_to_entry(_wero_row(TransactionDirection="OUT"), CFG)
    assert Decimal(wero["amount"]) > 0
    assert wero["remarks_1"] == "WERO/OUT"


def test_amount_discrepancy_leaves_the_group_unbalanced():
    wero = wero_row_to_entry(_wero_row(TransactionAmount="130.00"), CFG)
    payment = payment_row_to_entry(_payment_row(), CFG)
    assert Decimal(wero["amount"]) + Decimal(payment["amount"]) != Decimal("0")


# ---------------------------------------------------------------------------
# The return pair
# ---------------------------------------------------------------------------

def test_return_leg_sits_in_its_own_group():
    payment = payment_row_to_entry(_payment_row(), CFG)
    ret = return_row_to_entry(_return_row(), CFG)
    assert ret["reco_id"] == E2E_NORM + RET_SUFFIX
    assert ret["reco_id"] != payment["reco_id"]


def test_return_and_wero_reversal_net_to_zero():
    cfg = load_config({"wero_reversal_statuses": ["REVERSED"]})
    reversal = wero_row_to_entry(_wero_row(SettlementStatus="REVERSED"), cfg)
    ret = return_row_to_entry(_return_row(), cfg)

    assert reversal["reco_id"] == ret["reco_id"] == E2E_NORM + RET_SUFFIX
    assert reversal["event_type"] == "WERO_RVSL"
    assert reversal["external_ref"].startswith("WRVS:")
    assert Decimal(reversal["amount"]) + Decimal(ret["amount"]) == Decimal("0")


def test_reversal_statuses_are_matched_case_insensitively():
    cfg = load_config({"wero_reversal_statuses": ["reversed"]})
    entry = wero_row_to_entry(_wero_row(SettlementStatus="REVERSED"), cfg)
    assert entry["event_type"] == "WERO_RVSL"


def test_no_reversal_status_configured_means_no_reversal_leg():
    entry = wero_row_to_entry(_wero_row(SettlementStatus="REVERSED"), CFG)
    assert entry["event_type"] == "WERO"


# ---------------------------------------------------------------------------
# Entry shape / identity
# ---------------------------------------------------------------------------

_CONTRACT_KEYS = {
    "reco_id", "account", "currency", "amount", "value_date", "operation_date",
    "direction", "event_type", "external_ref", "transaction_particulars",
    "ref_no", "remarks_1", "transaction_id", "payload_raw",
}


@pytest.mark.parametrize("entry", [
    wero_row_to_entry(_wero_row(), CFG),
    payment_row_to_entry(_payment_row(), CFG),
    return_row_to_entry(_return_row(), CFG),
])
def test_entry_matches_the_batch_endpoint_contract(entry):
    assert set(entry) == _CONTRACT_KEYS
    # operation_date mirrors value_date: the backend hashes
    # `operation_date or value_date` date-only, so both must be stable.
    assert entry["operation_date"] == entry["value_date"]
    assert entry["account"] is None
    datetime.fromisoformat(entry["value_date"])


def test_external_ref_prefixes_keep_the_three_legs_distinct():
    refs = {
        wero_row_to_entry(_wero_row(), CFG)["external_ref"],
        payment_row_to_entry(_payment_row(), CFG)["external_ref"],
        return_row_to_entry(_return_row(), CFG)["external_ref"],
    }
    assert len(refs) == 3
    assert all(r.split(":", 1)[0] in {"WERO", "WRVS", "PAY", "RET"} for r in refs)


def test_payload_raw_is_json_safe():
    import json
    entry = wero_row_to_entry(_wero_row(), CFG)
    json.dumps(entry["payload_raw"])


def test_row_without_reference_is_pushed_with_a_null_reco_id():
    # Excluded from auto-matching, but visible as pending in the operational
    # view — dropping it would hide it.
    entry = wero_row_to_entry(_wero_row(OriginatorReference=None), CFG)
    assert entry["reco_id"] is None
    assert entry["external_ref"].startswith("WERO:")


def test_row_without_any_identity_is_a_row_error():
    with pytest.raises(ValueError):
        wero_row_to_entry(
            _wero_row(OriginatorReference=None, CaptureIDMoneyTransferID=None), CFG
        )


def test_row_without_any_usable_date_is_a_row_error():
    with pytest.raises(ValueError):
        wero_row_to_entry(
            _wero_row(SettlementRelatedTimestamp="", StartDate=None), CFG
        )


def test_wero_date_falls_back_to_the_watermark_column():
    entry = wero_row_to_entry(_wero_row(SettlementRelatedTimestamp=None), CFG)
    assert entry["value_date"].startswith("2026-08-20")


def test_payment_currency_column_is_used_when_configured():
    cfg = load_config({"payment_currency_column": "SettlementCurrency"})
    entry = payment_row_to_entry(_payment_row(ccy="USD"), cfg)
    assert entry["currency"] == "USD"
    assert "p.SettlementCurrency AS ccy" in build_payment_query(cfg)


# ---------------------------------------------------------------------------
# payment-status rows
# ---------------------------------------------------------------------------

def test_payment_status_row_carries_the_group_and_the_payment():
    row = _payment_row()
    entry = payment_row_to_entry(row, CFG)
    ps = payment_status_row(entry, row)
    assert ps == {
        "reco_id": E2E_NORM,
        "po_id": "PO123456",
        "status": "PDNG",
        "amount": "125.00",
        "payment_timestamp": "2026-08-20T11:05:00",
    }
    # naive by contract — the whole chain keeps the datamart wall clock
    assert "+" not in ps["payment_timestamp"]


def test_payment_status_row_is_skipped_without_a_reco_id():
    row = _payment_row(e2e_ref=None)
    entry = payment_row_to_entry(row, CFG)
    assert payment_status_row(entry, row) is None


# ---------------------------------------------------------------------------
# Config guard + SQL construction
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", [
    "wero_table", "wero_ref_column", "payment_e2e_column", "payment_date_column",
])
def test_injection_in_an_identifier_is_refused(key):
    with pytest.raises(WeroConfigError):
        load_config({key: "Amount; DROP TABLE reco.flow --"})


def test_schema_qualified_table_is_accepted():
    assert load_config({"wero_table": "wero.RECONCILIATION"})["wero_table"] == "wero.RECONCILIATION"


def test_required_identifier_cannot_be_blanked():
    with pytest.raises(WeroConfigError):
        load_config({"payment_e2e_column": ""})


def test_init_modules_cannot_be_empty():
    with pytest.raises(WeroConfigError):
        load_config({"payment_init_modules": []})


def test_unknown_config_keys_are_ignored():
    assert "nope" not in load_config({"nope": "x"})


def test_queries_bind_exactly_the_parameters_they_are_called_with():
    cfg = load_config(None)
    modules = cfg["payment_init_modules"]
    # _ingest_source calls them with [since] / [*modules, since]
    assert build_wero_query(cfg).count("?") == 1
    assert build_payment_query(cfg).count("?") == len(modules) + 1
    assert build_return_query(cfg).count("?") == len(modules) + 1


def test_return_query_brackets_the_reserved_word_and_joins_the_original_payment():
    sql = build_return_query(load_config(None))
    assert "std.[Return]" in sql
    assert "p.PaymentNumber = r.OriginalPo" in sql


def test_wero_watermark_is_not_the_varchar_business_date():
    # Comparing the varchar timestamp to a datetime parameter would force an
    # implicit conversion: no index, and a blow-up on a malformed value.
    cfg = load_config(None)
    assert cfg["wero_watermark_column"] != cfg["wero_date_column"]
    assert f"{cfg['wero_watermark_column']} >= ?" in build_wero_query(cfg)


def test_return_date_column_drives_both_the_filter_and_the_entry_date():
    cfg = load_config({"return_date_column": "ReturnDate"})
    sql = build_return_query(cfg)
    assert "r.ReturnDate AS return_date" in sql
    assert "AND r.ReturnDate >= ?" in sql


# ---------------------------------------------------------------------------
# SettlementStatus whitelist — neutral until production has been measured
# ---------------------------------------------------------------------------

def test_no_whitelist_means_no_filter_at_all():
    # The non-regression that matters: the shipped default must not change the
    # query or its parameters in any way.
    cfg = load_config(None)
    assert wero_status_params(cfg) == []
    sql = build_wero_query(cfg)
    assert "IN (" not in sql
    assert sql.count("?") == 1  # the watermark alone


def test_whitelist_binds_its_values_in_sql_order():
    cfg = load_config({"wero_settlement_statuses": ["Settled", "Accepted"]})
    sql, params = build_wero_query(cfg), wero_status_params(cfg)
    # _ingest_source calls it with [since, *params]: the watermark placeholder
    # comes first, so the count must be exactly len(params) + 1.
    assert sql.count("?") == len(params) + 1
    assert "UPPER(SettlementStatus) IN (?,?)" in sql


def test_whitelist_is_case_insensitive_and_deterministic():
    a = wero_status_params(load_config({"wero_settlement_statuses": ["settled", "ACCEPTED"]}))
    b = wero_status_params(load_config({"wero_settlement_statuses": ["Accepted", "Settled"]}))
    assert a == b == ["ACCEPTED", "SETTLED"]  # sorted, so SQL and params never drift


def test_whitelist_is_ignored_without_a_status_column():
    cfg = load_config({"wero_settlement_statuses": ["Settled"], "wero_status_column": None})
    assert wero_status_params(cfg) == []
    assert "IN (" not in build_wero_query(cfg)


def test_blank_statuses_are_dropped():
    cfg = load_config({"wero_settlement_statuses": ["Settled", "", "   ", None]})
    assert wero_status_params(cfg) == ["SETTLED"]


def test_no_reversal_status_exists_in_the_datamart():
    # Confirmed 2026-08-26: SettlementStatus only holds Accepted / Failed /
    # Rejected / Settled. The default must therefore emit no reversal leg.
    assert DEFAULT_CONFIG["wero_reversal_statuses"] == []
    entry = wero_row_to_entry(_wero_row(SettlementStatus="Rejected"), CFG)
    assert entry["event_type"] == "WERO"
    assert not entry["reco_id"].endswith(RET_SUFFIX)


# ---------------------------------------------------------------------------
# Columns settled against the real datamart (2026-08-26)
# ---------------------------------------------------------------------------

def test_return_shows_its_own_status_not_the_original_payment_s():
    assert DEFAULT_CONFIG["return_status_column"] == "Status"
    assert "LTRIM(RTRIM(r.Status))" in build_return_query(load_config(None))


def test_return_reason_code_rides_along_for_the_operator():
    assert "r.ReturnReasonCode" in build_return_query(load_config(None))


def test_match_key_columns_stay_on_their_confirmed_fallback():
    # These three columns EXIST but are not known to be POPULATED, and each one
    # feeds the match key or the amount — a wrong value breaks matching
    # silently. They stay None until diagnostics query D answers.
    for key in ("payment_currency_column", "return_amount_column", "return_date_column"):
        assert DEFAULT_CONFIG[key] is None, key


def test_seed_config_and_dag_defaults_stay_in_lockstep():
    """seed_flows.WERO_PARSER_CONFIG materialises the DAG defaults for the UI.

    Read by AST, not imported: this test file must stay free of app.* imports
    (app.main connects to Postgres at import time).
    """
    import ast

    seed_flows = Path(__file__).resolve().parents[1] / "app" / "db" / "seed_flows.py"
    tree = ast.parse(seed_flows.read_text(encoding="utf-8"))
    config = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", "") == "WERO_PARSER_CONFIG"
    )
    assert config == DEFAULT_CONFIG
