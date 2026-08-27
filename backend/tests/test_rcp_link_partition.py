"""Turning returned payments into per-lot ghost slices.

A return batch almost always spans several original lots (measured on the
2026-08-12 extract: up to 12+ distinct originals behind one msgid), so the
movement is split, not moved. Each slice must be worth its lot's payments TO
THE CENT and signed like the movement the bank actually booked — that exactness
is what makes the target lot balance against the individual return legs already
sitting in it.
"""
from decimal import Decimal

import pytest

from app.services.rcp_link_parser import (
    BUCKET_MSGID_ONLY,
    BUCKET_PACS_ONLY,
    BUCKET_PAIR,
    BUCKET_PO,
    BucketKey,
    DumpRow,
    ghost_external_ref,
    payment_bucket,
)
from app.services.rcp_link_service import (
    CLAIM_TYPE,
    MAX_PO_KEYS,
    TARGET_LOT,
    TARGET_RECO,
    WHY_MULTI_LOT,
    WHY_NO_LOT,
    WHY_NO_PAYMENT,
    RcpLinkService,
    _movement_type,
)


def _row(po, amount, msgid="M1"):
    return DumpRow(
        msgid=msgid, entity_srl_num=f"SRL-{po}", orig_entity_id=po,
        amount=Decimal(amount), file_name="return_o.csv",
    )


def _lot(lot_id, pacs="PACS1", msgid="MSG1"):
    return {
        "target_id": lot_id, "target_kind": TARGET_LOT, "bucket_kind": BUCKET_PAIR,
        "bucket_pacs008": pacs, "bucket_msgid": msgid, "bucket_po": "", "bucket_ref": "",
        "label": f"PAIR:{pacs}|{msgid}", "currency": "EUR",
    }


# ── bucket rules (must mirror the DAG's payment_bucket) ─────────────

def test_payment_bucket_follows_the_dag_rules():
    assert payment_bucket("PACS", "MSG", "PO") == BucketKey(BUCKET_PAIR, pacs="PACS", msgid="MSG")
    assert payment_bucket("PACS", "", "PO") == BucketKey(BUCKET_PACS_ONLY, pacs="PACS")
    # PO wins over MSGID: a payment with no pacs008 is a single, reached only
    # through its PaymentNumber.
    assert payment_bucket("", "MSG", "PO") == BucketKey(BUCKET_PO, po="PO")
    assert payment_bucket("", "MSG", "") == BucketKey(BUCKET_MSGID_ONLY, msgid="MSG")
    assert payment_bucket("", "", "") is None


def test_bucket_components_are_uppercased_like_the_lot_row():
    """The datamart's collation is case-insensitive; the lot is looked up on
    these very values, so 'Rumelange' and 'RUMELANGE' must not be two buckets."""
    key = payment_bucket("pacs-x", "2412-20260731-Rumelange", "po")
    assert key.pacs == "PACS-X"
    assert key.msgid == "2412-20260731-RUMELANGE"


def test_ghost_ref_is_stable_and_bounded():
    key = BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSG1")
    first = ghost_external_ref((CLAIM_TYPE, "BLK2026198009409"), key)
    again = ghost_external_ref((CLAIM_TYPE, "BLK2026198009409"), key)
    other = ghost_external_ref((CLAIM_TYPE, "BLK2026198009409"),
                               BucketKey(BUCKET_PAIR, pacs="PACS2", msgid="MSG1"))

    assert first == again           # re-committing upserts the same rows
    assert first != other           # one ghost per bucket
    assert first.startswith("KEY:BLK2026198009409~")
    assert len(ghost_external_ref((CLAIM_TYPE, "X" * 200), key)) <= 128


# ── grouping by target lot ──────────────────────────────────────────

def test_payments_group_into_one_slice_per_lot():
    lots = {
        "PO1": dict(_lot("lot-a"), resolved_via="datamart"),
        "PO2": dict(_lot("lot-a"), resolved_via="datamart"),
        "PO3": dict(_lot("lot-b", pacs="PACS2"), resolved_via="entry_payment_status"),
    }

    targets, unresolved = RcpLinkService._targets_for(
        [_row("PO1", "70.00"), _row("PO2", "30.00"), _row("PO3", "500.00")], lots
    )

    assert unresolved == []
    # Ordered by weight: the biggest slice first.
    assert [t["target_id"] for t in targets] == ["lot-b", "lot-a"]
    by_lot = {t["target_id"]: t for t in targets}
    assert by_lot["lot-a"]["amount"] == Decimal("100.00")
    assert by_lot["lot-a"]["payment_count"] == 2
    assert by_lot["lot-a"]["pos"] == ["PO1", "PO2"]
    assert by_lot["lot-b"]["resolved_via"] == "entry_payment_status"


def test_a_payment_returned_twice_sums_into_one_po_slice():
    lots = {"PO1": dict(_lot("lot-a"), resolved_via="datamart")}

    targets, _ = RcpLinkService._targets_for(
        [_row("PO1", "70.00"), _row("PO1", "30.00")], lots
    )

    assert targets[0]["amount"] == Decimal("100.00")
    assert targets[0]["payment_count"] == 1


def test_unresolved_payments_are_listed_not_dropped():
    """A partial split is proposed: what is missing must stay visible, since the
    ghosts will then fall short of the booked amount."""
    lots = {
        "PO1": dict(_lot("lot-a"), resolved_via="datamart"),
        "PO2": {"target_id": None, "reason": WHY_NO_PAYMENT},
    }

    targets, unresolved = RcpLinkService._targets_for(
        [_row("PO1", "70.00"), _row("PO2", "30.00")], lots
    )

    assert [t["amount"] for t in targets] == [Decimal("70.00")]
    assert unresolved == [{"po": "PO2", "amount": Decimal("30.00"), "reason": WHY_NO_PAYMENT}]


# ── ghost valuation ─────────────────────────────────────────────────

def test_ghosts_are_signed_like_the_booked_movement():
    sign, total, error = RcpLinkService.validate_slices(
        Decimal("-500.00"), [Decimal("300.00"), Decimal("200.00")]
    )
    assert (sign, total, error) == (Decimal("-1"), Decimal("500.00"), "")

    sign, _, _ = RcpLinkService.validate_slices(Decimal("500.00"), [Decimal("500.00")])
    assert sign == Decimal("1")


def test_slices_may_fall_short_but_never_exceed_the_booking():
    _, _, partial = RcpLinkService.validate_slices(Decimal("500.00"), [Decimal("300.00")])
    assert partial == ""

    _, _, over = RcpLinkService.validate_slices(
        Decimal("500.00"), [Decimal("300.00"), Decimal("300.00")]
    )
    assert "dépassent" in over


@pytest.mark.parametrize("amounts", [[], [Decimal("0")], [Decimal("10"), Decimal("0.00")]])
def test_empty_or_zero_slices_are_refused(amounts):
    """A zero ghost adds a PENDING entry that settles nothing."""
    _, _, error = RcpLinkService.validate_slices(Decimal("100.00"), amounts)
    assert error


# ── member keys & movement type ─────────────────────────────────────

def test_member_keys_carry_the_bucket_then_the_payments():
    key = BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSG1")
    keys = RcpLinkService._member_keys(key, ["po1", "po2"])

    assert sorted((k.key_type, k.key_value) for k in keys) == [
        ("MSGID", "MSG1"), ("PACS008", "PACS1"), ("PO", "PO1"), ("PO", "PO2"),
    ]


def test_member_keys_stop_fanning_out_on_a_big_bulk():
    key = BucketKey(BUCKET_PAIR, pacs="PACS1", msgid="MSG1")
    keys = RcpLinkService._member_keys(key, [f"PO{i}" for i in range(MAX_PO_KEYS + 1)])

    assert [k.key_type for k in keys] == ["MSGID", "PACS008"]


@pytest.mark.parametrize(
    "particulars,expected",
    [
        ("SCTXB/I/BLK2026198009409", "SCTXB"),
        ("NDGB##20260729000145##LUXEMBOURG", "NDGB"),
        ("sctxb/o/26071507550300562", "SCTXB"),
        ("", "SCTXB"),
        (None, "SCTXB"),
    ],
)
def test_movement_type_reads_the_tp_prefix(particulars, expected):
    assert _movement_type(particulars) == expected


# ── classic bulk flow: the reconciliation key of the original ───────

def _classic(monkeypatch, *, known=None, status=None):
    """A service whose two database touches in the classic branch are stubbed."""
    service = RcpLinkService()
    monkeypatch.setattr(
        service, "known_reco_ids",
        lambda db, *, flow_id, reco_ids: (
            set(reco_ids) if known is None else {r for r in reco_ids if r in known}
        ),
    )
    monkeypatch.setattr(
        service, "_reco_ids_from_payment_status", lambda db, po_ids: dict(status or {})
    )
    return service


def test_the_classic_key_is_pacs008_then_messageid(monkeypatch):
    """Mirrors the COALESCE of resolve_bulk_returns (reco_datamart.py): PACS008
    first. Keyed the other way round, the ghost would land in a group of its own
    and never balance against the return legs."""
    service = _classic(monkeypatch)
    payments = {
        "PO1": [("PACS-A", "MSG-A")],   # both → PACS008 wins
        "PO2": [("", "MSG-B")],         # no pacs008 → the MessageID keys it
    }

    resolved = service._reco_keys_for_pos(None, ["PO1", "PO2"], payments, flow_id=16)

    assert resolved["PO1"]["target_id"] == "PACS-A"
    assert resolved["PO2"]["target_id"] == "MSG-B"
    assert resolved["PO1"]["target_kind"] == TARGET_RECO
    assert resolved["PO1"]["resolved_via"] == "datamart"


def test_a_key_no_entry_of_the_flow_carries_is_refused(monkeypatch):
    """The classic analogue of 'the lot does not exist'."""
    service = _classic(monkeypatch, known={"PACS-A"})
    payments = {"PO1": [("PACS-A", "")], "PO2": [("PACS-ORPHAN", "")]}

    resolved = service._reco_keys_for_pos(None, ["PO1", "PO2"], payments, flow_id=16)

    assert resolved["PO1"]["target_id"] == "PACS-A"
    assert resolved["PO2"]["target_id"] is None
    assert resolved["PO2"]["reason"] == WHY_NO_LOT


def test_entry_payment_status_fills_in_when_the_datamart_is_silent(monkeypatch):
    """The table is keyed by the flow's own reco_id, whatever the flow — so it
    answers for a classic group exactly as it does for a lot."""
    service = _classic(monkeypatch, status={"PO1": "PACS-FROM-APP"})

    resolved = service._reco_keys_for_pos(None, ["PO1", "PO2"], {}, flow_id=16)

    assert resolved["PO1"] == {
        "target_id": "PACS-FROM-APP", "resolved_via": "entry_payment_status",
        "target_kind": TARGET_RECO, "label": "PACS-FROM-APP",
    }
    assert resolved["PO2"]["target_id"] is None
    assert resolved["PO2"]["reason"] == WHY_NO_PAYMENT


def test_a_payment_pointing_at_two_groups_is_left_unresolved(monkeypatch):
    """Two current std.Payment rows disagreeing on the group: nothing here can
    arbitrate, and picking one would silently misfile the amount."""
    service = _classic(monkeypatch)
    payments = {"PO1": [("PACS-A", "MSG"), ("PACS-B", "MSG")]}

    resolved = service._reco_keys_for_pos(None, ["PO1"], payments, flow_id=16)

    assert resolved["PO1"]["target_id"] is None
    assert resolved["PO1"]["reason"] == WHY_MULTI_LOT


def test_the_parser_picks_the_branch(monkeypatch):
    """targets_for_pos is the only place the two worlds meet."""
    from app.models.flow import ParserType

    service = RcpLinkService()
    monkeypatch.setattr(
        service, "_lots_for_pos", lambda db, pos, payments, fsid: {"called": "lots"}
    )
    monkeypatch.setattr(
        service, "_reco_keys_for_pos",
        lambda db, pos, payments, *, flow_id: {"called": "reco"},
    )

    as_bb = service.targets_for_pos(
        None, ["PO1"], {}, flow_id=16, flow_source_id=4,
        parser_type=ParserType.FINACLE_BATCH_BOOKING_TRUE,
    )
    as_classic = service.targets_for_pos(
        None, ["PO1"], {}, flow_id=16, flow_source_id=5,
        parser_type=ParserType.FINACLE_DB,
    )

    assert as_bb == {"called": "lots"}
    assert as_classic == {"called": "reco"}
