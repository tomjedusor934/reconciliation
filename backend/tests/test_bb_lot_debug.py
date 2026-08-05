"""Unit tests for the end-of-run BB bucket debug report (pure functions).

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
    BUCKET_PAIR,
    BUCKET_RESIDUAL,
    BucketKey,
    bucket_debug_report,
    parse_trace_keys,
    trace_key_reports,
)


def member(lot_id, movement_type, amount, keys, value_date="2026-07-01T00:00:00",
           split_parent=None):
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
        "split_parent_external_ref": split_parent,
        "payment_count": None,
        "keys": [{"key_type": kt, "key_value": kv} for kt, kv in keys],
    }


def buckets_for(*pairs):
    """(lot_id, pacs, msgid) → the bucket map the report annotates itself with."""
    return {
        lot_id: BucketKey(BUCKET_PAIR, pacs=pacs, msgid=msgid)
        for lot_id, pacs, msgid in pairs
    }


def test_empty_run_returns_none():
    assert bucket_debug_report([], {}, []) is None


def test_biggest_bucket_selected_with_type_and_amount_breakdown():
    members = [
        member("big", "NDGB", "10.00", [("MSGID", f"M{i}"), ("PACS008", "P1")])
        for i in range(4)
    ] + [
        member("big", "SCTXB", "-40.00", [("PACS008", "P1")]),
        member("small", "SWIFT", "5.00", [("PO", "PO1")]),
    ]
    report = bucket_debug_report(members, buckets_for(("big", "P1", "A")), [])
    assert report["lots_total"] == 2
    assert report["members_total"] == 6
    assert report["top_lots"][0] == {"lot_id": "big", "members": 5}
    assert report["bucket_kinds"] == {BUCKET_PAIR: 1}

    big = report["biggest_lot"]
    assert big["lot_id"] == "big"
    assert big["bucket"] == "PAIR:P1|A"
    assert big["members"] == 5
    assert big["by_type"] == {"NDGB": 4, "SCTXB": 1}
    assert big["amount_by_type"] == {"NDGB": "40.00", "SCTXB": "-40.00"}
    assert big["net_amount"] == "0.00"
    assert big["distinct_keys_by_type"] == {"MSGID": 4, "PACS008": 1}


def test_report_counts_ghosts_and_flags_a_fully_synthetic_bucket():
    """The number to watch after a deploy: a bucket where both sides are ghosts
    nets to zero by construction and proves nothing on its own."""
    members = [
        member("ghosty", "SCTXB", "-10.00", [("PACS008", "P1")], split_parent="A"),
        member("ghosty", "NDGB", "10.00", [("PACS008", "P1")], split_parent="B"),
    ]
    report = bucket_debug_report(members, buckets_for(("ghosty", "P1", "M1")), [])
    assert report["ghost_members_total"] == 2
    assert report["biggest_lot"]["ghost_members"] == 2
    assert report["biggest_lot"]["synthetic_only"] is True

    members.append(member("ghosty", "NDRJ", "0.00", [("PACS008", "P1")]))
    report = bucket_debug_report(members, buckets_for(("ghosty", "P1", "M1")), [])
    assert report["biggest_lot"]["synthetic_only"] is False


def test_report_counts_the_parents_std_payment_disagrees_with():
    parents = [
        {"external_ref": "A", "amount": "-1000.00", "payment_amount": "-1000.00"},
        {"external_ref": "B", "amount": "-1000.00", "payment_amount": "-990.00"},
    ]
    report = bucket_debug_report([], {}, parents)
    assert report["split_parents"] == 2
    assert report["parents_with_payment_gap"] == 1


def test_report_surfaces_shared_aggregate_keys():
    """The number that would have exposed the prod case at a glance: 184
    movements claiming one payment group."""
    parents = [
        {"external_ref": "A", "amount": "-1", "payment_amount": "-1"},
        {"external_ref": "B", "amount": "-1", "payment_amount": "-1",
         "shared_key_movements": 184},
    ]
    report = bucket_debug_report([], {}, parents)
    assert report["parents_sharing_a_key"] == 1
    assert report["max_shared_key"] == 184


def test_report_survives_a_run_with_only_parents():
    report = bucket_debug_report(
        [], {}, [{"external_ref": "A", "amount": "-1", "payment_amount": "-1"}]
    )
    assert report["biggest_lot"] is None
    json.dumps(report, default=str)  # must not raise


def test_top_keys_by_degree_exposes_the_shared_key():
    members = [
        member("big", "NDGB", "1.00", [("MSGID", f"M{i}"), ("PACS008", "P1")])
        for i in range(6)
    ]
    report = bucket_debug_report(members, buckets_for(("big", "P1", "M0")), [], top_keys=3)
    big = report["biggest_lot"]
    assert big["top_keys_by_degree"][0] == {
        "key": "PACS008:P1",
        "members": 6,
        "by_type": {"NDGB": 6},
    }
    assert len(big["top_keys_by_degree"]) == 3


def test_sample_members_keep_pushed_shape_and_truncate_keys():
    many_keys = [("MSGID", f"M{i:05d}") for i in range(30)]
    members = [member("big", "SCTXB", "-1.00", many_keys)]
    report = bucket_debug_report(
        members, buckets_for(("big", "P1", "M0")), [], sample=5, sample_keys=20
    )
    sample = report["biggest_lot"]["sample_members"][0]
    assert sample["movement_type"] == "SCTXB"
    assert sample["transaction_particulars"] == "SCTXB/I/x"
    assert len(sample["keys"]) == 20
    assert sample["keys_truncated"] == 10
    # The original buffer must not be mutated by sampling.
    assert len(members[0]["keys"]) == 30
    assert "keys_truncated" not in members[0]


def test_residual_buckets_show_up_in_the_kind_breakdown():
    members = [member("res", "SCTXB", "-10.00", [], split_parent="A")]
    buckets = {"res": BucketKey(BUCKET_RESIDUAL, ref="deadbeef")}
    report = bucket_debug_report(members, buckets, [])
    assert report["bucket_kinds"] == {BUCKET_RESIDUAL: 1}
    assert report["biggest_lot"]["bucket"] == "RESIDUAL:deadbeef"


def test_parse_trace_keys_tolerates_spaces_and_colons_in_values():
    raw = " PACS008:26070617550300076 , MSGID:TUP-VBS-009-20260706-15:25:21 ,, bogus "
    assert parse_trace_keys(raw) == [
        ("PACS008", "26070617550300076"),
        ("MSGID", "TUP-VBS-009-20260706-15:25:21"),
    ]
    assert parse_trace_keys("") == []


def test_trace_key_reports_shows_every_bucket_a_key_spreads_over():
    """A label MessageID legitimately appears in several buckets now — seeing it
    spread instead of gluing one mega-lot is how you confirm the split works."""
    members = [
        member("lot1", "SCTXB", "-100.00", [("PACS008", "P1"), ("MSGID", "LUXEMBOURG")]),
        member("lot1", "NDGB", "100.00", [("PACS008", "P1"), ("MSGID", "LUXEMBOURG")]),
        member("lot2", "SCTXB", "-50.00", [("PACS008", "P2"), ("MSGID", "LUXEMBOURG")]),
    ]
    buckets = buckets_for(("lot1", "P1", "LUXEMBOURG"), ("lot2", "P2", "LUXEMBOURG"))
    reports = trace_key_reports(members, buckets, [("MSGID", "LUXEMBOURG")])
    assert len(reports) == 1
    rep = reports[0]
    assert rep["found"] is True
    assert rep["traced_key"] == "MSGID:LUXEMBOURG"
    assert rep["lots_carrying_key"] == 2
    # Deep dive lands on the biggest of them.
    assert rep["lot_id"] == "lot1"
    assert rep["members"] == 2
    assert {lot["bucket"] for lot in rep["lots"]} == {
        "PAIR:P1|LUXEMBOURG", "PAIR:P2|LUXEMBOURG",
    }
    json.dumps(reports, default=str)  # must not raise


def test_trace_key_reports_unknown_key():
    members = [member("lot1", "SCTXB", "-100.00", [("PACS008", "P1")])]
    reports = trace_key_reports(members, buckets_for(("lot1", "P1", "M1")), [("PO", "NOPE")])
    assert reports[0] == {
        "traced_key": "PO:NOPE",
        "found": False,
        "note": "no member pushed this run carries the key",
    }
