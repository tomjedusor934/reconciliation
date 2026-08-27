"""Folding the daily extracts into one usable set.

Finacle exports the whole return/reject table every day, so six days of extracts
are six copies of the same rows plus what happened since — 207 202 lines for
40 519 real ones, 83 Mo, over nginx's 64 Mo upload limit. What matters here is
that folding them loses nothing: the key a later day fills in is recovered, a
row is counted once, and a genuine disagreement between two days is reported
instead of arbitrated.
"""
from pathlib import Path

import pytest

from scripts.rcp_merge_extracts import Family, family_of, merge, read_rows

RETURN_HEADER = (
    "entitySrlNum;paysysId;serviceId;origEntityId;returnSttlmAmt;orgnlSttlmAmt;{key}"
)


def _write(tmp_path: Path, day: str, name: str, header: str, rows) -> Path:
    folder = tmp_path / day
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "name, expected",
    [
        ("rejectevent_sctxb_0.csv", "rejectevent_sctxb_o"),   # 2026-08-13: digit zero
        ("rejectevent_sctxb_O.csv", "rejectevent_sctxb_o"),
        ("returnevent_sddxb_I.csv", "returnevent_sddxb_i"),
        ("returnevent_sddxb_i.csv", "returnevent_sddxb_i"),
        ("recallevent_sddxb.csv", "recallevent_sddxb"),
    ],
)
def test_one_table_is_one_family_whatever_the_day_spelled_it(name, expected):
    assert family_of(Path(f"20260813/{name}")) == expected


def test_a_key_filled_in_later_is_recovered(tmp_path):
    """The 2026-08-13 export left msgid empty on rows the 2026-08-14 one names
    in pmsgid — that is the whole reason a single day answers less."""
    old = _write(
        tmp_path, "20260813", "returnevent_sddxb_o.csv",
        RETURN_HEADER.format(key="msgid"),
        ["SRL1;SDDXB;RCC;PO1;77.00;74.00;"],
    )
    new = _write(
        tmp_path, "20260814", "returnevent_sddxb_O.csv",
        RETURN_HEADER.format(key="pmsgid"),
        ["SRL1;SDDXB;RCC;PO1;77.00;74.00;BLK1", "SRL2;SDDXB;RCC;PO2;10.00;10.00;BLK1"],
    )

    family = merge([old, new])["returnevent_sddxb_o"]

    assert family.rows_read == 3
    assert len(family.kept) == 2 and family.duplicates == 1
    assert family.with_key == 2
    assert family.conflicts == []


def test_the_merged_file_names_the_key_column_once(tmp_path):
    old = _write(
        tmp_path, "20260813", "returnevent_sddxb_o.csv",
        RETURN_HEADER.format(key="msgid"), ["SRL1;SDDXB;RCC;PO1;77.00;74.00;BLK9"],
    )
    new = _write(
        tmp_path, "20260814", "returnevent_sddxb_O.csv",
        RETURN_HEADER.format(key="pmsgid"), ["SRL2;SDDXB;RCC;PO2;10.00;10.00;BLK1"],
    )
    family = merge([old, new])["returnevent_sddxb_o"]

    out = tmp_path / "merged.csv"
    assert family.write(out) == 2
    headers, rows, _ = read_rows(out)

    assert headers[-1] == "pmsgid"
    assert "msgid" not in headers[:-1]
    assert sorted(row[-1] for row in rows) == ["BLK1", "BLK9"]


def test_two_days_disagreeing_is_reported_not_arbitrated(tmp_path):
    """Nothing here can know which day is right, so nothing here picks."""
    old = _write(
        tmp_path, "20260813", "returnevent_sddxb_o.csv",
        RETURN_HEADER.format(key="pmsgid"), ["SRL1;SDDXB;RCC;PO1;77.00;74.00;BLK1"],
    )
    new = _write(
        tmp_path, "20260814", "returnevent_sddxb_O.csv",
        RETURN_HEADER.format(key="pmsgid"), ["SRL1;SDDXB;RCC;PO9;77.00;99.00;BLK2"],
    )

    conflicts = merge([old, new])["returnevent_sddxb_o"].conflicts

    assert len(conflicts) == 3
    assert any("clé" in c for c in conflicts)
    assert any("origEntityId" in c for c in conflicts)
    assert any("montant" in c for c in conflicts)


def test_a_wrapped_export_merges_like_any_other(tmp_path):
    """extract/20260820/returnevent_sddxb_O.csv shipped one quoted column per
    row; unmerged and unrepaired, its 1 633 rows are simply absent."""
    wrapped = _write(
        tmp_path, "20260820", "returnevent_sddxb_O.csv", '"?column?"',
        ['"SRL3;SDDXB;RCC;PO3;12.00;12.00;BLK3"'],
    )
    wrapped.write_text(
        '"?column?"\n'
        f'"{RETURN_HEADER.format(key="pmsgid")}"\n'
        '"SRL3;SDDXB;RCC;PO3;12.00;12.00;BLK3"\n',
        encoding="utf-8",
    )

    family = merge([wrapped])["returnevent_sddxb_o"]

    assert family.with_key == 1
    assert next(iter(family.kept.values()))["key"] == "BLK3"


def test_a_family_no_row_can_be_matched_on_is_flagged_unusable(tmp_path):
    """rejectevent_sctxb_*: 12 387 rows, key column present and empty on every
    single one, since the first extract."""
    path = _write(
        tmp_path, "20260820", "rejectevent_sctxb_O.csv",
        "entitysrlnum;serviceid;origentityid;orgnlsttlmamt;pmsgid",
        ["SRL1;RRS;PO1;10.00;", "SRL2;RRS;PO2;20.00;"],
    )

    family = merge([path])["rejectevent_sctxb_o"]

    assert len(family.kept) == 2
    assert family.with_key == 0
    assert family.usable is False


def test_a_row_without_a_serial_number_still_deduplicates(tmp_path):
    header = "origentityid;orgnlsttlmamt;pmsgid"
    old = _write(tmp_path, "20260813", "x_o.csv", header, ["PO1;10.00;BLK1"])
    new = _write(tmp_path, "20260814", "x_O.csv", header,
                 ["PO1;10.00;BLK1", "PO2;20.00;BLK1"])

    family = merge([old, new])["x_o"]

    assert len(family.kept) == 2 and family.duplicates == 1


def test_an_empty_export_is_skipped_not_fatal(tmp_path):
    empty = _write(tmp_path, "20260813", "returnevent_sddxb_o.csv", "", [])
    empty.write_text("", encoding="utf-8")

    assert merge([empty])["returnevent_sddxb_o"].files == []


def test_family_reports_what_it_read():
    family = Family("returnevent_sddxb_o")
    assert family.usable is False and family.rows_read == 0
