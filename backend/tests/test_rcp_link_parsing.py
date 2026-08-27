"""Parsing of the RCP link extracts — pure, no DB.

The two file families are hostile in different ways: the workbook ships the
header typo ``num_0f_records`` (zero, not 'o'), and the dumps come in two
casings with two different amount columns, one of which (the reject dump) has a
``msgid`` column that is present but empty on every row. Everything asserted
here was observed on the 2026-08-12 extract.
"""
import io
from decimal import Decimal

from openpyxl import Workbook

from app.services.rcp_link_parser import (
    build_dump_index,
    parse_decimal,
    parse_dump_file,
    parse_link_workbook,
    sniff_delimiter,
)


def _workbook(rows, headers=None):
    headers = headers or [
        "spdate", "paysysid", "serviceid", "direction",
        "num_0f_records", "settlementamt", "trandate", "tranid", "msgid",
    ]
    book = Workbook()
    sheet = book.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_link_workbook_keeps_rcp_and_drops_ncp():
    content = _workbook([
        ["2026-08-06", "SCTXB", "NCP", "I", 565, 1622904.54, "2026-08-06", "PF1", "BLK1"],
        ["2026-07-17", "SCTXB", "RCP", "I", 1, 5000, "2026-07-17", "PF2", "BLK2"],
        ["2026-07-16", "SCTXB", "RCP", "O", 5, 648.2, "2026-07-16", "PF3", "26071"],
    ])
    movements, report = parse_link_workbook(content, "link.xlsx")

    assert [m.msgid for m in movements] == ["BLK2", "26071"]
    assert movements[0].num_records == 1
    assert movements[0].settlement_amount == Decimal("5000.00")
    assert movements[1].direction == "O"
    assert report.rows == 3 and report.error == ""


def test_link_workbook_accepts_the_untyped_header_too():
    content = _workbook(
        [["2026-07-17", "SCTXB", "RCP", "I", 3, 100, "2026-07-17", "PF2", "BLK2"]],
        headers=[
            "spdate", "paysysid", "serviceid", "direction",
            "num_of_records", "settlementamt", "trandate", "tranid", "msgid",
        ],
    )
    movements, _ = parse_link_workbook(content, "link.xlsx")
    assert movements[0].num_records == 3


def test_link_workbook_without_msgid_column_is_reported_not_crashed():
    content = _workbook([["2026-07-17", "SCTXB", "RCP", "I", 1, 5000, "x", "PF2"]],
                        headers=["spdate", "paysysid", "serviceid", "direction",
                                 "num_0f_records", "settlementamt", "trandate", "tranid"])
    movements, report = parse_link_workbook(content, "link.xlsx")
    assert movements == []
    assert report.has_msgid_column is False
    assert "msgid" in report.error


def test_dump_camelcase_reads_the_original_amount_not_the_returned_one():
    """The float books what the payment was worth, not what came back.

    Observed on the 2026-08-20 extract (entitySrlNum 000009216272): an inward
    SDD return carries returnSttlmAmt 77.00 for an orgnlSttlmAmt of 74.00 — the
    3.00 interbank return fee — while the link workbook books 74.00. Taking the
    returned amount made the ghosts exceed the booking, and the commit refused
    the movement outright.
    """
    content = (
        b"entitySrlNum;serviceId;serviceType;status;origEntityId;rtrRsnCd;"
        b"returnSttlmAmt;instructedAmt;orgnlSttlmAmt;pmsgid\n"
        b"000009216272;RCC;I;R;000008813744;;77.00;74.00;74.00;BLK2026208012991\n"
        b"000008957597;RCC;I;R;000008957555;AC04;49.00;49.00;49.00;BLK2026208012991\n"
    )
    rows, report = parse_dump_file(content, "returnevent_sddxb_o.csv")

    assert report.amount_column == "orgnlsttlmamt"
    assert report.delimiter == ";"
    assert report.rows == 2 and report.rows_with_msgid == 2
    assert [r.orig_entity_id for r in rows] == ["000008813744", "000008957555"]
    assert sum((r.amount for r in rows), Decimal("0")) == Decimal("123.00")


def test_a_recall_dump_has_its_own_amount_columns():
    """recallevent_* carries none of the return/reject amounts — before they
    were listed, its 3 WCC movements controlled at 0.00."""
    content = (
        b"entitysrlnum;msgid;serviceid;origentityid;recalledinstdamt;"
        b"recallsttlmamt;pmsgid\n"
        b"000009000001;SWR1;WCC;000008111111;120.50;120.50;BLK2026216008041\n"
    )
    rows, report = parse_dump_file(content, "recallevent_sddxb_i.csv")

    assert report.amount_column == "recalledinstdamt"
    assert rows[0].amount == Decimal("120.50")
    # Both key columns are present in this family — pmsgid is the batch.
    assert rows[0].msgid == "BLK2026216008041"


def test_pmsgid_wins_over_msgid_when_a_file_carries_both():
    """Finacle renamed the key on 2026-08-14. Files shipped before still say
    msgid, the recall dump says both, and only pmsgid names the batch."""
    both = (
        b"entitysrlnum;msgid;origentityid;orgnlsttlmamt;pmsgid\n"
        b"SRL1;NOT-THE-BATCH;PO1;10.00;BLK1\n"
    )
    legacy = (
        b"entitysrlnum;origentityid;orgnlsttlmamt;msgid\n"
        b"SRL2;PO2;10.00;BLK2\n"
    )
    assert parse_dump_file(both, "recall.csv")[0][0].msgid == "BLK1"
    assert parse_dump_file(legacy, "old.csv")[0][0].msgid == "BLK2"


def test_a_wrapped_psql_export_is_unwrapped_not_lost():
    """extract/20260820/returnevent_sddxb_O.csv: the export shipped one quoted
    column per row. Read as-is it has no key column and its 1 633 rows vanish."""
    content = (
        b'"?column?"\n'
        b'"entitySrlNum;origEntityId;orgnlSttlmAmt;pmsgid"\n'
        b'"000008525574;000008196031;104.00;BLK2026196006912"\n'
        b'"000008053737;C6F29XM0GN000034;73.00;BLK2026187001740"\n'
    )
    rows, report = parse_dump_file(content, "returnevent_sddxb_o.csv")

    assert report.rows == 2 and report.rows_with_msgid == 2
    assert report.amount_column == "orgnlsttlmamt"
    assert [r.msgid for r in rows] == ["BLK2026196006912", "BLK2026187001740"]
    assert sum((r.amount for r in rows), Decimal("0")) == Decimal("177.00")
    # Recovered, but the operator is told the file was malformed.
    assert "?column?" in report.error


def test_dump_lowercase_falls_back_to_original_amount():
    content = (
        b"entitysrlnum;paysysid;serviceid;servicetype;status;origentityid;rsncd;"
        b"orgnlsttlmamt;msgid\n"
        b"ffa0cdbd;SCTXB;RRS;O;R;C5F11SRA003A0065;MS03;100.0;BLK9\n"
    )
    rows, report = parse_dump_file(content, "reject_o.csv")

    assert report.amount_column == "orgnlsttlmamt"
    assert rows[0].amount == Decimal("100.00")
    assert rows[0].reason_code == "MS03"


def test_dump_with_empty_msgid_column_yields_nothing_and_says_so():
    """The 2026-08-12 reject dump: 17 195 rows, not one usable."""
    content = (
        b"entitysrlnum;serviceid;origentityid;orgnlsttlmamt;msgid\n"
        b"ffa0cdbd;RRS;C5F11SRA003A0065;100.0;\n"
        b"4c36b0dd;RRS;C5F19XM05Y000001;600.0;\n"
    )
    rows, report = parse_dump_file(content, "reject_o.csv")

    assert rows == []
    assert report.rows == 2
    assert report.rows_with_msgid == 0
    assert report.has_msgid_column is True  # the column exists, the values do not


def test_dump_index_deduplicates_across_files():
    """The extracts overlap; a row counted twice would inflate both the control
    and the ghost it feeds."""
    header = b"entitySrlNum;origEntityId;orgnlSttlmAmt;msgid\n"
    first, _ = parse_dump_file(header + b"SRL1;PO1;70.00;M1\nSRL2;PO2;30.00;M1\n", "a.csv")
    second, _ = parse_dump_file(header + b"SRL2;PO2;30.00;M1\nSRL3;PO3;10.00;M2\n", "b.csv")

    index, duplicates = build_dump_index(first + second)

    assert len(index["M1"]) == 2
    assert sum((r.amount for r in index["M1"]), Decimal("0")) == Decimal("100.00")
    assert len(index["M2"]) == 1
    # Said out loud: six cumulative daily extracts collapse 207 202 rows into
    # 40 519, and an operator must see that rather than infer it.
    assert duplicates == 1


def test_sniff_delimiter_and_decimals():
    assert sniff_delimiter("a;b;c") == ";"
    assert sniff_delimiter("a,b,c") == ","
    assert sniff_delimiter("single") == ";"
    assert parse_decimal("1 234,56") == Decimal("1234.56")
    assert parse_decimal("1,234.56") == Decimal("1234.56")
    assert parse_decimal(648.2) == Decimal("648.2")
    assert parse_decimal("") is None


def test_the_four_return_services_are_kept_and_ncp_dropped():
    """RCP alone was not enough: RCC, RRS and WCC are return/reject batches too.
    NCP stays out — those payment batches settle normally."""
    content = _workbook([
        ["2026-07-17", "SCTXB", "RCP", "I", 1, 10, "d", "PF1", "M1"],
        ["2026-07-17", "SCTXB", "RCC", "O", 1, 20, "d", "PF2", "M2"],
        ["2026-07-17", "SCTXB", "RRS", "O", 1, 30, "d", "PF3", "M3"],
        ["2026-07-17", "SCTXB", "WCC", "O", 1, 40, "d", "PF4", "M4"],
        ["2026-07-17", "SCTXB", "NCP", "I", 5, 50, "d", "PF5", "M5"],
    ])

    movements, report = parse_link_workbook(content, "link.xlsx")

    assert [(m.msgid, m.service_id) for m in movements] == [
        ("M1", "RCP"), ("M2", "RCC"), ("M3", "RRS"), ("M4", "WCC"),
    ]
    # Every service is counted, kept or not — a type nobody expected is visible.
    assert report.services == {"RCP": 1, "RCC": 1, "RRS": 1, "WCC": 1, "NCP": 1}


def test_the_service_set_can_be_narrowed():
    content = _workbook([
        ["2026-07-17", "SCTXB", "RCP", "I", 1, 10, "d", "PF1", "M1"],
        ["2026-07-17", "SCTXB", "RRS", "O", 1, 30, "d", "PF3", "M3"],
    ])

    movements, report = parse_link_workbook(content, "link.xlsx", services=["RRS"])

    assert [m.msgid for m in movements] == ["M3"]
    assert report.services == {"RCP": 1, "RRS": 1}
