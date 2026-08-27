"""Part 1 control: a movement's dump rows must be as many as it announces AND
add up to what it booked.

This is not a cosmetic check — it is the conservation proof of the split. When
count and sum both hold, slicing the movement over its target lots is exact to
the cent; when they do not, the movement is still proposed (decision: the
control is non-blocking) and the gap surfaces as ``parent_mismatch`` once
committed.
"""
from decimal import Decimal

from app.services.rcp_link_parser import (
    CTRL_AMOUNT_MISMATCH,
    CTRL_COUNT_MISMATCH,
    CTRL_DUPLICATE_MOVEMENT,
    CTRL_NOT_FOUND,
    CTRL_NOT_SETTLED,
    CTRL_OK,
    DumpRow,
    LinkMovement,
    control_all,
    control_movement,
)


def _movement(msgid="M1", count=2, amount="100.00"):
    return LinkMovement(
        msgid=msgid, service_id="RCP", direction="I",
        num_records=count, settlement_amount=Decimal(amount), tran_id="PF1",
    )


def _row(msgid="M1", po="PO1", amount="50.00", file_name="return_o.csv"):
    return DumpRow(
        msgid=msgid, entity_srl_num=f"SRL-{po}", orig_entity_id=po,
        amount=Decimal(amount), file_name=file_name,
    )


def test_ok_when_count_and_sum_agree():
    result = control_movement(_movement(), [_row(po="A"), _row(po="B")])

    assert result.status == CTRL_OK and result.ok
    assert result.delta_count == 0
    assert result.delta_amount == Decimal("0.00")
    assert result.files == ["return_o.csv"]


def test_missing_rows_are_a_count_mismatch_with_the_gap():
    """BLK2026195003172 in the 2026-08-12 extract: 9 rows of 10, -9 000 €."""
    result = control_movement(
        _movement(count=10, amount="11299.47"),
        [_row(po=str(i), amount="255.497") for i in range(9)],
    )

    assert result.status == CTRL_COUNT_MISMATCH
    assert result.delta_count == -1
    assert result.delta_amount == Decimal("-9000.00")


def test_right_count_wrong_sum_is_an_amount_mismatch():
    result = control_movement(_movement(), [_row(po="A"), _row(po="B", amount="49.99")])

    assert result.status == CTRL_AMOUNT_MISMATCH
    assert result.delta_amount == Decimal("-0.01")


def test_a_batch_not_settled_yet_is_told_apart_from_a_real_gap():
    """RRS/O batches are listed at 0.00 and priced a day or two later — 9 rows
    of the workbook did exactly that over the six daily extracts. Nothing to
    arbitrate: the next extract fixes it.
    """
    result = control_movement(
        _movement(count=2, amount="0.00"), [_row(po="A"), _row(po="B")]
    )

    assert result.status == CTRL_NOT_SETTLED
    assert result.found_amount == Decimal("100.00")
    assert result.expected_amount == Decimal("0.00")


def test_a_movement_booked_at_zero_with_no_row_stays_not_found():
    assert control_movement(_movement(amount="0.00"), []).status == CTRL_NOT_FOUND


def test_no_row_at_all_is_not_found():
    result = control_movement(_movement(), [])

    assert result.status == CTRL_NOT_FOUND
    assert result.found_count == 0
    assert result.found_amount == Decimal("0.00")


def test_rounding_is_done_at_the_cent():
    result = control_movement(
        _movement(count=3, amount="100.00"),
        [_row(po="A", amount="33.333"), _row(po="B", amount="33.333"),
         _row(po="C", amount="33.334")],
    )

    assert result.found_amount == Decimal("100.00")
    assert result.status == CTRL_OK


def test_a_msgid_listed_twice_in_the_workbook_is_flagged():
    """Two movements claiming the same rows would each take them whole."""
    movements = [
        _movement(msgid="M1"), _movement(msgid="M1"),
        _movement(msgid="M2", count=1, amount="100.00"),
    ]
    index = {"M1": [_row(po="A"), _row(po="B")], "M2": [_row(msgid="M2", po="C", amount="100.00")]}

    results, duplicates = control_all(movements, index)

    assert duplicates == ["M1"]
    assert [r.status for r in results] == [
        CTRL_DUPLICATE_MOVEMENT, CTRL_DUPLICATE_MOVEMENT, CTRL_OK,
    ]


def test_files_of_a_msgid_spread_over_two_dumps():
    result = control_movement(
        _movement(),
        [_row(po="A", file_name="return_o.csv"), _row(po="B", file_name="reject_o.csv")],
    )

    assert result.files == ["reject_o.csv", "return_o.csv"]
    assert result.status == CTRL_OK
