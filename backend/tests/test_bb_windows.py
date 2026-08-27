"""Unit tests for the per-key MSGID fan-out windows (pure functions, no I/O).

The property under test: a MessageID only claims payments contemporaneous with
the movements CARRYING it this run. An unbounded label ('LUXEMBOURG') dragged
17,96 Md€ of multi-year history into 2,17 Md€ of booked movements — ~19 000
single-ghost lots and a dashboard at 20 Md€ pending IN.
"""
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

DAGS_DIR = Path(__file__).resolve().parents[2] / "shared" / "dags"
if not DAGS_DIR.is_dir():
    pytest.skip(
        "shared/dags not mounted in this environment (backend-only container)",
        allow_module_level=True,
    )
sys.path.insert(0, str(DAGS_DIR))

from reco_datamart_bb import (  # noqa: E402
    OPEN_WINDOW,
    BBLookupInputs,
    _descriptor,
    _movement_date,
    _widen,
    collect_lookup_inputs,
    merge_msgid_windows,
    window_bounds,
)

# ---------------------------------------------------------------------------
# _movement_date — every shape a row can arrive in
# ---------------------------------------------------------------------------

def test_movement_date_reads_raw_and_app_shapes():
    # Raw std.Movement: pyodbc hands DATE/DATETIME objects.
    assert _movement_date({"ValueDate": datetime(2026, 7, 29, 14, 5)}) == date(2026, 7, 29)
    assert _movement_date({"ValueDate": date(2026, 7, 29)}) == date(2026, 7, 29)
    # TransactionDate is the documented fallback (same as movement_row_to_entry).
    assert _movement_date({"TransactionDate": "2026-07-29"}) == date(2026, 7, 29)
    # App entries (unresolved retries): ISO strings, with or without tz.
    assert _movement_date({"value_date": "2026-07-06T00:00:00+00:00"}) == date(2026, 7, 6)
    assert _movement_date({"value_date": "2026-07-06 00:00:00+00"}) == date(2026, 7, 6)
    # Nothing usable → None, never a crash.
    assert _movement_date({}) is None
    assert _movement_date({"ValueDate": "garbage"}) is None


def test_widen_grows_and_tolerates_undated():
    windows = {}
    _widen(windows, "K", None)
    assert windows == {"K": [None, None]}  # key registered, window empty
    _widen(windows, "K", date(2026, 7, 10))
    _widen(windows, "K", date(2026, 7, 3))
    _widen(windows, "K", date(2026, 7, 20))
    _widen(windows, "K", None)  # a dateless carrier never shrinks the window
    assert windows == {"K": [date(2026, 7, 3), date(2026, 7, 20)]}


# ---------------------------------------------------------------------------
# collect_lookup_inputs — the windows come from the carriers
# ---------------------------------------------------------------------------

def _ndgb(msgid, day):
    return {
        "TransactionParticulars": f"NDGB##x##{msgid}",
        "Remarks_1": msgid,
        "ValueDate": day,
    }


def test_collect_accumulates_one_window_per_msgid():
    acc = BBLookupInputs()
    collect_lookup_inputs(
        [
            _ndgb("LUXEMBOURG", datetime(2026, 7, 3)),
            _ndgb("LUXEMBOURG", datetime(2026, 8, 5)),
            _ndgb("PTEL-X", datetime(2026, 7, 21)),
        ],
        acc,
    )
    assert acc.ndgb_msgids == {
        "LUXEMBOURG": [date(2026, 7, 3), date(2026, 8, 5)],
        "PTEL-X": [date(2026, 7, 21), date(2026, 7, 21)],
    }


def test_unresolved_retries_widen_their_keys_window():
    """An old movement re-processed must reach ITS payments: the app entry's
    value_date (ISO string) widens the window back to cover it."""
    acc = BBLookupInputs()
    collect_lookup_inputs([_ndgb("LUXEMBOURG", datetime(2026, 8, 5))], acc)
    collect_lookup_inputs(
        [{"transaction_particulars": "NDGB##x##LUXEMBOURG",
          "remarks_1": "LUXEMBOURG",
          "value_date": "2026-05-22T00:00:00+00:00"}],
        acc,
    )
    assert acc.ndgb_msgids["LUXEMBOURG"] == [date(2026, 5, 22), date(2026, 8, 5)]


def test_descriptors_carry_the_date_pass1_needs():
    row = {"TransactionParticulars": "NDGB##x##K", "Remarks_1": "K",
           "ValueDate": datetime(2026, 7, 29)}
    acc = BBLookupInputs()
    collect_lookup_inputs([_descriptor(row)], acc)
    assert acc.ndgb_msgids == {"K": [date(2026, 7, 29), date(2026, 7, 29)]}


def test_ip_aggregates_register_their_refs_window():
    acc = BBLookupInputs()
    collect_lookup_inputs(
        [{"TransactionParticulars": "BKRTP/TXREF123/x",
          "ValueDate": datetime(2026, 7, 15)}],
        acc,
    )
    assert acc.instant_txn_refs == {"TXREF123": [date(2026, 7, 15), date(2026, 7, 15)]}


# ---------------------------------------------------------------------------
# merge_msgid_windows — IP aggregates widen the group they resolved to
# ---------------------------------------------------------------------------

def test_merge_widens_a_shared_msgid_and_adds_a_txnref_only_one():
    merged = merge_msgid_windows(
        {"MSGA": [date(2026, 7, 10), date(2026, 7, 10)]},
        {"REF1": [date(2026, 7, 1), date(2026, 7, 1)],
         "REF2": [date(2026, 7, 20), date(2026, 7, 20)],
         "REF3": [None, None]},
        {"REF1": "MSGA", "REF2": "MSGB", "REF3": "MSGC", "REF4": None},
    )
    assert merged["MSGA"] == [date(2026, 7, 1), date(2026, 7, 10)]
    assert merged["MSGB"] == [date(2026, 7, 20), date(2026, 7, 20)]
    assert merged["MSGC"] == [None, None]  # claimed, window unknown
    assert "REF4" not in merged and None not in merged


def test_merge_does_not_mutate_its_inputs():
    ndgb = {"MSGA": [date(2026, 7, 10), date(2026, 7, 10)]}
    merge_msgid_windows(ndgb, {"R": [date(2026, 1, 1), date(2026, 1, 1)]}, {"R": "MSGA"})
    assert ndgb == {"MSGA": [date(2026, 7, 10), date(2026, 7, 10)]}


# ---------------------------------------------------------------------------
# window_bounds — the margins, and the loud fallback
# ---------------------------------------------------------------------------

def test_window_bounds_applies_margins_exclusive_upper():
    dmin, dmax = window_bounds(
        [date(2026, 7, 3), date(2026, 8, 5)], lookback_days=15, lookahead_days=5
    )
    assert dmin == date(2026, 6, 18)
    assert dmax == date(2026, 8, 11)  # max + 5 + 1 (exclusive bound)


def test_a_key_without_dates_gets_the_open_window():
    """Unbounded is today's behaviour; silently dropping a key's payments is
    not. The caller logs these."""
    assert window_bounds(None) == OPEN_WINDOW
    assert window_bounds([None, None]) == OPEN_WINDOW


def test_the_luxembourg_case_a_2025_payment_falls_outside():
    """The regression this whole change exists for: the July-2026 window of the
    LUXEMBOURG carriers must exclude the label's 2025 treasury payments
    (C5… PaymentNumbers, 50-99 M€ each) that used to become single-ghost lots."""
    dmin, dmax = window_bounds([date(2026, 7, 3), date(2026, 8, 5)])
    in_window = date(2026, 7, 29)     # the pacs 26072905552301130 batch
    historic = date(2025, 6, 11)      # C5F11XM08J… treasury payment
    assert dmin <= in_window < dmax
    assert not (dmin <= historic < dmax)
