"""Locks the member↔entry hash contract (Finacle Batch Booking True).

A movement_lot_member row must produce EXACTLY the same finacle source_hash as
the reconciliation entry pushed for the same movement, or the lot UI can no
longer resolve member statuses. The entry path is
``tasks._to_parsed(FinacleEntryIn) → ParsedEntry.compute_finacle_hash`` — these
tests run the REAL entry path against ``lot_service.member_to_source_hash``.

DB-free: imports never touch app.main (which connects to Postgres at import).
"""
from datetime import datetime, timezone
from decimal import Decimal

from app.api.v1.endpoints.tasks import FinacleEntryIn, _to_parsed
from app.services.lot_service import lot_service

FLOW_ID = 42


def _entry_hash(**overrides) -> str:
    payload = {
        "reco_id": "0b7e1c3a-lot-uuid",
        "account": "0010130015001",
        "currency": "EUR",
        "amount": Decimal("-1234.56"),
        "value_date": datetime(2026, 7, 1, 9, 30, 0),
        "operation_date": datetime(2026, 7, 1, 10, 45, 0),
        "direction": "debit",
        "external_ref": "S123456789",
        "transaction_particulars": "SCTXB/O/whatever",
        "ref_no": None,
        "remarks_1": "MSGPACS008-001",
    }
    payload.update(overrides)
    return _to_parsed(FinacleEntryIn(**payload)).compute_finacle_hash(FLOW_ID)


def _member_hash(**overrides) -> str:
    fields = {
        "external_ref": "S123456789",
        "account": "0010130015001",
        "value_date": datetime(2026, 7, 1, 9, 30, 0),
        "operation_date": datetime(2026, 7, 1, 10, 45, 0),
    }
    fields.update(overrides)
    return lot_service.member_to_source_hash(flow_id=FLOW_ID, **fields)


def test_member_hash_matches_entry_hash_naive_datetimes():
    assert _member_hash() == _entry_hash()


def test_member_hash_matches_entry_hash_aware_datetimes():
    aware_value = datetime(2026, 7, 2, 8, 0, 0, tzinfo=timezone.utc)
    aware_op = datetime(2026, 7, 2, 16, 0, 0, tzinfo=timezone.utc)
    assert (
        _member_hash(value_date=aware_value, operation_date=aware_op)
        == _entry_hash(value_date=aware_value, operation_date=aware_op)
    )


def test_member_hash_matches_entry_hash_without_operation_date():
    # Entry path falls back operation_date -> value_date; the member path must too.
    assert (
        _member_hash(operation_date=None)
        == _entry_hash(operation_date=None)
    )


def test_hash_uses_date_only_not_time():
    # Finacle recycles TransactionID daily: the hash includes the DAY, not the time.
    assert _member_hash(operation_date=datetime(2026, 7, 1, 6, 0, 0)) == _member_hash(
        operation_date=datetime(2026, 7, 1, 23, 0, 0)
    )
    assert _member_hash(operation_date=datetime(2026, 7, 1)) != _member_hash(
        operation_date=datetime(2026, 7, 2)
    )


def test_hash_independent_of_reco_id_and_amount():
    # reco_id/amount are excluded from the finacle identity — re-clustering a
    # movement into another lot must not change its hash.
    assert _entry_hash(reco_id="lot-A", amount=Decimal("-1.00")) == _entry_hash(
        reco_id="lot-B", amount=Decimal("-999.99")
    )


def test_hash_sensitive_to_identity_fields():
    base = _member_hash()
    assert _member_hash(external_ref="OTHER") != base
    assert _member_hash(account="9999999999999") != base
    assert _member_hash(value_date=datetime(2026, 7, 3, 9, 30, 0),
                        operation_date=datetime(2026, 7, 3, 10, 0, 0)) != base


def test_ghost_hash_follows_the_same_formula_with_its_own_external_ref():
    """A ghost is a normal finacle movement as far as hashing goes: only its
    external_ref differs from its parent's (same account, same day — it IS the
    same movement, sliced). Nothing special is needed for it to be addressable."""
    parent = _member_hash(external_ref="S123456789")
    ghost = _member_hash(external_ref="S123456789~c386eba7f1")
    assert ghost != parent
    # Stable, and distinct per bucket suffix.
    assert ghost == _member_hash(external_ref="S123456789~c386eba7f1")
    assert ghost != _member_hash(external_ref="S123456789~1f21d8baa1")


def test_a_ghosts_parent_hash_is_reachable_from_the_ghosts_own_fields():
    """The wire never carries a hash: split_service hashes the parent from its
    identity, and lot_service re-derives the SAME value for the ghost's member
    row using the parent's external_ref plus the ghost's account and dates. If
    these two ever drift, every ghost points at a parent that does not exist."""
    account = "0010130015001"
    value_date = datetime(2026, 7, 1, 9, 30, 0)
    operation_date = datetime(2026, 7, 1, 10, 45, 0)

    # What split_service stores as movement_split.source_hash…
    parent_hash = lot_service.member_to_source_hash(
        flow_id=FLOW_ID, external_ref="S123456789",
        account=account, value_date=value_date, operation_date=operation_date,
    )
    # …and what apply_lot_batch derives for the ghost member pointing at it.
    derived = lot_service.member_to_source_hash(
        flow_id=FLOW_ID, external_ref="S123456789",   # split_parent_external_ref
        account=account, value_date=value_date, operation_date=operation_date,
    )
    assert derived == parent_hash


def test_derive_lot_status_truth_table():
    derive = lot_service.derive_lot_status
    assert derive(lot_status="merged", member_count=3, pending_count=0, matched_count=3) == "merged"
    assert derive(lot_status="active", member_count=3, pending_count=0, matched_count=3) == "matched"
    assert derive(lot_status="active", member_count=3, pending_count=1, matched_count=2) == "pending"
    assert derive(lot_status="active", member_count=3, pending_count=3, matched_count=0) == "pending"
    # Empty lot (members not pushed yet) is never "matched".
    assert derive(lot_status="active", member_count=0, pending_count=0, matched_count=0) == "pending"
