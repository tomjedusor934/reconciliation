"""Unit tests for the end-of-run BB lot debug report (pure function).

Imports shared/dags/reco_datamart_bb.py directly (module-level deps: stdlib +
requests via reco_common) — no DB, no pyodbc, no airflow, no app.* import.
"""
import json
import sys
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

from reco_datamart_bb import (  # noqa: E402
    ClusterPlan,
    lot_debug_report,
    parse_trace_keys,
    trace_key_reports,
)


def member(lot_id, movement_type, amount, keys, value_date="2026-07-01T00:00:00"):
    """Member dict in the exact _build_member shape."""
    return {
        "lot_id": lot_id,
        "movement_type": movement_type,
        "external_ref": None,
        "account": "001",
        "currency": "EUR",
        "amount": amount,
        "value_date": value_date,
        "operation_date": None,
        "direction": "credit",
        "transaction_particulars": f"{movement_type}/I/x",
        "ref_no": None,
        "remarks_1": None,
        "keys": [{"key_type": kt, "key_value": kv} for kt, kv in keys],
    }


def plan_for(new_lots, merges=None):
    return ClusterPlan(key_to_lot={}, new_lots=list(new_lots), merges=merges or [])


def test_empty_members_returns_none():
    assert lot_debug_report([], plan_for([])) is None


def test_biggest_lot_selected_with_type_and_amount_breakdown():
    members = [
        member("big", "NDGB", "10.00", [("MSGID", f"M{i}"), ("PACS008", "P1")])
        for i in range(4)
    ] + [
        member("big", "SCTXB", "-40.00", [("PACS008", "P1")]),
        member("small", "SWIFT", "5.00", [("PO", "PO1")]),
    ]
    report = lot_debug_report(members, plan_for(["big", "small"]))
    assert report["lots_total"] == 2
    assert report["members_total"] == 6
    assert report["top_lots"][0] == {"lot_id": "big", "members": 5}

    big = report["biggest_lot"]
    assert big["lot_id"] == "big"
    assert big["is_new"] is True
    assert big["members"] == 5
    assert big["by_type"] == {"NDGB": 4, "SCTXB": 1}
    assert big["amount_by_type"] == {"NDGB": "40.00", "SCTXB": "-40.00"}
    assert big["net_amount"] == "0.00"
    assert big["distinct_keys_by_type"] == {"MSGID": 4, "PACS008": 1}


def test_top_keys_by_degree_exposes_the_gluing_key():
    # PACS008:P1 carried by every member = the degenerate glue; each MSGID is
    # private (degree 1).
    members = [
        member("big", "NDGB", "1.00", [("MSGID", f"M{i}"), ("PACS008", "P1")])
        for i in range(6)
    ]
    report = lot_debug_report(members, plan_for(["big"]), top_keys=3)
    big = report["biggest_lot"]
    assert big["top_keys_by_degree"][0] == {
        "key": "PACS008:P1",
        "members": 6,
        "by_type": {"NDGB": 6},
    }
    assert len(big["top_keys_by_degree"]) == 3
    assert big["key_degree_histogram"] == {"1": 6, "3-10": 1}


def test_sample_members_keep_pushed_shape_and_truncate_keys():
    many_keys = [("MSGID", f"M{i:05d}") for i in range(30)]
    members = [member("big", "SCTXB", "-1.00", many_keys)]
    report = lot_debug_report(members, plan_for(["big"]), sample=5, sample_keys=20)
    sample = report["biggest_lot"]["sample_members"][0]
    assert sample["movement_type"] == "SCTXB"
    assert sample["transaction_particulars"] == "SCTXB/I/x"
    assert len(sample["keys"]) == 20
    assert sample["keys_truncated"] == 10
    # The original buffer must not be mutated by sampling.
    assert len(members[0]["keys"]) == 30
    assert "keys_truncated" not in members[0]


def test_merge_survivor_flag_and_json_serializable():
    members = [member("survivor", "NDGB", "1.00", [("MSGID", "M1")])]
    plan = plan_for(
        [], merges=[{"absorbed_lot_id": "old", "surviving_lot_id": "survivor"}]
    )
    report = lot_debug_report(members, plan)
    big = report["biggest_lot"]
    assert big["is_new"] is False
    assert big["merges_as_survivor"] == 1
    json.dumps(report, default=str)  # must not raise


def test_parse_trace_keys_tolerates_spaces_and_colons_in_values():
    raw = " PACS008:26070617550300076 , MSGID:TUP-VBS-009-20260706-15:25:21 ,, bogus "
    assert parse_trace_keys(raw) == [
        ("PACS008", "26070617550300076"),
        ("MSGID", "TUP-VBS-009-20260706-15:25:21"),
    ]
    assert parse_trace_keys("") == []


def trace_fixture():
    """Bulk + NDGB + return around PACS1, plus a BRIDGE NDGB carrying PACS1 and
    PACS2 (the chaining the trace must expose), all in lot 'big'."""
    plan = ClusterPlan(
        key_to_lot={("PACS008", "PACS1"): "big", ("PACS008", "PACS2"): "big"},
        new_lots=["big"],
        merges=[],
    )
    members = [
        member("big", "SCTXB", "-100.00", [("PACS008", "PACS1")]),
        member("big", "NDGB", "60.00", [("MSGID", "A1"), ("PACS008", "PACS1")]),
        member("big", "SCTXB", "40.00", [("PO", "PO1"), ("PACS008", "PACS1")]),
        member("big", "NDGB", "5.00",
               [("MSGID", "A2"), ("PACS008", "PACS1"), ("PACS008", "PACS2")]),
    ]
    members[3]["external_ref"] = "BRIDGE-REF"
    return members, plan


def test_trace_key_reports_finds_lot_carriers_and_bridges():
    members, plan = trace_fixture()
    reports = trace_key_reports(members, plan, [("PACS008", "PACS1")])
    assert len(reports) == 1
    rep = reports[0]
    assert rep["found"] is True
    assert rep["traced_key"] == "PACS008:PACS1"
    assert rep["lot_id"] == "big"
    assert rep["members"] == 4
    assert rep["by_type"] == {"SCTXB": 2, "NDGB": 2}
    assert rep["traced_key_carriers_by_type"] == {"SCTXB": 2, "NDGB": 2}
    assert rep["bridge_members_total"] == 1
    bridge = rep["bridge_members"][0]
    assert bridge["movement_type"] == "NDGB"
    assert bridge["external_ref"] == "BRIDGE-REF"
    assert bridge["pacs_count"] == 2
    assert bridge["pacs"] == ["PACS1", "PACS2"]
    json.dumps(reports, default=str)  # must not raise


def test_trace_key_reports_unknown_key_and_memberless_lot():
    members, plan = trace_fixture()
    plan.key_to_lot[("PO", "GHOST")] = "other-lot"  # known lot, no member pushed
    reports = trace_key_reports(
        members, plan, [("PO", "NOPE"), ("PO", "GHOST")]
    )
    assert reports[0] == {
        "traced_key": "PO:NOPE",
        "found": False,
        "note": "key unknown to this run's cluster plan",
    }
    assert reports[1]["found"] is False
    assert reports[1]["lot_id"] == "other-lot"
