"""The split batch: registering a parent, materialising its ghosts, withdrawing
the real movement and reaping stale ghosts must all happen together.

Between any two of those steps the database says both the movement AND its
ghosts count — the double count the whole design exists to prevent. So the
ordering and the atomicity are locked here, DB-free: a fake Session records
commits and the repository is stubbed (app.main is never imported).
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.repositories.movement_split_repository import movement_split_repository
from app.services.lot_service import lot_service
from app.services.split_service import split_service

NOW = datetime(2026, 7, 1, 9, 30, 0, tzinfo=timezone.utc)


class _Flow:
    id = 42
    code = "float_account_outward"


class _Source:
    id = 7
    code = "finacle_db"


class _Child:
    def __init__(self, external_ref, lot_id, amount, payment_count=1):
        self.external_ref = external_ref
        self.lot_id = lot_id
        self.amount = Decimal(amount)
        self.direction = "debit" if Decimal(amount) < 0 else "credit"
        self.payment_count = payment_count
        self.bucket_kind = "PAIR"
        self.bucket_pacs008 = "PACS1"
        self.bucket_msgid = "MSGA"
        self.bucket_po = ""


class _Parent:
    def __init__(self, children, amount="-1000.00", payment_amount=None,
                 external_ref="S1", shared_key_movements=1):
        self.movement_type = "SCTXB"
        self.external_ref = external_ref
        self.account = "0010130015001"
        self.currency = "EUR"
        self.amount = Decimal(amount)
        self.direction = "debit"
        self.value_date = NOW
        self.operation_date = NOW
        self.transaction_particulars = "SCTXB/O/x"
        self.ref_no = None
        self.remarks_1 = "PACS1"
        self.event_type = "TR"
        self.transaction_id = "TX-1"
        self.payload_raw = {"MovementID": 1}
        self.payment_count = sum(c.payment_count for c in children)
        # Defaults to "std.Payment agrees with the accounting".
        self.payment_amount = Decimal(payment_amount if payment_amount is not None else amount)
        self.shared_key_movements = shared_key_movements
        self.children = children


class _FakeSession:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


@pytest.fixture()
def calls(monkeypatch):
    """Stub the repository, recording each call in order."""
    seen = {"order": [], "emarged": set()}

    def _upsert_parents(db, rows):
        seen["order"].append("upsert_parents")
        seen["parents"] = rows
        return len(rows), 0

    def _upsert_ghosts(db, rows):
        seen["order"].append("upsert_ghosts")
        seen["ghosts"] = rows
        return len(rows), 0, 0

    def _withdraw(db, *, parent_hashes):
        seen["order"].append("withdraw")
        seen["withdrawn"] = list(parent_hashes)
        return len(parent_hashes), set(seen["emarged"])

    def _flag(db, *, parent_hashes):
        seen["order"].append("flag_emarged")
        seen["flagged"] = list(parent_hashes)

    def _reap(db, *, expected):
        seen["order"].append("reap")
        seen["expected"] = list(expected)
        return 0

    monkeypatch.setattr(movement_split_repository, "upsert_parents", _upsert_parents)
    monkeypatch.setattr(movement_split_repository, "upsert_ghost_entries", _upsert_ghosts)
    monkeypatch.setattr(movement_split_repository, "withdraw_parent_movements", _withdraw)
    monkeypatch.setattr(movement_split_repository, "flag_emarged", _flag)
    monkeypatch.setattr(movement_split_repository, "reap_stale_children", _reap)
    return seen


def _apply(db, parents, run_id=99):
    return split_service.apply_split_batch(
        db, flow=_Flow(), source=_Source(), parents=parents, run_id=run_id
    )


def test_ghosts_exist_before_the_real_movement_is_withdrawn(calls):
    """Withdrawing first would leave the amount out of the reconciliation for
    the rest of the batch; both orders are wrong outside one transaction, but
    inside it this is the order that reads correctly if it ever crashes."""
    db = _FakeSession()
    _apply(db, [_Parent([_Child("S1~a", "lot-a", "-700.00"), _Child("S1~b", "lot-b", "-300.00")])])

    assert calls["order"] == ["upsert_parents", "upsert_ghosts", "withdraw", "reap"]
    assert db.committed == 1 and db.rolled_back == 0


def test_ghost_rows_carry_the_bucket_and_point_at_their_parent(calls):
    db = _FakeSession()
    _apply(db, [_Parent([_Child("S1~a", "lot-a", "-700.00", payment_count=2),
                         _Child("S1~b", "lot-b", "-300.00")])])

    parent_hash = calls["parents"][0]["source_hash"]
    ghosts = {g["external_ref"]: g for g in calls["ghosts"]}
    assert set(ghosts) == {"S1~a", "S1~b"}
    for ghost in ghosts.values():
        assert ghost["split_parent_hash"] == parent_hash
        assert ghost["status"] == "PENDING"
        assert ghost["ingestion_run_id"] == 99
        assert ghost["account"] == "0010130015001"
        assert ghost["payload_raw"]["split_of"] == "S1"
    assert ghosts["S1~a"]["amount"] == Decimal("-700.00")
    assert ghosts["S1~a"]["payload_raw"]["payment_count"] == 2
    assert ghosts["S1~a"]["reco_id"] == "lot-a"


def test_parent_hash_is_the_one_a_lot_member_will_derive(calls):
    """Same formula on both sides — this is what makes the ghost→parent link
    resolvable without any hash crossing the wire."""
    db = _FakeSession()
    _apply(db, [_Parent([_Child("S1~a", "lot-a", "-1000.00")])])

    assert calls["parents"][0]["source_hash"] == lot_service.member_to_source_hash(
        flow_id=_Flow.id, external_ref="S1", account="0010130015001",
        value_date=NOW, operation_date=NOW,
    )


def test_reaper_is_told_exactly_which_ghosts_still_exist(calls):
    """Anything outside this set belongs to a bucket that disappeared and would
    otherwise sit PENDING forever with a stale amount."""
    db = _FakeSession()
    _apply(db, [_Parent([_Child("S1~a", "lot-a", "-700.00"), _Child("S1~b", "lot-b", "-300.00")])])

    parent_hash = calls["parents"][0]["source_hash"]
    ghost_hashes = {g["source_hash"] for g in calls["ghosts"]}
    assert {p for p, _c in calls["expected"]} == {parent_hash}
    assert {c for _p, c in calls["expected"]} == ghost_hashes


def test_parent_row_records_what_the_children_add_up_to(calls):
    db = _FakeSession()
    _apply(db, [_Parent([_Child("S1~a", "lot-a", "-700.00", payment_count=2),
                         _Child("S1~b", "lot-b", "-300.00")])])

    parent = calls["parents"][0]
    assert parent["child_count"] == 2
    assert parent["child_amount"] == Decimal("-1000.00") == parent["amount"]
    assert parent["payment_count"] == 3
    assert parent["flow_source_id"] == _Source.id


def test_parent_row_records_the_payment_gap_and_the_shared_key(calls):
    """The gap between std.Payment and the accounting is stored on the movement.
    It used to be carried by a residual ghost — the door through which a 1,8 M€
    slice walked into a bucket."""
    db = _FakeSession()
    _apply(db, [_Parent([_Child("S1~a", "lot-a", "-1000.00")],
                        payment_amount="-990.00", shared_key_movements=184)])

    parent = calls["parents"][0]
    assert parent["payment_amount"] == Decimal("-990.00")
    assert parent["amount"] - parent["payment_amount"] == Decimal("-10.00")
    assert parent["shared_key_movements"] == 184
    # The gap changes nothing about the money placed in the buckets.
    assert parent["child_amount"] == parent["amount"]


def test_a_broken_conservation_is_logged_not_swallowed(calls, caplog):
    """The one error no downstream check can catch — the sums would simply be
    wrong everywhere — so it must be loud."""
    db = _FakeSession()
    with caplog.at_level("WARNING"):
        _apply(db, [_Parent([_Child("S1~a", "lot-a", "-700.00")])])  # 300 missing
    assert "children sum to" in caplog.text
    assert "-300.00" in caplog.text


def test_an_already_emarged_parent_is_flagged_and_not_withdrawn(calls):
    """Émargé history is never rewritten. The conflict is surfaced instead of
    being silently resolved one way or the other."""
    db = _FakeSession()
    parent = _Parent([_Child("S1~a", "lot-a", "-1000.00")])
    expected_hash = lot_service.member_to_source_hash(
        flow_id=_Flow.id, external_ref="S1", account="0010130015001",
        value_date=NOW, operation_date=NOW,
    )
    calls["emarged"] = {expected_hash}

    result = _apply(db, [parent])

    assert calls["flagged"] == [expected_hash]
    assert result["parents_emarged"] == 1
    assert db.committed == 1


def test_a_failure_rolls_the_whole_batch_back(calls, monkeypatch):
    def boom(db, *, parent_hashes):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(movement_split_repository, "withdraw_parent_movements", boom)
    db = _FakeSession()
    with pytest.raises(RuntimeError):
        _apply(db, [_Parent([_Child("S1~a", "lot-a", "-1000.00")])])
    assert db.committed == 0 and db.rolled_back == 1
