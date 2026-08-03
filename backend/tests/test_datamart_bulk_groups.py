"""Unit tests for bulk-booked instant payments (multiline & co) — pure.

An instant payment is normally 1 PaymentNumber = 1 MessageID, so its ref_no can
serve as reco_id. Multiline breaks that: N instant payments share one MessageID
and are booked in bulk, so the N movements must be regrouped onto the MessageID.

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
    bulk_groups_from_rows,
    compute_reco_id,
    instant_po_id_for,
)


def raw_row(tp=None, ref_no=None, remarks_1=None, channel=None):
    return {
        "TransactionParticulars": tp,
        "PaymentOrderID_Ref": ref_no,
        "Remarks_1": remarks_1,
        "Initiating_channel": channel,
    }


def app_entry(tp=None, ref_no=None, remarks_1=None, channel=None):
    """The lowercase shape the backend hands back on /tasks/finacle/unresolved —
    the retry path feeds those dicts through the very same functions."""
    return {
        "transaction_particulars": tp,
        "ref_no": ref_no,
        "remarks_1": remarks_1,
        "payload_raw": {"Initiating_channel": channel},
    }


# --------------------------------------------------------------------------
# bulk_groups_from_rows — (MessageID, PaymentNumber, InitModule) -> {po: reco}
# --------------------------------------------------------------------------

def test_one_to_one_payment_is_never_regrouped():
    # the 99% case: 1 PO on its own MessageID, ordinary module → keeps its PO id
    mapping, modules = bulk_groups_from_rows([("MSG1", "PO1", "SCTIP")])
    assert mapping == {}
    assert modules == {}


def test_two_pos_on_one_message_id_is_a_bulk():
    mapping, _ = bulk_groups_from_rows(
        [("MSG1", "PO1", "SCTIP"), ("MSG1", "PO2", "SCTIP")]
    )
    assert mapping == {"PO1": "MSG1", "PO2": "MSG1"}


def test_single_po_with_multiline_init_module_is_a_bulk():
    # a bulk of one is indistinguishable on cardinality — InitModule catches it
    mapping, modules = bulk_groups_from_rows([("MSG1", "PO1", "MLTLIP")])
    assert mapping == {"PO1": "MSG1"}
    assert modules == {"PO1": "MLTLIP"}


def test_multiline_flag_applies_to_the_whole_message_id():
    # only one member carries the module, the whole group is still a bulk
    mapping, modules = bulk_groups_from_rows(
        [("MSG1", "PO1", "MLTLIP"), ("MSG1", "PO2", None)]
    )
    assert mapping == {"PO1": "MSG1", "PO2": "MSG1"}
    assert modules == {"PO1": "MLTLIP"}  # traceability only, no invented value


def test_groups_are_independent():
    mapping, _ = bulk_groups_from_rows(
        [("MSG1", "PO1", None), ("MSG1", "PO2", None), ("MSG2", "PO3", None)]
    )
    assert mapping == {"PO1": "MSG1", "PO2": "MSG1"}  # MSG2 is 1-1 → untouched


def test_blank_message_id_or_po_is_ignored():
    mapping, _ = bulk_groups_from_rows(
        [("   ", "PO1", "MLTLIP"), (None, "PO2", "MLTLIP"), ("MSG1", "  ", "MLTLIP")]
    )
    assert mapping == {}


def test_init_module_matching_is_case_and_space_insensitive():
    mapping, modules = bulk_groups_from_rows([("MSG1", "PO1", "  mltlip ")])
    assert mapping == {"PO1": "MSG1"}
    assert modules == {"PO1": "MLTLIP"}


def test_duplicate_scd2_rows_do_not_fake_a_bulk():
    # the same PO twice (e.g. two 'current' rows) is still ONE payment
    mapping, _ = bulk_groups_from_rows([("MSG1", "PO1", None), ("MSG1", "PO1", None)])
    assert mapping == {}


# --------------------------------------------------------------------------
# instant_po_id_for — which PO ids seed the bulk scan
# --------------------------------------------------------------------------

def test_instant_po_id_for_shapes():
    assert instant_po_id_for(raw_row(tp="SWIFT/x/y", ref_no="PO1")) == "PO1"
    assert instant_po_id_for(raw_row(tp="BKRTP/x/y", ref_no="PO2")) == "PO2"
    assert instant_po_id_for(raw_row(tp="SCRT1/x/y", ref_no=" PO3 ")) == "PO3"
    # instant prefix without ref_no = a reversal hiding in the IP flow, not a PO
    assert instant_po_id_for(raw_row(tp="SWIFT/BKRTP000305424/NAME")) is None
    # other flows never seed the scan
    assert instant_po_id_for(raw_row(tp="SCTXB/I/BLK1", ref_no="PO4")) is None
    assert instant_po_id_for(raw_row(tp="NDRT##a", ref_no="RET1")) is None
    assert instant_po_id_for(raw_row(tp=None, ref_no="PO5", channel="Z6")) is None
    assert instant_po_id_for(raw_row(tp=None, ref_no="PO6")) is None
    # works on re-pushed app entries too (lowercase keys)
    assert instant_po_id_for(app_entry(tp="SWIFT/x/y", ref_no="PO7")) == "PO7"
    assert instant_po_id_for(app_entry(tp=None, ref_no="PO8", channel="Z9")) is None


# --------------------------------------------------------------------------
# compute_reco_id — the bulk map overrides the instant ref_no, nothing else
# --------------------------------------------------------------------------

BULK_MAP = {"PO1": "MSG1", "PO2": "MSG1"}


def test_bulk_members_are_keyed_on_the_message_id():
    assert compute_reco_id(
        raw_row(tp="SWIFT/x/y", ref_no="PO1"), {}, {}, {}, BULK_MAP
    ) == "MSG1"
    assert compute_reco_id(
        raw_row(tp="BKRTP/x/y", ref_no="PO2"), {}, {}, {}, BULK_MAP
    ) == "MSG1"


def test_plain_instant_payment_keeps_its_own_po_id():
    assert compute_reco_id(
        raw_row(tp="SWIFT/x/y", ref_no="PO404"), {}, {}, {}, BULK_MAP
    ) == "PO404"


def test_bulk_map_does_not_leak_into_other_branches():
    # a MOSEL/Webripost movement keys on ref_no but is not a payment of this circuit
    assert compute_reco_id(
        raw_row(tp=None, ref_no="PO1", channel="Z6"), {}, {}, {}, BULK_MAP
    ) == "PO1"
    # a bulk-direct movement still keys on remarks_1
    assert compute_reco_id(
        raw_row(tp="SCTXB/I/BLK1", remarks_1="PACS9"), {}, {}, {}, BULK_MAP
    ) == "PACS9"


def test_backward_compatible_without_the_bulk_map():
    # 4-arg calls (the pre-existing signature) keep the exact previous behavior
    assert compute_reco_id(raw_row(tp="SWIFT/x/y", ref_no="PO1"), {}, {}, {}) == "PO1"
    assert compute_reco_id(raw_row(tp="GLXYZ/other/x"), {}, {}, {}) == UNRESOLVED_RECO_ID


def test_reversal_in_the_instant_flow_still_falls_through_to_the_reversal_map():
    # instant prefix, no ref_no → reversal lookup on parts[1] (regression guard for
    # the dead branch removed alongside this feature)
    assert compute_reco_id(
        raw_row(tp="BKRTP/BKRTP000305424/POST TELECOM"),
        {}, {"BKRTP000305424": "MSG-REV"}, {}, BULK_MAP,
    ) == "MSG-REV"
