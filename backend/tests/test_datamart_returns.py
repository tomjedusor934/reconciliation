"""Unit tests for the legacy parser's NDRT / PREFIX/RCC handling (pure).

Imports shared/dags/reco_datamart.py directly — no DB, no pyodbc, no airflow.
"""
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

from reco_datamart import (  # noqa: E402
    UNRESOLVED_RECO_ID,
    compute_reco_id,
    payment_po_id_for,
    return_po_id_for,
    return_reject_seg,
    reversal_ref_for,
)


def raw_row(tp=None, ref_no=None, remarks_1=None, channel=None):
    return {
        "TransactionParticulars": tp,
        "PaymentOrderID_Ref": ref_no,
        "Remarks_1": remarks_1,
        "Initiating_channel": channel,
    }


RETURN_MAP = {"RET1": "PACS-ORIG-1"}


def test_return_po_id_for_shapes():
    # NDRT: ref_no first, '##' last-segment tolerated
    assert return_po_id_for(raw_row(tp="NDRT##a##b", ref_no="RET1")) == "RET1"
    assert return_po_id_for(raw_row(tp="NDRT##a", ref_no="paysis##RET2")) == "RET2"
    assert return_po_id_for(raw_row(tp="NDRT##a", ref_no=None)) is None
    # bulk /RCC: ref_no first, TP segment[3] fallback
    assert return_po_id_for(raw_row(tp="SCTXB/RCC/O/RETTP/x", ref_no="RET3")) == "RET3"
    assert return_po_id_for(raw_row(tp="SDDXB/RCC/I/RETTP/x", ref_no=None)) == "RETTP"
    # other shapes never match
    assert return_po_id_for(raw_row(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1")) is None
    assert return_po_id_for(raw_row(tp="SWIFT/x", ref_no="R1")) is None


def test_compute_reco_id_ndrt_and_rcc_resolve_via_return_map():
    assert compute_reco_id(
        raw_row(tp="NDRT##a", ref_no="RET1"), {}, {}, RETURN_MAP
    ) == "PACS-ORIG-1"
    assert compute_reco_id(
        raw_row(tp="SCTXB/RCC/O/RET1/x", ref_no="RET1"), {}, {}, RETURN_MAP
    ) == "PACS-ORIG-1"
    # not resolved yet (missing std.Return / std.Payment row) → transient None
    assert compute_reco_id(raw_row(tp="NDRT##a", ref_no="RET404"), {}, {}, RETURN_MAP) is None
    assert compute_reco_id(
        raw_row(tp="SDXBB/RCC/I/RET404/x", ref_no=None), {}, {}, RETURN_MAP
    ) is None


def test_ndrt_and_rcc_are_not_reversal_candidates():
    assert reversal_ref_for(raw_row(tp="NDRT##1000305817##1000305716")) is None
    assert reversal_ref_for(raw_row(tp="SCTXB/RCC/O/RET1/x")) is None
    # classic NCC/NCP exclusion untouched
    assert reversal_ref_for(raw_row(tp="SCTXB/NCP/I/PO1/x")) is None


def test_existing_branches_untouched():
    # direct bulk → remarks_1, classic return → payment_map, unknown → sentinel
    assert compute_reco_id(
        raw_row(tp="SCTXB/I/BLK1", remarks_1="PACS9"), {}, {}, {}
    ) == "PACS9"
    assert compute_reco_id(
        raw_row(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1"), {"PO1": "RECO-PO1"}, {}, {}
    ) == "RECO-PO1"
    assert compute_reco_id(raw_row(tp="GLXYZ/other/x"), {}, {}, {}) == UNRESOLVED_RECO_ID


# --------------------------------------------------------------------------
# RRS / RCP, and the return/reject shape worn by an unlisted prefix.
#
# These four shapes were missing from the segment sets and from the prefix
# lists, so they fell through to the direct branch, which reads Remarks_1 AS a
# pacs008 — and on a return Remarks_1 is the counterparty IBAN or a UUID. On the
# outward float that minted 5 652 lots keyed on an IBAN or a UUID.
# --------------------------------------------------------------------------

def test_rrs_is_a_return_resolved_via_std_payment():
    row = raw_row(
        tp="SDDXB/RRS/I/PO1", ref_no="PO1",
        remarks_1="DC77B536-23D1-4F26-AA6A-5DA12A85C1EC",  # a UUID, NOT a pacs008
    )
    assert payment_po_id_for(raw_row(tp="SDDXB/RRS/I/PO1")) == "PO1"
    assert compute_reco_id(row, {"PO1": "RECO-PO1"}, {}, {}) == "RECO-PO1"
    # the UUID in Remarks_1 must never become the key
    assert compute_reco_id(row, {"PO1": "RECO-PO1"}, {}, {}) != row["Remarks_1"]


def test_rcp_is_a_reject_resolved_via_std_return():
    row = raw_row(
        tp="SCTXB/RCP/I/RET1/STACKINSAT", ref_no="RET1",
        remarks_1="FR7619733000010100000206732",  # an IBAN, NOT a pacs008
    )
    assert return_po_id_for(row) == "RET1"
    assert compute_reco_id(row, {}, {}, RETURN_MAP) == "PACS-ORIG-1"
    # TP segment[3] is the fallback when ref_no is empty, as for /RCC
    assert return_po_id_for(raw_row(tp="SCTXB/RCP/I/RET1/x")) == "RET1"


def test_return_shape_resolves_whatever_the_prefix():
    # 'Rev of /NCP/O/<po>/<name>' — how Finacle labels the reversal of a return.
    # Same segment, same PaymentNumber; only the prefix is free text.
    assert compute_reco_id(
        raw_row(tp="Rev of /NCP/O/PO1/TRESORERIE DE L'ETAT", ref_no="PO1"),
        {"PO1": "RECO-PO1"}, {}, {},
    ) == "RECO-PO1"
    assert compute_reco_id(
        raw_row(tp="Rev of BKRTP/NCP/O/PO1/CARLO"), {"PO1": "RECO-PO1"}, {}, {},
    ) == "RECO-PO1"
    assert return_reject_seg(["Rev of ", "NCP", "O", "PO1"]) == "NCP"
    assert return_reject_seg(["SCTXB", "I", "BLK1"]) is None
    assert return_reject_seg(["SCTXB"]) is None


def test_rvsl_puts_the_transaction_ref_one_segment_right():
    assert reversal_ref_for(
        raw_row(tp="SCTXB/RVSL/SCTXB260807000609659/REQUESTED BY CUSTOMER")
    ) == "SCTXB260807000609659"
    # the ordinary shape still reads segment[1]
    assert reversal_ref_for(raw_row(tp="SCTXB/SCTXB260727000294311/BARTHEL")) == "SCTXB260727000294311"


def test_a_real_bkrtp_return_stays_keyed_by_its_own_ref_no():
    # The instant branch is tested BEFORE the generic shape, on purpose: a
    # BKRTP/NCP is an IP keyed by ref_no, not a bulk return. Widening the shape
    # rule must not re-key the 15 269 of them on the outward float.
    assert compute_reco_id(
        raw_row(tp="BKRTP/NCP/O/PO1/TRESORERIE DE L'ETAT", ref_no="PO1"),
        {"PO1": "RECO-PO1"}, {}, {},
    ) == "PO1"
