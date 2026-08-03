"""Unit tests for the Batch Booking True clustering logic (pure functions).

Imports shared/dags/reco_datamart_bb.py directly (module-level deps: stdlib +
requests via reco_common) — no DB, no pyodbc, no airflow, no app.* import.
"""
import sys
import uuid as uuid_module
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
    KEY_MSGID,
    KEY_PACS008,
    KEY_PO,
    UNRESOLVED_RECO_ID,
    BBLookupInputs,
    ClusterPlan,
    bb_reco_for,
    build_clusters,
    classify_bb_movement,
    collect_lookup_inputs,
    degenerate_msgids,
    movement_keys,
    ndrj_po_id,
    sp_return_po_id,
    strip_degenerate_msgids,
)


def raw_row(tp=None, ref_no=None, remarks_1=None):
    """std.Movement shape (datamart casing)."""
    return {
        "TransactionParticulars": tp,
        "PaymentOrderID_Ref": ref_no,
        "Remarks_1": remarks_1,
    }


def app_entry(tp=None, ref_no=None, remarks_1=None):
    """Backend entry shape (lowercase fields) — the /tasks/finacle/unresolved payload."""
    return {
        "transaction_particulars": tp,
        "ref_no": ref_no,
        "remarks_1": remarks_1,
    }


NO_MAPS = ({}, {}, {})


# ---------------------------------------------------------------------------
# classify / po-id extraction
# ---------------------------------------------------------------------------

def test_classify_bb_movement():
    assert classify_bb_movement(raw_row(tp="SCTXB/O/BLK1/x")) == "SCTXB"
    assert classify_bb_movement(raw_row(tp="SDDXB/I/x")) == "SDDXB"
    assert classify_bb_movement(raw_row(tp="SDXBB/O/x")) == "SDXBB"
    assert classify_bb_movement(raw_row(tp="NDGB/whatever")) == "NDGB"
    assert classify_bb_movement(raw_row(tp="NDRJ/reject")) == "NDRJ"
    assert classify_bb_movement(raw_row(tp="SWIFT/ref/name")) == "SWIFT"
    assert classify_bb_movement(raw_row(tp="BKRTP/ref/name")) == "BKRTP"
    assert classify_bb_movement(raw_row(tp="GLXYZ/other")) is None
    assert classify_bb_movement(raw_row(tp=None)) is None
    assert classify_bb_movement(raw_row(tp="   ")) is None


def test_ndrj_po_id_extraction():
    assert ndrj_po_id(raw_row(ref_no="paysis##PO123")) == "PO123"
    # segment after the LAST '##'
    assert ndrj_po_id(raw_row(ref_no="a##b##PO9")) == "PO9"
    # no '##' → the ref_no itself (defensive)
    assert ndrj_po_id(raw_row(ref_no="PO777")) == "PO777"
    assert ndrj_po_id(raw_row(ref_no="  ")) is None
    assert ndrj_po_id(raw_row(ref_no=None)) is None


def test_sp_return_po_id_ref_no_first_then_tp_fallback():
    # Primary: ref_no (per spec)
    assert sp_return_po_id(raw_row(tp="SCTXB/NCP/I/POTP/x", ref_no="POREF")) == "POREF"
    # Fallback: legacy TP segment[3]
    assert sp_return_po_id(raw_row(tp="SCTXB/NCC/O/POTP/x", ref_no=None)) == "POTP"
    assert sp_return_po_id(raw_row(tp="SCTXB/NCC", ref_no=None)) is None


# ---------------------------------------------------------------------------
# movement_keys
# ---------------------------------------------------------------------------

def test_sp_direct_keys_pacs_plus_message_ids():
    pacs_map = {"PACS1": ["AGG1", "AGG2"]}
    keys = movement_keys(raw_row(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map, {}, {})
    assert keys == frozenset(
        {(KEY_PACS008, "PACS1"), (KEY_MSGID, "AGG1"), (KEY_MSGID, "AGG2")}
    )


def test_sp_direct_without_remarks_is_transient():
    assert movement_keys(raw_row(tp="SCTXB/O/x", remarks_1=None), *NO_MAPS) == frozenset()
    # reversal hiding in the bulk flow: no remarks_1 either → transient too
    assert movement_keys(raw_row(tp="SCTXB/TRN123/NAME"), *NO_MAPS) == frozenset()


def test_sp_return_keys_po_plus_original_bulk_pacs_never_msgid():
    # Business rule: a return carries its own PO + the ORIGINAL bulk's PACS008
    # (it reconciles inside the bulk's lot with its NDGB) — but never the
    # payment's MessageID (pure glue, no link value).
    po_map = {"PO1": [("AGG1", "PACS1")]}
    keys = movement_keys(
        raw_row(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1"), {}, {}, po_map
    )
    assert keys == frozenset({(KEY_PO, "PO1"), (KEY_PACS008, "PACS1")})
    # payment not resolvable yet → PO alone (links later through the key map)
    keys = movement_keys(raw_row(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1"), {}, {}, {})
    assert keys == frozenset({(KEY_PO, "PO1")})


def test_sp_return_joins_its_original_bulk_lot():
    # Expected perimeter (validated on pacs 26070617550300076): the direct
    # bulk, its NDGB and the returns of its payments share ONE lot.
    pacs_map = {"PACS1": ["AGG1"]}
    msgid_map = {"AGG1": [("PACS1", "PO1")]}
    po_map = {"PO1": [("AGG1", "PACS1")]}
    bulk = movement_keys(raw_row(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map, {}, {})
    ndgb = movement_keys(raw_row(tp="NDGB/agg", remarks_1="AGG1"), {}, msgid_map, {})
    ret = movement_keys(raw_row(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1"), {}, {}, po_map)
    plan = build_clusters([bulk, ndgb, ret], {})
    assert len(plan.new_lots) == 1
    assert (
        bb_reco_for(bulk, plan.key_to_lot)
        == bb_reco_for(ndgb, plan.key_to_lot)
        == bb_reco_for(ret, plan.key_to_lot)
    )


def test_ndgb_keys_msgid_plus_pacs_or_po_fallback():
    msgid_map = {
        "AGG1": [("PACS1", "PO1"), ("PACS2", "PO2"), (None, "PO3")]  # PO3 = swift/bkrtp single
    }
    keys = movement_keys(raw_row(tp="NDGB/agg", remarks_1="AGG1"), {}, msgid_map, {})
    assert keys == frozenset(
        {
            (KEY_MSGID, "AGG1"),
            (KEY_PACS008, "PACS1"),
            (KEY_PACS008, "PACS2"),
            (KEY_PO, "PO3"),  # pacs empty → PaymentNumber fallback
        }
    )


def test_ndgb_without_remarks_is_transient():
    assert movement_keys(raw_row(tp="NDGB/agg", remarks_1=None), *NO_MAPS) == frozenset()


def test_ndrj_keys_with_and_without_payment():
    po_map = {"PO9": [("AGG7", "PACS7")]}
    keys = movement_keys(raw_row(tp="NDRJ/rej", ref_no="paysis##PO9"), {}, {}, po_map)
    assert keys == frozenset(
        {(KEY_PO, "PO9"), (KEY_MSGID, "AGG7"), (KEY_PACS008, "PACS7")}
    )
    # payment not in std.Payment yet → PO key alone still links to NDGB/SWIFT sides
    keys = movement_keys(raw_row(tp="NDRJ/rej", ref_no="paysis##PO9"), {}, {}, {})
    assert keys == frozenset({(KEY_PO, "PO9")})


def test_singles_keyed_by_ref_no_without_lookup():
    assert movement_keys(raw_row(tp="SWIFT/x/y", ref_no="R1"), *NO_MAPS) == frozenset(
        {(KEY_PO, "R1")}
    )
    assert movement_keys(raw_row(tp="BKRTP/x/y", ref_no="R2"), *NO_MAPS) == frozenset(
        {(KEY_PO, "R2")}
    )
    assert movement_keys(raw_row(tp="SWIFT/x/y", ref_no=None), *NO_MAPS) == frozenset()


def test_unknown_types_are_not_supported():
    assert movement_keys(raw_row(tp="GLXYZ/other"), *NO_MAPS) is None
    assert movement_keys(raw_row(tp=None), *NO_MAPS) is None


def test_double_hash_separated_particulars():
    # _tp_parts splits on '##' when present
    keys = movement_keys(raw_row(tp="SCTXB##O##x", remarks_1="PACS1"), {}, {}, {})
    assert keys == frozenset({(KEY_PACS008, "PACS1")})


def test_movement_keys_works_on_app_entry_shape():
    pacs_map = {"PACS1": ["AGG1"]}
    keys = movement_keys(app_entry(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map, {}, {})
    assert keys == frozenset({(KEY_PACS008, "PACS1"), (KEY_MSGID, "AGG1")})


# ---------------------------------------------------------------------------
# degenerate MessageID labels ('ESCH/ALZETTE', 'ADEM-VIR<ts>'…)
# ---------------------------------------------------------------------------

def test_degenerate_msgids_flags_labels_across_bulks_only():
    pacs_map = {
        "PACS1": ["M1", "LABEL"],
        "PACS2": ["M2", "LABEL"],
        "PACS3": ["LABEL"],
        "PACS4": ["LABEL"],
    }
    # msgid_map contributes a 5th pacs for LABEL and a singles-only batch id.
    msgid_map = {
        "LABEL": [("PACS5", None)],
        "BATCH-OF-SINGLES": [(None, "PO1"), (None, "PO2"), (None, "PO3"), (None, "PO4")],
        "M1": [("PACS1", None)],
    }
    bad = degenerate_msgids(pacs_map, msgid_map, max_pacs=3)
    assert bad == {"LABEL"}  # 5 distinct pacs > 3
    # A batch id shared by many SINGLES (no pacs) is legit NDGB settlement —
    # never flagged by the pacs-spread heuristic.
    assert "BATCH-OF-SINGLES" not in bad
    assert "M1" not in bad


def test_strip_degenerate_msgids_filters_all_three_maps():
    pacs_map = {"PACS1": ["M1", "LABEL"]}
    msgid_map = {"LABEL": [("PACS5", None)], "M1": [("PACS1", None)]}
    po_map = {"PO9": [("LABEL", "PACS7")]}
    p2, m2, po2 = strip_degenerate_msgids(pacs_map, msgid_map, po_map, {"LABEL"})
    assert p2 == {"PACS1": ["M1"]}
    assert m2 == {"M1": [("PACS1", None)]}
    assert po2 == {"PO9": [(None, "PACS7")]}  # pacs kept, label dropped


def test_ndgb_with_label_remarks_stays_transient():
    keys = movement_keys(
        raw_row(tp="NDGB/agg", remarks_1="ESCH/ALZETTE"),
        {}, {}, {},
        bad_msgids=frozenset({"ESCH/ALZETTE"}),
    )
    assert keys == frozenset()  # retried, never glued


def test_label_msgid_no_longer_chains_two_bulks():
    # Two bulks sharing only the label must land in two lots once the maps are
    # stripped (regression for the ESCH/ALZETTE-style cross-batch gluing).
    pacs_map = {"PACS1": ["M1", "LABEL"], "PACS2": ["M2", "LABEL"]}
    bad = degenerate_msgids(pacs_map, {}, max_pacs=1)
    assert bad == {"LABEL"}
    pacs_map, msgid_map, po_map = strip_degenerate_msgids(pacs_map, {}, {}, bad)
    b1 = movement_keys(raw_row(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map, msgid_map, po_map)
    b2 = movement_keys(raw_row(tp="SCTXB/O/y", remarks_1="PACS2"), pacs_map, msgid_map, po_map)
    plan = build_clusters([b1, b2], {})
    assert len(plan.new_lots) == 2
    assert bb_reco_for(b1, plan.key_to_lot) != bb_reco_for(b2, plan.key_to_lot)


# ---------------------------------------------------------------------------
# collect_lookup_inputs
# ---------------------------------------------------------------------------

def test_collect_lookup_inputs_both_shapes():
    acc = BBLookupInputs()
    collect_lookup_inputs(
        [
            raw_row(tp="SCTXB/O/x", remarks_1="PACS1"),
            raw_row(tp="SDDXB/NCC/O/POTP/x", ref_no="POREF"),  # SP return → po_ids
            raw_row(tp="NDGB/agg", remarks_1="AGG1"),
            raw_row(tp="NDRJ/rej", ref_no="paysis##PO9"),
            raw_row(tp="SWIFT/x", ref_no="R1"),  # no lookup input
            raw_row(tp="GL/other"),
        ],
        acc,
    )
    collect_lookup_inputs([app_entry(tp="SCTXB/I/x", remarks_1="PACS2")], acc)
    assert acc.sp_pacs008 == {"PACS1", "PACS2"}
    assert acc.ndgb_msgids == {"AGG1"}
    assert acc.po_ids == {"POREF", "PO9"}  # NDRJ + SP returns


# ---------------------------------------------------------------------------
# build_clusters / bb_reco_for
# ---------------------------------------------------------------------------

def _keys_of(*pairs):
    return frozenset(pairs)


def test_interleaved_bulks_form_one_lot():
    # NDGB AGG1 settles payments from two SP bulks (PACS1, PACS2):
    # SP1 {PACS1, AGG1} / SP2 {PACS2, AGG1} / NDGB {AGG1, PACS1, PACS2}
    plan = build_clusters(
        [
            _keys_of((KEY_PACS008, "PACS1"), (KEY_MSGID, "AGG1")),
            _keys_of((KEY_PACS008, "PACS2"), (KEY_MSGID, "AGG1")),
            _keys_of((KEY_MSGID, "AGG1"), (KEY_PACS008, "PACS1"), (KEY_PACS008, "PACS2")),
        ],
        existing={},
    )
    lots = {plan.key_to_lot[k] for k in plan.key_to_lot}
    assert len(lots) == 1
    assert len(plan.new_lots) == 1
    assert plan.merges == []


def test_disjoint_movements_form_separate_lots():
    plan = build_clusters(
        [
            _keys_of((KEY_PACS008, "PACS1")),
            _keys_of((KEY_PACS008, "PACS2")),
        ],
        existing={},
    )
    assert len(plan.new_lots) == 2
    assert plan.key_to_lot[(KEY_PACS008, "PACS1")] != plan.key_to_lot[(KEY_PACS008, "PACS2")]


def test_bridge_merges_existing_lots_oldest_survives():
    existing = {
        (KEY_PACS008, "PACS1"): ("LOT-B", "2026-07-02T00:00:00"),
        (KEY_PACS008, "PACS2"): ("LOT-A", "2026-07-01T00:00:00"),
    }
    # An NDGB bridging both pacs008 → single component → oldest lot (LOT-A) survives
    plan = build_clusters(
        [_keys_of((KEY_MSGID, "AGG1"), (KEY_PACS008, "PACS1"), (KEY_PACS008, "PACS2"))],
        existing=existing,
    )
    assert plan.new_lots == []
    assert plan.merges == [{"absorbed_lot_id": "LOT-B", "surviving_lot_id": "LOT-A"}]
    assert plan.key_to_lot[(KEY_PACS008, "PACS1")] == "LOT-A"
    assert plan.key_to_lot[(KEY_PACS008, "PACS2")] == "LOT-A"
    assert plan.key_to_lot[(KEY_MSGID, "AGG1")] == "LOT-A"


def test_existing_lot_keys_stay_together_without_new_bridge():
    # Keys pre-linked to the same lot must share a component through the
    # synthetic anchor even when no new movement carries both.
    existing = {
        (KEY_PACS008, "PACS1"): ("LOT-A", "2026-07-01T00:00:00"),
        (KEY_MSGID, "AGG1"): ("LOT-A", "2026-07-01T00:00:00"),
    }
    plan = build_clusters([_keys_of((KEY_MSGID, "AGG1"))], existing=existing)
    assert plan.new_lots == [] and plan.merges == []
    assert plan.key_to_lot[(KEY_PACS008, "PACS1")] == "LOT-A"
    assert plan.key_to_lot[(KEY_MSGID, "AGG1")] == "LOT-A"


def test_reject_joins_lot_via_payment_message_id():
    existing = {(KEY_MSGID, "AGG1"): ("LOT-A", "2026-07-01T00:00:00")}
    # NDRJ resolved to {PO9, AGG1} → joins LOT-A
    plan = build_clusters(
        [_keys_of((KEY_PO, "PO9"), (KEY_MSGID, "AGG1"))], existing=existing
    )
    assert plan.new_lots == [] and plan.merges == []
    assert plan.key_to_lot[(KEY_PO, "PO9")] == "LOT-A"


def test_swift_links_to_ndgb_through_po_key():
    plan = build_clusters(
        [
            _keys_of((KEY_PO, "R1")),                       # SWIFT single (ref_no)
            _keys_of((KEY_MSGID, "AGG1"), (KEY_PO, "R1")),  # NDGB with empty-pacs payment R1
        ],
        existing={},
    )
    assert len(plan.new_lots) == 1
    assert plan.key_to_lot[(KEY_PO, "R1")] == plan.key_to_lot[(KEY_MSGID, "AGG1")]


def test_build_clusters_is_deterministic(monkeypatch):
    counter = {"n": 0}

    def fake_uuid4():
        counter["n"] += 1
        return f"uuid-{counter['n']:04d}"

    monkeypatch.setattr(uuid_module, "uuid4", fake_uuid4)
    key_sets = [
        _keys_of((KEY_PACS008, "PACS2")),
        _keys_of((KEY_PACS008, "PACS1"), (KEY_MSGID, "AGG1")),
        _keys_of((KEY_PO, "R1")),
    ]
    counter["n"] = 0
    plan_a = build_clusters(list(key_sets), existing={})
    counter["n"] = 0
    plan_b = build_clusters(list(reversed(key_sets)), existing={})
    assert plan_a.key_to_lot == plan_b.key_to_lot
    assert plan_a.new_lots == plan_b.new_lots
    assert plan_a.merges == plan_b.merges


def test_bb_reco_for():
    key_to_lot = {(KEY_PACS008, "PACS1"): "LOT-A"}
    assert bb_reco_for(None, key_to_lot) == UNRESOLVED_RECO_ID  # non-BB type
    assert bb_reco_for(frozenset(), key_to_lot) is None          # transient
    assert bb_reco_for(_keys_of((KEY_PACS008, "PACS1")), key_to_lot) == "LOT-A"
    # key unknown to the plan (row appeared between the two passes) → transient
    assert bb_reco_for(_keys_of((KEY_PACS008, "NEW")), key_to_lot) is None


def test_cluster_plan_shape():
    plan = build_clusters([], existing={})
    assert isinstance(plan, ClusterPlan)
    assert plan.key_to_lot == {} and plan.new_lots == [] and plan.merges == []
