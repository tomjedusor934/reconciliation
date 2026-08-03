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
    return_po_id_for,
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
