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


# ---------------------------------------------------------------------------
# Bulk-booked instant payments (multiline & co): the aggregate carries no ref_no
# and reaches its member singles through the MessageID fan-out.
# Validated on prod TransactionID PF0043409 / MessageID MULTI.116072584.
# ---------------------------------------------------------------------------

# The bulk's payments have an EMPTY MessageIDPACS008 (they are instant singles),
# so the fan-out yields PO keys — the very keys the member movements carry.
MULTI_MSGID_MAP = {"MULTI1": [(None, "PO1"), (None, "PO2")]}
MULTI_TXNREF_MAP = {"REF1": "MULTI1"}


def test_ip_aggregate_without_ref_no_fans_out_to_its_payments():
    keys = movement_keys(
        raw_row(tp="SCRT1/REF1/SANJEE ROLLES"),
        {}, MULTI_MSGID_MAP, {},
        txnref_map=MULTI_TXNREF_MAP,
    )
    assert keys == frozenset({(KEY_MSGID, "MULTI1"), (KEY_PO, "PO1"), (KEY_PO, "PO2")})


def test_ip_aggregate_joins_its_member_singles_in_one_lot():
    # The prod shape: one aggregate credit + two BKRTP debits on the same account.
    agg = movement_keys(
        raw_row(tp="SCRT1/REF1/SANJEE ROLLES"),
        {}, MULTI_MSGID_MAP, {}, txnref_map=MULTI_TXNREF_MAP,
    )
    m1 = movement_keys(raw_row(tp="BKRTP/NCP/O/PO1/CENTRE", ref_no="PO1"), *NO_MAPS)
    m2 = movement_keys(raw_row(tp="BKRTP/NCP/O/PO2/CENTRE", ref_no="PO2"), *NO_MAPS)
    assert m1 == frozenset({(KEY_PO, "PO1")})  # members unchanged: PO key only
    plan = build_clusters([agg, m1, m2], {})
    assert len(plan.new_lots) == 1
    assert (
        bb_reco_for(agg, plan.key_to_lot)
        == bb_reco_for(m1, plan.key_to_lot)
        == bb_reco_for(m2, plan.key_to_lot)
    )


def test_scrt1_with_a_ref_no_is_a_single():
    # New: SCRT1 used to fall through to 'Not Supported' (None) entirely.
    assert movement_keys(raw_row(tp="SCRT1/O/REF1", ref_no="PO9"), *NO_MAPS) == frozenset(
        {(KEY_PO, "PO9")}
    )
    assert classify_bb_movement(raw_row(tp="SCRT1/O/REF1")) == "SCRT1"


def test_ip_aggregate_misses_stay_transient_never_not_supported():
    # payment not in std.Payment yet → empty set (retried), NOT the sentinel
    assert movement_keys(raw_row(tp="SCRT1/REF404/x"), *NO_MAPS) == frozenset()
    # MessageID resolved but no payment fanned out → the MSGID key alone still
    # identifies the group (same behaviour as the NDGB branch)
    assert movement_keys(
        raw_row(tp="SCRT1/REF1/x"), {}, {}, {}, txnref_map=MULTI_TXNREF_MAP
    ) == frozenset({(KEY_MSGID, "MULTI1")})


def test_bulk_return_shape_never_goes_to_the_reversal_lookup():
    # BKRTP wearing the NCC/NCP shape is keyed by ref_no, never by TP segment[1]
    # ('NCP' is not a TransactionRef) — the exclusion comes from reversal_ref_for.
    assert movement_keys(
        raw_row(tp="BKRTP/NCP/O/PO1/CENTRE"), {}, MULTI_MSGID_MAP, {},
        txnref_map={"NCP": "MULTI1"},
    ) == frozenset()


def test_pacs008_wins_over_po_in_the_fan_out():
    # A bulk whose payments DO carry a pacs008 links through PACS008, like NDGB.
    keys = movement_keys(
        raw_row(tp="SWIFT/REF1/x"), {}, {"MULTI1": [("PACS1", "PO1")]}, {},
        txnref_map=MULTI_TXNREF_MAP,
    )
    assert keys == frozenset({(KEY_MSGID, "MULTI1"), (KEY_PACS008, "PACS1")})


def test_collect_lookup_inputs_instant_txn_refs():
    acc = BBLookupInputs()
    collect_lookup_inputs(
        [
            raw_row(tp="SCRT1/REF1/SANJEE ROLLES"),          # aggregate → collected
            raw_row(tp="SWIFT/REF2/x"),                      # aggregate → collected
            raw_row(tp="SCRT1/O/REF3", ref_no="PO9"),        # single → NOT collected
            raw_row(tp="BKRTP/NCP/O/PO1/x", ref_no="PO1"),   # return shape → NOT collected
            app_entry(tp="SCRT1/REF4/x"),                    # app entry shape works too
        ],
        acc,
    )
    assert acc.instant_txn_refs == {"REF1", "REF2", "REF4"}
    assert acc.po_ids == set()  # singles need no lookup


def test_double_hash_separated_particulars():
    # _tp_parts splits on '##' when present
    keys = movement_keys(raw_row(tp="SCTXB##O##x", remarks_1="PACS1"), {}, {}, {})
    assert keys == frozenset({(KEY_PACS008, "PACS1")})


def test_movement_keys_works_on_app_entry_shape():
    pacs_map = {"PACS1": ["AGG1"]}
    keys = movement_keys(app_entry(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map, {}, {})
    assert keys == frozenset({(KEY_PACS008, "PACS1"), (KEY_MSGID, "AGG1")})


# ---------------------------------------------------------------------------
# degenerate MessageID labels — LOG-ONLY detection ('LUXEMBOURG', 'ESCH/ALZETTE'…)
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


def test_label_msgids_are_kept_as_keys():
    # LOG-ONLY: a label like 'LUXEMBOURG' still keys (business links go
    # through it — e.g. a 700M NDGB) even though degenerate_msgids flags it.
    pacs_map = {"PACS1": ["LUXEMBOURG"], "PACS2": ["LUXEMBOURG"]}
    assert degenerate_msgids(pacs_map, {}, max_pacs=1) == {"LUXEMBOURG"}
    b1 = movement_keys(raw_row(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map, {}, {})
    assert (KEY_MSGID, "LUXEMBOURG") in b1
    ndgb = movement_keys(raw_row(tp="NDGB/agg", remarks_1="LUXEMBOURG"), {}, {}, {})
    assert ndgb == frozenset({(KEY_MSGID, "LUXEMBOURG")})
    plan = build_clusters([b1, ndgb], {})
    assert bb_reco_for(b1, plan.key_to_lot) == bb_reco_for(ndgb, plan.key_to_lot)


# ---------------------------------------------------------------------------
# NDRT / PREFIX/RCC rejects (std.[Return] → original payment)
# ---------------------------------------------------------------------------

RETURN_MAP = {"RET1": [("ORIG1", "AGG1", "PACS1")]}


def test_classify_ndrt():
    assert classify_bb_movement(raw_row(tp="NDRT##1000##2000")) == "NDRT"


def test_ndrt_and_rcc_keys_full_resolution():
    expected = frozenset({
        (KEY_PO, "RET1"),        # pairs the NDRT↔RCC legs of the same return
        (KEY_PO, "ORIG1"),       # links the original single movement
        (KEY_MSGID, "AGG1"),     # original payment links
        (KEY_PACS008, "PACS1"),
    })
    ndrt = movement_keys(raw_row(tp="NDRT##1000##2000", ref_no="RET1"), {}, {}, {}, RETURN_MAP)
    assert ndrt == expected
    rcc = movement_keys(raw_row(tp="SCTXB/RCC/O/RET1/x", ref_no="RET1"), {}, {}, {}, RETURN_MAP)
    assert rcc == expected


def test_ndrt_rcc_unresolved_return_keeps_own_po():
    # std.[Return] row not found yet → own PO only (links later via key map);
    # original payment missing (LEFT JOIN) → OriginalPo still keys.
    assert movement_keys(
        raw_row(tp="NDRT##x", ref_no="RET9"), {}, {}, {}, {}
    ) == frozenset({(KEY_PO, "RET9")})
    partial = {"RET9": [("ORIG9", None, None)]}
    assert movement_keys(
        raw_row(tp="SDDXB/RCC/I/RET9/x", ref_no="RET9"), {}, {}, {}, partial
    ) == frozenset({(KEY_PO, "RET9"), (KEY_PO, "ORIG9")})
    # rcc PO falls back to TP segment[3] when ref_no is empty
    assert movement_keys(
        raw_row(tp="SCTXB/RCC/I/RETTP/x", ref_no=None), {}, {}, {}, {}
    ) == frozenset({(KEY_PO, "RETTP")})
    # no PO at all → transient
    assert movement_keys(raw_row(tp="NDRT##x", ref_no=None), {}, {}, {}, {}) == frozenset()


def test_ndrt_joins_original_bulk_lot_and_pairs_with_rcc():
    pacs_map = {"PACS1": ["AGG1"]}
    bulk = movement_keys(raw_row(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map, {}, {}, {})
    ndrt = movement_keys(raw_row(tp="NDRT##a", ref_no="RET1"), {}, {}, {}, RETURN_MAP)
    rcc = movement_keys(raw_row(tp="SDXBB/RCC/O/RET1/x", ref_no="RET1"), {}, {}, {}, RETURN_MAP)
    plan = build_clusters([bulk, ndrt, rcc], {})
    assert len(plan.new_lots) == 1
    assert (
        bb_reco_for(bulk, plan.key_to_lot)
        == bb_reco_for(ndrt, plan.key_to_lot)
        == bb_reco_for(rcc, plan.key_to_lot)
    )


def test_collect_lookup_inputs_return_pos():
    acc = BBLookupInputs()
    collect_lookup_inputs(
        [
            raw_row(tp="NDRT##a", ref_no="RET1"),
            raw_row(tp="SCTXB/RCC/O/RETTP/x", ref_no="RET2"),
            raw_row(tp="SDDXB/RCC/I/RETTP/x", ref_no=None),  # TP[3] fallback
            raw_row(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1"),   # classic return → po_ids
        ],
        acc,
    )
    assert acc.return_po_ids == {"RET1", "RET2", "RETTP"}
    assert acc.po_ids == {"PO1"}


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
