"""The split batch: registering a group's parents, materialising its ghosts,
withdrawing the real movements and reaping stale ghosts must all happen
together — and the ghosts must be anchored on the group's CANONICAL parent.

Between any two of those steps the database says both the movements AND their
ghosts count — the double count the whole design exists to prevent. So the
ordering and the atomicity are locked here, DB-free: a fake Session records
commits and the repository is stubbed (app.main is never imported).
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.repositories.movement_split_repository import movement_split_repository
from app.services.lot_service import lot_service
from app.services.split_service import split_service

NOW = datetime(2026, 7, 1, 9, 30, 0, tzinfo=timezone.utc)
OLDER = datetime(2026, 6, 15, 0, 0, 0, tzinfo=timezone.utc)


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
    def __init__(self, amount="-1000.00", payment_amount=None,
                 external_ref="S1", shared_key_movements=1, value_date=NOW):
        self.movement_type = "SCTXB"
        self.external_ref = external_ref
        self.account = "0010130015001"
        self.currency = "EUR"
        self.amount = Decimal(amount)
        self.direction = "debit"
        self.value_date = value_date
        self.operation_date = value_date
        self.transaction_particulars = "SCTXB/O/x"
        self.ref_no = None
        self.remarks_1 = "PACS1"
        self.event_type = "TR"
        self.transaction_id = "TX-1"
        self.payload_raw = {"MovementID": 1}
        self.payment_count = 3
        self.payment_amount = Decimal(payment_amount if payment_amount is not None else amount)
        self.shared_key_movements = shared_key_movements


class _Group:
    def __init__(self, parents, children, claim=("MSGID", "PTEL-X"),
                 value_date=NOW):
        self.claim_key_type = claim[0]
        self.claim_key_value = claim[1]
        self.account = "0010130015001"
        self.currency = "EUR"
        self.value_date = value_date
        self.operation_date = value_date
        self.event_type = "TR"
        self.parents = parents
        self.children = children


class _FakeSession:
    def __init__(self):
        self.committed = 0
        self.rolled_back = 0

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


def _hash_of(parent):
    return lot_service.member_to_source_hash(
        flow_id=_Flow.id, external_ref=parent.external_ref, account=parent.account,
        value_date=parent.value_date, operation_date=parent.operation_date,
    )


@pytest.fixture()
def calls(monkeypatch):
    """Stub the repository, recording each call in order.

    ``resolve_group_canonicals`` mirrors the real behaviour by default: the
    canonical is the OLDEST parent just upserted. A test simulating an existing
    group overrides ``calls['canonicals']``.
    """
    seen = {"order": [], "emarged": set(), "canonicals": None}

    def _upsert_parents(db, rows):
        seen["order"].append("upsert_parents")
        seen["parents"] = list(rows)
        return len(rows), 0

    def _resolve(db, *, flow_source_id, claims):
        seen["order"].append("resolve_canonicals")
        seen["claims_resolved"] = list(claims)
        if seen["canonicals"] is not None:
            return seen["canonicals"]
        out = {}
        for row in sorted(seen["parents"],
                          key=lambda r: (r["value_date"], r["external_ref"] or "")):
            claim = (row["claim_key_type"], row["claim_key_value"])
            out.setdefault(claim, SimpleNamespace(
                claim_key_type=claim[0], claim_key_value=claim[1],
                source_hash=row["source_hash"], external_ref=row["external_ref"],
                account=row["account"], currency=row["currency"],
                value_date=row["value_date"], operation_date=row["operation_date"],
                transaction_particulars=row["transaction_particulars"],
                ref_no=row["ref_no"], remarks_1=row["remarks_1"],
            ))
        return out

    def _upsert_ghosts(db, rows):
        seen["order"].append("upsert_ghosts")
        seen["ghosts"] = list(rows)
        return len(rows), 0, 0

    def _withdraw(db, *, parent_hashes):
        seen["order"].append("withdraw")
        seen["withdrawn"] = list(parent_hashes)
        return len(parent_hashes), set(seen["emarged"])

    def _flag(db, *, parent_hashes):
        seen["order"].append("flag_emarged")
        seen["flagged"] = list(parent_hashes)

    def _reap(db, *, flow_source_id, claims, expected_hashes):
        seen["order"].append("reap")
        seen["claims_reaped"] = list(claims)
        seen["expected"] = list(expected_hashes)
        return 0

    def _totals(db, *, flow_source_id, claims):
        return {
            claim: SimpleNamespace(
                parent_total=sum(
                    r["amount"] for r in seen["parents"]
                    if (r["claim_key_type"], r["claim_key_value"]) == claim
                ),
                parent_count=sum(
                    1 for r in seen["parents"]
                    if (r["claim_key_type"], r["claim_key_value"]) == claim
                ),
            )
            for claim in claims
        }

    monkeypatch.setattr(movement_split_repository, "upsert_parents", _upsert_parents)
    monkeypatch.setattr(movement_split_repository, "resolve_group_canonicals", _resolve)
    monkeypatch.setattr(movement_split_repository, "upsert_ghost_entries", _upsert_ghosts)
    monkeypatch.setattr(movement_split_repository, "withdraw_parent_movements", _withdraw)
    monkeypatch.setattr(movement_split_repository, "flag_emarged", _flag)
    monkeypatch.setattr(movement_split_repository, "reap_stale_group_children", _reap)
    monkeypatch.setattr(movement_split_repository, "group_parent_totals", _totals)
    return seen


def _apply(db, groups, run_id=99):
    return split_service.apply_split_batch(
        db, flow=_Flow(), source=_Source(), groups=groups, run_id=run_id
    )


def _one_group(**kwargs):
    return _Group(
        [_Parent()],
        [_Child("KEY:PTEL-X~a", "lot-a", "-700.00", payment_count=2),
         _Child("KEY:PTEL-X~b", "lot-b", "-300.00")],
        **kwargs,
    )


def test_ghosts_exist_before_the_real_movements_are_withdrawn(calls):
    """Withdrawing first would leave the amount out of the reconciliation for
    the rest of the batch; both orders are wrong outside one transaction, but
    inside it this is the order that reads correctly if it ever crashes."""
    db = _FakeSession()
    _apply(db, [_one_group()])

    assert calls["order"] == [
        "upsert_parents", "resolve_canonicals", "upsert_ghosts", "withdraw", "reap",
    ]
    assert db.committed == 1 and db.rolled_back == 0


def test_ghost_rows_carry_the_bucket_and_point_at_the_canonical(calls):
    db = _FakeSession()
    _apply(db, [_one_group()])

    parent_hash = calls["parents"][0]["source_hash"]
    ghosts = {g["external_ref"]: g for g in calls["ghosts"]}
    assert set(ghosts) == {"KEY:PTEL-X~a", "KEY:PTEL-X~b"}
    for ghost in ghosts.values():
        assert ghost["split_parent_hash"] == parent_hash
        assert ghost["status"] == "PENDING"
        assert ghost["ingestion_run_id"] == 99
        assert ghost["account"] == "0010130015001"
        assert ghost["payload_raw"]["split_of"] == "S1"
        assert ghost["payload_raw"]["claim_key"] == "MSGID:PTEL-X"
    assert ghosts["KEY:PTEL-X~a"]["amount"] == Decimal("-700.00")
    assert ghosts["KEY:PTEL-X~a"]["payload_raw"]["payment_count"] == 2
    assert ghosts["KEY:PTEL-X~a"]["reco_id"] == "lot-a"


def test_ghosts_anchor_on_the_stored_canonical_not_the_wire(calls):
    """A group that already exists keeps its anchor: a later run pushing a NEW
    parent must upsert the very same ghost hashes, or every re-emission would
    duplicate the émargé ghosts of the previous runs."""
    stored = _Parent(external_ref="S0-STORED", value_date=OLDER)
    calls["canonicals"] = {
        ("MSGID", "PTEL-X"): SimpleNamespace(
            claim_key_type="MSGID", claim_key_value="PTEL-X",
            source_hash=_hash_of(stored), external_ref=stored.external_ref,
            account=stored.account, currency=stored.currency,
            value_date=stored.value_date, operation_date=stored.operation_date,
            transaction_particulars=stored.transaction_particulars,
            ref_no=stored.ref_no, remarks_1=stored.remarks_1,
        )
    }
    db = _FakeSession()
    _apply(db, [_one_group()])  # the batch itself carries only S1 (NOW)

    ghost = calls["ghosts"][0]
    assert ghost["split_parent_hash"] == _hash_of(stored)
    assert ghost["value_date"] == OLDER
    assert ghost["source_hash"] == lot_service.member_to_source_hash(
        flow_id=_Flow.id, external_ref=ghost["external_ref"],
        account=stored.account, value_date=OLDER, operation_date=OLDER,
    )


def test_parent_hash_is_the_one_a_lot_member_will_derive(calls):
    """Same formula on both sides — this is what makes the ghost→parent link
    resolvable without any hash crossing the wire."""
    db = _FakeSession()
    _apply(db, [_one_group()])

    assert calls["parents"][0]["source_hash"] == lot_service.member_to_source_hash(
        flow_id=_Flow.id, external_ref="S1", account="0010130015001",
        value_date=NOW, operation_date=NOW,
    )


def test_every_parent_of_the_group_is_registered_with_the_claim(calls):
    db = _FakeSession()
    parents = [_Parent(external_ref=f"S{i}", amount="-500.00",
                       shared_key_movements=3) for i in range(3)]
    _apply(db, [_Group(parents, [_Child("KEY:PTEL-X~a", "lot-a", "-1500.00")])])

    assert len(calls["parents"]) == 3
    for row in calls["parents"]:
        assert row["claim_key_type"] == "MSGID"
        assert row["claim_key_value"] == "PTEL-X"
        assert row["shared_key_movements"] == 3
        # The GROUP's children, recorded identically on every parent.
        assert row["child_count"] == 1
        assert row["child_amount"] == Decimal("-1500.00")
    # All three real movements withdrawn.
    assert len(calls["withdrawn"]) == 3


def test_the_claim_is_folded_before_it_touches_the_database(calls):
    db = _FakeSession()
    _apply(db, [_one_group(claim=("msgid", "ptel-x"))])
    assert calls["parents"][0]["claim_key_type"] == "MSGID"
    assert calls["parents"][0]["claim_key_value"] == "PTEL-X"
    assert calls["claims_resolved"] == [("MSGID", "PTEL-X")]


def test_reaper_is_told_the_groups_and_the_surviving_hashes(calls):
    """Anything of those groups outside this set belongs to a bucket that
    disappeared — or to the retired per-parent naming — and would otherwise sit
    PENDING forever with a stale amount."""
    db = _FakeSession()
    _apply(db, [_one_group()])

    ghost_hashes = {g["source_hash"] for g in calls["ghosts"]}
    assert calls["claims_reaped"] == [("MSGID", "PTEL-X")]
    assert set(calls["expected"]) == ghost_hashes


def test_a_group_without_parents_emits_no_ghosts(calls, caplog):
    """Nothing to anchor on, nothing to withdraw — skipped loudly, not half-applied."""
    db = _FakeSession()
    with caplog.at_level("WARNING"):
        result = _apply(db, [_Group([], [_Child("KEY:PTEL-X~a", "lot-a", "-1.00")])])
    assert "no registered parent" in caplog.text
    assert calls["ghosts"] == []
    assert result["ghosts_inserted"] == 0
    assert db.committed == 1


def test_a_group_delta_is_logged_not_swallowed(calls, caplog):
    """Σ ghosts ≠ Σ parents is a fact the second reconciliation will tag — the
    run log must still say it happened."""
    db = _FakeSession()
    group = _Group([_Parent(amount="-1000.00")],
                   [_Child("KEY:PTEL-X~a", "lot-a", "-990.00")])
    with caplog.at_level("INFO"):
        _apply(db, [group])
    assert "does not add up" in caplog.text
    assert "-10.00" in caplog.text


def test_an_already_emarged_parent_is_flagged_and_not_withdrawn(calls):
    """Émargé history is never rewritten. The conflict is surfaced instead of
    being silently resolved one way or the other."""
    db = _FakeSession()
    group = _one_group()
    expected_hash = _hash_of(group.parents[0])
    calls["emarged"] = {expected_hash}

    result = _apply(db, [group])

    assert calls["flagged"] == [expected_hash]
    assert result["parents_emarged"] == 1
    assert db.committed == 1


def test_a_failure_rolls_the_whole_batch_back(calls, monkeypatch):
    def boom(db, *, parent_hashes):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(movement_split_repository, "withdraw_parent_movements", boom)
    db = _FakeSession()
    with pytest.raises(RuntimeError):
        _apply(db, [_one_group()])
    assert db.committed == 0 and db.rolled_back == 1
