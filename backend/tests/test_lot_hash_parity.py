"""Locks the member↔entry hash contract (Finacle Batch Booking True).

A movement_lot_member row must produce EXACTLY the same finacle source_hash as
the reconciliation entry pushed for the same movement, or the lot UI can no
longer resolve member statuses. The entry path is
``tasks._to_parsed(FinacleEntryIn) → ParsedEntry.compute_finacle_hash`` — these
tests run the REAL entry path against ``lot_service.member_to_source_hash``.

For a GHOST member the contract has a second leg: apply_lot_batch must anchor
the hash on the claim group's CANONICAL parent (via movement_split), exactly
like split_service anchored the ghost entry — or member and entry drift apart
whenever a later run re-emits a group off a different parent.

DB-free: imports never touch app.main (which connects to Postgres at import).
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from app.api.v1.endpoints.tasks import FinacleEntryIn, _to_parsed
from app.repositories.movement_lot_repository import movement_lot_repository
from app.repositories.movement_split_repository import movement_split_repository
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
    external_ref differs (KEY:<claim>~<tags>, the group's identity). Nothing
    special is needed for it to be addressable."""
    real = _member_hash(external_ref="S123456789")
    ghost = _member_hash(external_ref="KEY:PTEL-X~c386eba7~f1a2b3c4d5")
    assert ghost != real
    # Stable, and distinct per bucket suffix.
    assert ghost == _member_hash(external_ref="KEY:PTEL-X~c386eba7~f1a2b3c4d5")
    assert ghost != _member_hash(external_ref="KEY:PTEL-X~c386eba7~0000000000")


class _FakeSession:
    def commit(self):
        pass

    def rollback(self):
        pass


def test_ghost_member_hash_anchors_on_the_groups_canonical(monkeypatch):
    """The wire never carries a hash: split_service hashed the ghost entry on
    the CANONICAL parent's account/dates, so apply_lot_batch must derive the
    member's hash from the same anchor — not from the wire fields — or the
    member and its entry stop joining on source_hash."""
    canonical = SimpleNamespace(
        claim_key_type="MSGID", claim_key_value="PTEL-X",
        source_hash="f" * 64, external_ref="S0-CANONICAL",
        account="0010130015001",
        value_date=datetime(2026, 6, 15, tzinfo=timezone.utc),
        operation_date=datetime(2026, 6, 15, tzinfo=timezone.utc),
    )
    upserted = {}

    monkeypatch.setattr(
        movement_split_repository, "resolve_group_canonicals",
        lambda db, *, flow_source_id, claims: {("MSGID", "PTEL-X"): canonical},
    )
    monkeypatch.setattr(
        movement_lot_repository, "create_lots",
        lambda db, *, flow_id, flow_source_id, buckets: len(buckets),
    )
    monkeypatch.setattr(
        movement_lot_repository, "lot_currencies",
        lambda db, *, lot_ids: {lid: "EUR" for lid in lot_ids},
    )

    def _upsert_members(db, rows):
        upserted["rows"] = list(rows)
        return len(rows), 0, {r["source_hash"]: i for i, r in enumerate(rows)}

    monkeypatch.setattr(movement_lot_repository, "upsert_members", _upsert_members)
    monkeypatch.setattr(movement_lot_repository, "insert_keys", lambda db, rows: len(rows))
    monkeypatch.setattr(
        movement_lot_repository, "sync_lot_currencies", lambda db, *, lot_ids: None
    )
    monkeypatch.setattr(
        movement_lot_repository, "sync_synthetic_only", lambda db, *, lot_ids: None
    )

    ghost_member = SimpleNamespace(
        lot_id="11111111-1111-4111-8111-111111111111",
        movement_type="NDGB",
        external_ref="KEY:PTEL-X~c386eba7~f1a2b3c4d5",
        account="0010130015001",
        currency="EUR",
        amount=Decimal("-700.00"),
        direction="debit",
        # Wire dates = the RUN's canonical, deliberately different from stored.
        value_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        operation_date=datetime(2026, 7, 1, tzinfo=timezone.utc),
        transaction_particulars=None,
        ref_no=None,
        remarks_1="PTEL-X",
        claim_key_type="msgid",   # folded by the service
        claim_key_value="ptel-x",
        payment_count=2,
        keys=[],
    )
    lot_service.apply_lot_batch(
        _FakeSession(),
        flow=SimpleNamespace(id=FLOW_ID),
        source=SimpleNamespace(id=7),
        lots=[],
        members=[ghost_member],
    )

    row = upserted["rows"][0]
    assert row["split_parent_hash"] == canonical.source_hash
    assert row["value_date"] == canonical.value_date
    assert row["source_hash"] == lot_service.member_to_source_hash(
        flow_id=FLOW_ID, external_ref=ghost_member.external_ref,
        account=canonical.account, value_date=canonical.value_date,
        operation_date=canonical.operation_date,
    )
    # NOT what the wire fields alone would have produced.
    assert row["source_hash"] != lot_service.member_to_source_hash(
        flow_id=FLOW_ID, external_ref=ghost_member.external_ref,
        account=ghost_member.account, value_date=ghost_member.value_date,
        operation_date=ghost_member.operation_date,
    )


def test_derive_lot_status_truth_table():
    derive = lot_service.derive_lot_status
    assert derive(lot_status="merged", member_count=3, pending_count=0, matched_count=3) == "merged"
    assert derive(lot_status="active", member_count=3, pending_count=0, matched_count=3) == "matched"
    assert derive(lot_status="active", member_count=3, pending_count=1, matched_count=2) == "pending"
    assert derive(lot_status="active", member_count=3, pending_count=3, matched_count=0) == "pending"
    # Empty lot (members not pushed yet) is never "matched".
    assert derive(lot_status="active", member_count=0, pending_count=0, matched_count=0) == "pending"
