"""Unit tests for the Batch Booking True bucketing logic (pure functions).

Imports shared/dags/reco_datamart_bb.py directly (module-level deps: stdlib +
requests via reco_common) — no DB, no pyodbc, no airflow, no app.* import.

Replaces test_bb_clustering.py: there is no union-find any more. A lot is a
(PACS008 × MSGID) bucket whose uuid5 is a pure function of its identity, and a
movement spanning several buckets is split into ghosts instead of dragging them
all into one cluster.
"""
import sys
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

from reco_datamart_bb import (  # noqa: E402
    BUCKET_MSGID_ONLY,
    BUCKET_PACS_ONLY,
    BUCKET_PAIR,
    BUCKET_PO,
    KEY_MSGID,
    KEY_PACS008,
    KEY_PO,
    UNRESOLVED_RECO_ID,
    BBLookupInputs,
    BucketKey,
    PaymentGroup,
    PaymentRef,
    bucket_id,
    bucket_keys,
    classify_bb_movement,
    collect_lookup_inputs,
    movement_resolution,
    ndrj_po_id,
    partition_payments,
    payment_bucket,
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
D = Decimal


def resolve(row, pacs_map=None, msgid_map=None, po_map=None, return_map=None, txnref_map=None):
    return movement_resolution(
        row, pacs_map or {}, msgid_map or {}, po_map or {}, return_map or {},
        txnref_map=txnref_map or {},
    )


# ---------------------------------------------------------------------------
# classify / po-id extraction (unchanged behaviour — the shapes still matter)
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
    assert ndrj_po_id(raw_row(ref_no="a##b##PO9")) == "PO9"
    assert ndrj_po_id(raw_row(ref_no="PO777")) == "PO777"
    assert ndrj_po_id(raw_row(ref_no="  ")) is None
    assert ndrj_po_id(raw_row(ref_no=None)) is None


def test_sp_return_po_id_ref_no_first_then_tp_fallback():
    assert sp_return_po_id(raw_row(tp="SCTXB/NCP/I/POTP/x", ref_no="POREF")) == "POREF"
    assert sp_return_po_id(raw_row(tp="SCTXB/NCC/O/POTP/x", ref_no=None)) == "POTP"
    assert sp_return_po_id(raw_row(tp="SCTXB/NCC", ref_no=None)) is None


# ---------------------------------------------------------------------------
# bucket identity
# ---------------------------------------------------------------------------

def test_bucket_id_is_a_pure_function_of_the_identity():
    key = BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSGA")
    assert bucket_id(3, key) == bucket_id(3, BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSGA"))
    # This is what lets the DAG name its lots without reading anything back.
    assert bucket_id(3, key) != bucket_id(4, key)                      # per source
    assert bucket_id(3, key) != bucket_id(3, BucketKey(BUCKET_PAIR, pacs="PACS2", msgid="MSGA"))
    assert bucket_id(3, key) != bucket_id(3, BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSGB"))
    # A uuid, because it lands in reconciliation_entry.reco_id / movement_lot.id.
    assert len(bucket_id(3, key)) == 36


def test_a_label_msgid_no_longer_glues_unrelated_pacs008():
    """The whole point: 'LUXEMBOURG' under two pacs008 is two buckets, not one
    mega-lot. This is the regression that produced 50 898 members in prod."""
    a = bucket_id(1, BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="LUXEMBOURG"))
    b = bucket_id(1, BucketKey(BUCKET_PAIR, pacs="PACS2", msgid="LUXEMBOURG"))
    assert a != b


def test_payment_bucket_kinds():
    assert payment_bucket("P", "M", "PO") == BucketKey(BUCKET_PAIR, pacs="P", msgid="M")
    assert payment_bucket("P", None, "PO") == BucketKey(BUCKET_PACS_ONLY, pacs="P")
    # No pacs008 → a single (SWIFT/BKRTP), reached through its PaymentNumber and
    # never its MessageID: its own movement carries no MSGID key.
    assert payment_bucket(None, "M", "PO") == BucketKey(BUCKET_PO, po="PO")
    assert payment_bucket(None, "M", None) == BucketKey(BUCKET_MSGID_ONLY, msgid="M")
    assert payment_bucket(None, None, None) is None
    assert payment_bucket("  ", "", None) is None


def test_partition_payments_groups_by_pair_and_sums():
    groups = partition_payments([
        PaymentRef("P", "A", "po1", D("600")),
        PaymentRef("P", "A", "po2", D("100")),
        PaymentRef("P", "B", "po3", D("300")),
    ])
    assert set(groups) == {
        BucketKey(BUCKET_PAIR, pacs="P", msgid="A"),
        BucketKey(BUCKET_PAIR, pacs="P", msgid="B"),
    }
    assert groups[BucketKey(BUCKET_PAIR, pacs="P", msgid="A")].amount == D("700")
    assert groups[BucketKey(BUCKET_PAIR, pacs="P", msgid="A")].pos == ["po1", "po2"]
    assert groups[BucketKey(BUCKET_PAIR, pacs="P", msgid="B")].count == 1


# ---------------------------------------------------------------------------
# payment resolution, per movement shape
# ---------------------------------------------------------------------------

def test_sp_direct_resolves_every_payment_of_its_pacs008():
    pacs_map = {"PACS1": [("MSGA", "po1", D("600")), ("MSGB", "po2", D("400"))]}
    res = resolve(raw_row(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map=pacs_map)
    assert len(res.payments) == 2
    assert len(partition_payments(res.payments)) == 2  # → must be split
    assert res.fallback == BucketKey(BUCKET_PACS_ONLY, pacs="PACS1")


def test_sp_direct_without_payments_falls_back_to_its_pacs008_bucket():
    """std.Payment has nothing yet: the movement still lands somewhere, so its
    counterpart can meet it, instead of waiting in limbo."""
    res = resolve(raw_row(tp="SCTXB/O/x", remarks_1="PACS1"))
    assert res.payments == ()
    assert res.fallback == BucketKey(BUCKET_PACS_ONLY, pacs="PACS1")


def test_sp_direct_without_remarks_is_transient():
    res = resolve(raw_row(tp="SCTXB/O/x", remarks_1=None))
    assert res.payments == () and res.fallback is None


def test_sp_return_lands_in_the_original_bulks_bucket():
    po_map = {"PO1": [("MSGA", "PACS1", D("50"))]}
    res = resolve(raw_row(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1"), po_map=po_map)
    assert list(partition_payments(res.payments)) == [
        BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSGA")
    ]


def test_ndgb_resolves_its_message_groups_payments():
    msgid_map = {"AGG1": [("PACS1", "po1", D("600"))]}
    res = resolve(raw_row(tp="NDGB/agg", remarks_1="AGG1"), msgid_map=msgid_map)
    assert list(partition_payments(res.payments)) == [
        BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="AGG1")
    ]


def test_ndgb_with_a_label_msgid_spans_buckets_and_must_split():
    """A MessageID reused as a label resolves payments across several pacs008 —
    the NDGB is then an aggregate of several buckets, exactly like an SP bulk."""
    msgid_map = {"LUXEMBOURG": [("PACS1", "po1", D("10")), ("PACS2", "po2", D("20"))]}
    res = resolve(raw_row(tp="NDGB/agg", remarks_1="LUXEMBOURG"), msgid_map=msgid_map)
    assert len(partition_payments(res.payments)) == 2


def test_ndgb_singles_fan_out_to_po_buckets():
    """Empty pacs008 = the underlying payments are SWIFT/BKRTP singles, which
    only ever carry their PaymentNumber."""
    msgid_map = {"AGG1": [("", "po1", D("10")), (None, "po2", D("20"))]}
    res = resolve(raw_row(tp="NDGB/agg", remarks_1="AGG1"), msgid_map=msgid_map)
    assert set(partition_payments(res.payments)) == {
        BucketKey(BUCKET_PO, po="po1"),
        BucketKey(BUCKET_PO, po="po2"),
    }


def test_ndrj_reject_joins_its_payments_bucket():
    po_map = {"PO9": [("MSGA", "PACS1", D("5"))]}
    res = resolve(raw_row(tp="NDRJ/rej", ref_no="paysis##PO9"), po_map=po_map)
    assert list(partition_payments(res.payments)) == [
        BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSGA")
    ]
    # Unknown PO → still lands on its own PO bucket rather than nowhere.
    res = resolve(raw_row(tp="NDRJ/rej", ref_no="paysis##PO9"))
    assert res.fallback == BucketKey(BUCKET_PO, po="PO9")


def test_instant_single_is_its_own_po_bucket_without_a_lookup():
    for prefix in ("SWIFT", "BKRTP", "SCRT1"):
        res = resolve(raw_row(tp=f"{prefix}/x/y", ref_no="R1"))
        assert res.payments == ()
        assert res.fallback == BucketKey(BUCKET_PO, po="R1")


def test_instant_aggregate_without_ref_no_goes_through_transaction_ref():
    msgid_map = {"AGG1": [("PACS1", "po1", D("7"))]}
    res = resolve(
        raw_row(tp="SWIFT/TRN123/NAME", ref_no=None),
        msgid_map=msgid_map,
        txnref_map={"TRN123": "AGG1"},
    )
    assert list(partition_payments(res.payments)) == [
        BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="AGG1")
    ]
    # TransactionRef not in std.Payment yet → transient, retried next run.
    assert resolve(raw_row(tp="SWIFT/TRN999/NAME", ref_no=None)).fallback is None


def test_reject_of_return_settles_against_the_original_payment():
    return_map = {"RET1": [("ORIG1", "MSGA", "PACS1", D("42"))]}
    for tp in ("NDRT/x", "SCTXB/RCC/I/x"):
        res = resolve(raw_row(tp=tp, ref_no="RET1"), return_map=return_map)
        assert list(partition_payments(res.payments)) == [
            BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSGA")
        ]
        # Its own return PaymentNumber pairs the NDRT↔RCC legs when std.Payment
        # knows nothing yet.
        assert res.fallback == BucketKey(BUCKET_PO, po="RET1")


def test_non_bb_movement_types_return_none():
    assert resolve(raw_row(tp="GLXYZ/other")) is None
    assert resolve(raw_row(tp=None)) is None


def test_resolution_handles_double_hash_separator_and_app_entry_shape():
    pacs_map = {"PACS1": [("MSGA", "po1", D("1"))]}
    assert resolve(raw_row(tp="SCTXB##O##x", remarks_1="PACS1"), pacs_map=pacs_map).payments
    assert resolve(app_entry(tp="SCTXB/O/x", remarks_1="PACS1"), pacs_map=pacs_map).payments


# ---------------------------------------------------------------------------
# searchable keys
# ---------------------------------------------------------------------------

def test_bucket_keys_carry_the_identity_and_small_payment_sets():
    key = BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSGA")
    keys = bucket_keys(key, PaymentGroup(D("10"), ["po1", "po2"]))
    assert (KEY_PACS008, "PACS1") in keys and (KEY_MSGID, "MSGA") in keys
    assert (KEY_PO, "po1") in keys and (KEY_PO, "po2") in keys


def test_bucket_keys_drop_the_po_fan_out_of_a_bulk(monkeypatch):
    """A bulk bucket holds thousands of PO ids; indexing them all would trade a
    real cost for a lookup that entry_payment_status already serves."""
    import reco_datamart_bb as bb

    monkeypatch.setattr(bb, "MAX_PO_KEYS", 2)
    key = BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSGA")
    keys = bb.bucket_keys(key, PaymentGroup(D("10"), ["po1", "po2", "po3"]))
    assert keys == [(KEY_MSGID, "MSGA"), (KEY_PACS008, "PACS1")]


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


def test_unresolved_sentinel_is_the_backend_one():
    assert UNRESOLVED_RECO_ID == "Not Supported"
