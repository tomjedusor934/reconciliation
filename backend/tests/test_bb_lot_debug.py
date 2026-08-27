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
    BucketKey,
    bucket_debug_report,
    parse_trace_keys,
    trace_key_reports,
)


def member(lot_id, movement_type, amount, keys, value_date="2026-07-01T00:00:00",
           claim=None):
    """Member dict in the exact _build_member shape (claim set ⇒ ghost)."""
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
        "claim_key_type": claim[0] if claim else None,
        "claim_key_value": claim[1] if claim else None,
        "payment_count": None,
        "keys": [{"key_type": kt, "key_value": kv} for kt, kv in keys],
    }


def group(name, parents_amounts, children_amounts):
    """Claim-group payload in the exact plan_claim_group shape (the fields the
    report reads)."""
    return {
        "claim_key_type": "MSGID",
        "claim_key_value": name,
        "parents": [
            {"external_ref": f"{name}-p{i}", "amount": a,
             "payment_amount": pa}
            for i, (a, pa) in enumerate(parents_amounts)
        ],
        "children": [{"external_ref": f"KEY:{name}~{i}", "amount": a}
                     for i, a in enumerate(children_amounts)],
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
    assert report["single_member_buckets"] == 1  # "small"
    assert report["single_member_ghost_buckets"] == 0

    big = report["biggest_lot"]
    assert big["lot_id"] == "big"
    assert big["bucket"] == "PAIR:P1|A"
    assert big["members"] == 5
    assert big["by_type"] == {"NDGB": 4, "SCTXB": 1}
    assert big["amount_by_type"] == {"NDGB": "40.00", "SCTXB": "-40.00"}
    assert big["net_amount"] == "0.00"
    assert big["distinct_keys_by_type"] == {"MSGID": 4, "PACS008": 1}


def test_single_ghost_buckets_are_counted_apart():
    """The over-fetch signature: thousands of buckets fed exactly one GHOST —
    payments claimed by a key whose counterpart can never arrive."""
    members = [
        member("g1", "NDGB", "10.00", [], claim=("MSGID", "LUXEMBOURG")),
        member("g2", "NDGB", "20.00", [], claim=("MSGID", "LUXEMBOURG")),
        member("r1", "SCTXB", "-5.00", []),
        member("pair", "NDGB", "1.00", [], claim=("MSGID", "LUXEMBOURG")),
        member("pair", "SCTXB", "-1.00", []),
    ]
    report = bucket_debug_report(members, {}, [])
    assert report["single_member_buckets"] == 3       # g1, g2, r1
    assert report["single_member_ghost_buckets"] == 2  # g1, g2


def test_report_counts_ghosts_and_flags_a_fully_synthetic_bucket():
    """The number to watch after a deploy: a bucket where both sides are ghosts
    nets to zero by construction and proves nothing on its own."""
    members = [
        member("ghosty", "SCTXB", "-10.00", [("PACS008", "P1")], claim=("PACS008", "P1")),
        member("ghosty", "NDGB", "10.00", [("PACS008", "P1")], claim=("MSGID", "M1")),
    ]
    report = bucket_debug_report(members, buckets_for(("ghosty", "P1", "M1")), [])
    assert report["ghost_members_total"] == 2
    assert report["biggest_lot"]["ghost_members"] == 2
    assert report["biggest_lot"]["synthetic_only"] is True

    members.append(member("ghosty", "NDRJ", "0.00", [("PACS008", "P1")]))
    report = bucket_debug_report(members, buckets_for(("ghosty", "P1", "M1")), [])
    assert report["biggest_lot"]["synthetic_only"] is False


def test_report_counts_the_parents_std_payment_disagrees_with():
    groups = [group("K1", [("-1000.00", "-1000.00"), ("-1000.00", "-990.00")],
                    ["-2000.00"])]
    report = bucket_debug_report([], {}, groups)
    assert report["split_parents"] == 2
    assert report["claim_groups"] == 1
    assert report["parents_with_payment_gap"] == 1


def test_report_surfaces_shared_keys_and_group_deltas():
    """The two numbers that expose the prod cases at a glance: 184 movements
    claiming one payment group, and a group whose ghosts do not add up to its
    movements (the second reconciliation will tag its lots)."""
    groups = [
        group("PTEL", [("-1", "-1")] * 184, ["-183.50"]),  # delta -0.50
        group("CLEAN", [("-10", "-10")], ["-10"]),          # delta 0
    ]
    report = bucket_debug_report([], {}, groups)
    assert report["groups_sharing_a_key"] == 1
    assert report["max_shared_key"] == 184
    assert report["groups_with_delta"] == 1
    top = report["top_group_deltas"][0]
    assert top["claim"] == "MSGID:PTEL"
    assert top["parents"] == 184
    assert top["delta"] == "-0.50"


def test_report_survives_a_run_with_only_groups():
    report = bucket_debug_report([], {}, [group("K1", [("-1", "-1")], ["-1"])])
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
