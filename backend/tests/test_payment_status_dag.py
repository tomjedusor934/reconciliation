"""Unit tests for the payment-status DAG module (pure functions).

Imports shared/dags/reco_payment_status.py directly — no DB, no pyodbc, no
airflow (the MsSqlHook import is local to run_payment_status_sync).
"""
import sys
from datetime import date, datetime
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
    _to_date,
    build_status_rows,
    collect_ps_lookup_inputs,
    movement_payment_pos,
)


def raw_row(tp=None, ref_no=None, remarks_1=None, channel=None):
    """std.Movement shape (datamart casing)."""
    return {
        "TransactionParticulars": tp,
        "PaymentOrderID_Ref": ref_no,
        "Remarks_1": remarks_1,
        "Initiating_channel": channel,
    }


# (po_id, status, amount, req_exec_date) — amount/date stringified over the wire.
PACS_MAP = {
    "PACS1": [
        ("PO-A", "ACC", "10.00", "2026-07-06"),
        ("PO-B", "PDNG", "5.50", "2026-07-07"),
    ]
}
MSGID_MAP = {"AGG1": [("PO-C", "ACC", "1.00", "2026-07-08")]}
RETURN_MAP = {"RET1": [("PO-ORIG", "RJCT", "3.00", "2026-07-09")]}
# po_id -> (status, amount, req_exec_date)
PO_STATUS = {
    "PO1": ("RJCT", "2.00", "2026-07-01"),
    "POREF": ("ACC", "4.00", None),
}


def test_collect_inputs_per_movement_type():
    pacs, msgids, pos, return_pos = set(), set(), set(), set()
    collect_ps_lookup_inputs(
        [
            raw_row(tp="SCTXB/O/x", remarks_1="PACS1"),          # direct bulk → pacs (+msgid fallback)
            raw_row(tp="SDDXB/NCC/O/POTP/x", ref_no="POREF"),    # SP return → po
            raw_row(tp="SCTXB/RCC/x", ref_no="RET1"),            # SP reject → return po
            raw_row(tp="NDGB/agg", remarks_1="AGG1"),            # NDGB → msgid
            raw_row(tp="NDRJ/rej", ref_no="paysis##PO9"),        # reject → po
            raw_row(tp="NDRT/rej", ref_no="RET2"),               # reject-of-return → return po
            raw_row(tp="SWIFT/x", ref_no="R1"),                  # single → po
            raw_row(tp=None, ref_no="MOSEL-PO", channel="Z6"),   # channel → po
            raw_row(tp=None, ref_no="IGNORED", channel="XX"),    # unknown channel
            raw_row(tp="GL/other"),                              # unsupported → nothing
        ],
        pacs, msgids, pos, return_pos,
    )
    # a direct bulk id is fed to BOTH resolvers (pacs008 first, MessageID fallback)
    assert pacs == {"PACS1"}
    assert msgids == {"PACS1", "AGG1"}
    assert pos == {"POREF", "PO9", "R1", "MOSEL-PO"}
    assert return_pos == {"RET1", "RET2"}


def test_movement_payment_pos_bulk_expands_to_all_payments():
    pos = movement_payment_pos(
        raw_row(tp="SCTXB/O/x", remarks_1="PACS1"), PACS_MAP, MSGID_MAP, PO_STATUS
    )
    assert pos == [
        ("PO-A", "ACC", "10.00", "2026-07-06"),
        ("PO-B", "PDNG", "5.50", "2026-07-07"),
    ]


def test_movement_payment_pos_singles_and_returns():
    # SP return: its own PO, fields resolved through po_status
    assert movement_payment_pos(
        raw_row(tp="SCTXB/NCP/I/PO1/x", ref_no="PO1"), {}, {}, PO_STATUS
    ) == [("PO1", "RJCT", "2.00", "2026-07-01")]
    # single with a PO unknown to std.Payment → kept with every field None ('?')
    assert movement_payment_pos(
        raw_row(tp="BKRTP/x", ref_no="R404"), {}, {}, PO_STATUS
    ) == [("R404", None, None, None)]
    # NDGB expands through msgid_map
    assert movement_payment_pos(
        raw_row(tp="NDGB/agg", remarks_1="AGG1"), {}, MSGID_MAP, {}
    ) == [("PO-C", "ACC", "1.00", "2026-07-08")]
    # SP reject resolves to the ORIGINAL payment through return_status
    assert movement_payment_pos(
        raw_row(tp="SCTXB/RCC/x", ref_no="RET1"), {}, {}, PO_STATUS, RETURN_MAP
    ) == [("PO-ORIG", "RJCT", "3.00", "2026-07-09")]
    # MOSEL/Z6 movement keyed by channel (this payment has no ReqExecDate)
    assert movement_payment_pos(
        raw_row(tp=None, ref_no="POREF", channel="Z6"), {}, {}, PO_STATUS
    ) == [("POREF", "ACC", "4.00", None)]
    # no payment reference at all
    assert movement_payment_pos(raw_row(tp="GL/other"), {}, {}, {}) == []
    assert movement_payment_pos(raw_row(tp=None), {}, {}, {}) == []


def test_build_status_rows_keys_every_payment_on_the_reco():
    rows = build_status_rows(
        "BLK2026177009757",
        [("PO-A", "ACC", "10.00", "2026-07-06"), ("PO-B", None, None, None)],
    )
    assert rows == [
        {
            "reco_id": "BLK2026177009757",
            "po_id": "PO-A",
            "status": "ACC",
            "amount": "10.00",
            "req_exec_date": "2026-07-06",
        },
        {
            "reco_id": "BLK2026177009757",
            "po_id": "PO-B",
            "status": None,
            "amount": None,
            "req_exec_date": None,
        },
    ]


def test_to_date_always_yields_a_pure_date():
    # the backend column is a DATE: a datetime string would fail validation
    assert _to_date(datetime(2026, 7, 6, 11, 30)) == "2026-07-06"
    assert _to_date(date(2026, 7, 6)) == "2026-07-06"
    assert _to_date("2026-07-06 11:30:00") == "2026-07-06"
    assert _to_date("2026-07-06") == "2026-07-06"
    assert _to_date(None) is None
    assert _to_date("   ") is None
