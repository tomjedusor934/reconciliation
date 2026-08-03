"""Unit tests for the payment-status DAG module (pure functions).

Imports shared/dags/reco_payment_status.py directly — no DB, no pyodbc, no
airflow (the MsSqlHook import is local to run_payment_status_sync).
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

from reco_payment_status import (  # noqa: E402
    build_status_rows,
    collect_ps_lookup_inputs,
    movement_payment_pos,
    to_movement_shape,
)


def raw_row(tp=None, ref_no=None, remarks_1=None, channel=None):
    """std.Movement shape (datamart casing)."""
    return {
        "TransactionParticulars": tp,
        "PaymentOrderID_Ref": ref_no,
        "Remarks_1": remarks_1,
        "Initiating_channel": channel,
    }


def app_movement(tp=None, ref_no=None, remarks_1=None, channel=None):
    """Backend /payment-status/movements shape (lowercase entry fields)."""
    return to_movement_shape(
        {
            "external_ref": "T1",
            "account": "001",
            "value_date": "2026-07-06T00:00:00",
            "operation_date": None,
            "transaction_particulars": tp,
            "ref_no": ref_no,
            "remarks_1": remarks_1,
            "initiating_channel": channel,
        }
    )


# pacs/msgid/return maps hold (po_id, status, amount); po_status holds
# {po_id: (status, amount)}. amount is the stringified std.Payment amount.
PACS_MAP = {"PACS1": [("PO-A", "ACC", "10.00"), ("PO-B", "PDNG", "20.00")]}
MSGID_MAP = {"AGG1": [("PO-C", "ACC", "5.00")]}
PO_STATUS = {"PO1": ("RJCT", "7.00"), "POREF": ("ACC", "3.00")}
RETURN_STATUS = {"RET1": [("ORIG1", "RJCT", "9.00")]}


def test_collect_inputs_per_movement_type():
    pacs, msgids, pos, ret_pos = set(), set(), set(), set()
    collect_ps_lookup_inputs(
        [
            raw_row(tp="SCTXB/O/x", remarks_1="PACS1"),          # direct bulk → pacs
            raw_row(tp="SDDXB/NCC/O/POTP/x", ref_no="POREF"),    # SP return → po
            raw_row(tp="SCTXB/RCC/O/RETTP/x", ref_no="RET1"),    # SP reject → return po
            raw_row(tp="NDRT##a", ref_no="RET2"),                # reject-of-return → return po
            raw_row(tp="NDGB/agg", remarks_1="AGG1"),            # NDGB → msgid
            raw_row(tp="NDRJ/rej", ref_no="paysis##PO9"),        # reject → po
            raw_row(tp="SWIFT/x", ref_no="R1"),                  # single → po
            raw_row(tp=None, ref_no="MOSEL-PO", channel="Z6"),   # channel → po
            raw_row(tp="GL/other"),                              # unsupported → nothing
        ],
        pacs, msgids, pos, ret_pos,
    )
    assert pacs == {"PACS1"}
    # Direct bulk feeds BOTH resolvers (pacs008 first, MessageID fallback).
    assert msgids == {"AGG1", "PACS1"}
    assert pos == {"POREF", "PO9", "R1", "MOSEL-PO"}
    assert ret_pos == {"RET1", "RET2"}


def test_collect_inputs_works_on_app_movement_shape():
    pacs, msgids, pos, ret_pos = set(), set(), set(), set()
    collect_ps_lookup_inputs(
        [
            app_movement(tp="SCTXB/O/x", remarks_1="PACS1"),
            app_movement(tp=None, ref_no="MOSEL-PO", channel="Z6"),
        ],
        pacs, msgids, pos, ret_pos,
    )
    assert pacs == {"PACS1"}
    assert msgids == {"PACS1"}  # direct bulk also feeds the MessageID resolver
    assert pos == {"MOSEL-PO"}  # channel remapped by to_movement_shape


def test_movement_payment_pos_bulk_expands_to_all_payments():
    pos = movement_payment_pos(
        app_movement(tp="SCTXB/O/x", remarks_1="PACS1"), PACS_MAP, MSGID_MAP, PO_STATUS
    )
    assert pos == [("PO-A", "ACC", "10.00"), ("PO-B", "PDNG", "20.00")]


def test_movement_payment_pos_bulk_falls_back_to_messageid():
    # A BLK bulk id lives only in std.Payment.MessageID (not MessageIDPACS008):
    # pacs_map misses, msgid_map hits → payments resolved via the fallback.
    blk_msgid_map = {"BLK2026": [("PO-X", "ACCC", "1.00"), ("PO-Y", "PDNG", "2.00")]}
    pos = movement_payment_pos(
        app_movement(tp="SCTXB/I/BLK2026", remarks_1="BLK2026"), {}, blk_msgid_map, {}
    )
    assert pos == [("PO-X", "ACCC", "1.00"), ("PO-Y", "PDNG", "2.00")]


def test_movement_payment_pos_bulk_prefers_pacs_over_messageid():
    # When the same remarks_1 resolves in BOTH maps, pacs008 wins — the
    # MessageID fallback only kicks in when pacs_map has nothing for that id.
    pacs = {"ID1": [("PO-A", "ACC", "1.00")]}
    msgid = {"ID1": [("PO-Z", "RJCT", "9.00")]}
    assert movement_payment_pos(
        app_movement(tp="SCTXB/I/x", remarks_1="ID1"), pacs, msgid, {}
    ) == [("PO-A", "ACC", "1.00")]


def test_movement_payment_pos_singles_and_returns():
    # SP return: its own PO, (status, amount) resolved through po_status
    assert movement_payment_pos(
        app_movement(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1"), {}, {}, PO_STATUS
    ) == [("PO1", "RJCT", "7.00")]
    # single with a PO unknown to std.Payment → kept with status/amount None ('?')
    assert movement_payment_pos(
        app_movement(tp="BKRTP/x", ref_no="R404"), {}, {}, PO_STATUS
    ) == [("R404", None, None)]
    # NDGB expands through msgid_map
    assert movement_payment_pos(
        app_movement(tp="NDGB/agg", remarks_1="AGG1"), {}, MSGID_MAP, {}
    ) == [("PO-C", "ACC", "5.00")]
    # MOSEL/Z6 movement keyed by channel
    assert movement_payment_pos(
        app_movement(tp=None, ref_no="POREF", channel="Z6"), {}, {}, PO_STATUS
    ) == [("POREF", "ACC", "3.00")]
    # no payment reference at all
    assert movement_payment_pos(app_movement(tp="GL/other"), {}, {}, {}) == []
    assert movement_payment_pos(app_movement(tp=None), {}, {}, {}) == []


def test_movement_payment_pos_ndrt_rcc_resolve_to_original_po():
    # po_id stored = OriginalPo (the PaymentNumber carrying the Status/Amount)
    assert movement_payment_pos(
        app_movement(tp="NDRT##a", ref_no="RET1"), {}, {}, {}, RETURN_STATUS
    ) == [("ORIG1", "RJCT", "9.00")]
    assert movement_payment_pos(
        app_movement(tp="SDXBB/RCC/I/RET1/x", ref_no="RET1"), {}, {}, {}, RETURN_STATUS
    ) == [("ORIG1", "RJCT", "9.00")]
    # std.[Return] row not resolvable yet → nothing to store
    assert movement_payment_pos(
        app_movement(tp="NDRT##a", ref_no="RET404"), {}, {}, {}, RETURN_STATUS
    ) == []


def test_build_status_rows_keys_by_reco_id():
    rows = build_status_rows("RECO-1", [("PO-A", "ACC", "10.00"), ("PO-B", None, None)])
    assert rows == [
        {"reco_id": "RECO-1", "po_id": "PO-A", "status": "ACC", "amount": "10.00"},
        {"reco_id": "RECO-1", "po_id": "PO-B", "status": None, "amount": None},
    ]
