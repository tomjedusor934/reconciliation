"""The match basket: refreshing it, and forcing it (DB-free).

force_match used to only ever see two hand-picked rows from one screen. A match
basket hands it hundreds of ids, spanning several reco_ids, assembled over
several searches — which is where its old shortcuts start to bite:

  * one `get_one` per id (N round-trips), and only the FIRST bad id reported;
  * `mark_forced`'s count discarded, so an entry reconciled concurrently just
    dropped out of the group — silently leaving a "balanced" group that no
    longer sums to zero;
  * `reco_id` taken as the first non-null member, which files a deliberately
    multi-reco group under an arbitrary one of them.

The repositories are stubbed, so nothing here touches Postgres. app.main is
never imported (it connects at import time).
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.match_group import MatchMode
from app.models.reconciliation_entry import EntryStatus
from app.services import reconciliation_service as svc_module
from app.services.reconciliation_service import reconciliation_service

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def _entry(entry_id, amount, *, reco_id="R1", flow_id=1, currency="EUR", status=EntryStatus.PENDING):
    return SimpleNamespace(
        id=entry_id,
        amount=Decimal(amount),
        reco_id=reco_id,
        flow_id=flow_id,
        currency=currency,
        status=status,
    )


class _Repo:
    """Stand-in for reconciliation_entry_repository."""

    def __init__(self, entries, *, marked=None):
        self._entries = {e.id: e for e in entries}
        # How many rows mark_forced claims to have updated; None → all of them.
        self._marked = marked
        self.get_many_calls = 0
        self.moved = None
        self.reverted = None
        self.marked_ids = None

    def get_many(self, db, *, entry_ids):
        self.get_many_calls += 1
        return [self._entries[i] for i in entry_ids if i in self._entries]

    def mark_forced(self, db, *, entry_ids, match_group_id):
        self.marked_ids = list(entry_ids)
        return len(entry_ids) if self._marked is None else self._marked

    def revert_forced(self, db, *, match_group_id):
        self.reverted = match_group_id
        return 0

    def move_to_emargement(self, db, *, entry_ids):
        self.moved = list(entry_ids)
        return len(entry_ids)


class _MatchGroupRepo:
    def __init__(self):
        self.created = None
        self.deleted = None
        self._next_id = 42

    def create(self, db, *, mg):
        mg.id = self._next_id
        self.created = mg
        return mg

    def delete(self, db, *, match_group_id):
        self.deleted = match_group_id
        return 1


@pytest.fixture()
def repos(monkeypatch):
    """Install stub repositories; the test picks the entries."""

    def _install(entries, *, marked=None):
        entry_repo = _Repo(entries, marked=marked)
        mg_repo = _MatchGroupRepo()
        monkeypatch.setattr(svc_module, "reconciliation_entry_repository", entry_repo)
        monkeypatch.setattr(svc_module, "match_group_repository", mg_repo)
        return entry_repo, mg_repo

    return _install


def test_balanced_basket_is_forced_in_one_read(repos):
    entries = [_entry(1, "-100.00"), _entry(2, "60.00"), _entry(3, "40.00")]
    entry_repo, mg_repo = repos(entries)

    mg = reconciliation_service.force_match(
        MagicMock(), entry_ids=[1, 2, 3], comment="reversal", user_id=7
    )

    assert mg.mode is MatchMode.FORCED
    assert mg.total == Decimal("0")
    assert mg.created_by_user_id == 7
    assert mg.comment == "reversal"
    # One query for the whole basket, not one per id.
    assert entry_repo.get_many_calls == 1
    assert entry_repo.moved == [1, 2, 3]


def test_multi_reco_basket_leaves_the_group_unlabelled(repos):
    """The point of a basket: legs that offset each other under different
    reco_ids. Filing the group under one of them would be arbitrary."""
    entries = [
        _entry(1, "-364681616.94", reco_id="REVERSAL-NDRJ"),
        _entry(2, "169253242.00", reco_id="SALARY-JULY"),
        _entry(3, "195428374.94", reco_id=None),
    ]
    _, mg_repo = repos(entries)

    mg = reconciliation_service.force_match(
        MagicMock(), entry_ids=[1, 2, 3], comment=None, user_id=1
    )
    assert mg.reco_id is None


def test_single_reco_basket_keeps_its_reco_id(repos):
    entries = [_entry(1, "-100.00", reco_id="R9"), _entry(2, "100.00", reco_id=None)]
    repos(entries)

    mg = reconciliation_service.force_match(
        MagicMock(), entry_ids=[1, 2], comment=None, user_id=1
    )
    assert mg.reco_id == "R9"


def test_every_unusable_id_is_reported_at_once(repos):
    """A basket built over days rots; naming only the first bad id makes the
    operator replay the whole force to discover the next one."""
    entries = [_entry(1, "-100.00"), _entry(4, "100.00")]
    repos(entries)

    with pytest.raises(ValueError) as exc:
        reconciliation_service.force_match(
            MagicMock(), entry_ids=[1, 2, 3, 4], comment=None, user_id=1
        )
    assert "2" in str(exc.value) and "3" in str(exc.value)


def test_non_pending_entries_are_all_named(repos):
    entries = [
        _entry(1, "-100.00"),
        _entry(2, "60.00", status=EntryStatus.MATCHED),
        _entry(3, "40.00", status=EntryStatus.EXCLUDED),
    ]
    repos(entries)

    with pytest.raises(ValueError) as exc:
        reconciliation_service.force_match(
            MagicMock(), entry_ids=[1, 2, 3], comment=None, user_id=1
        )
    msg = str(exc.value)
    assert "not pending" in msg
    assert "2 (status=matched)" in msg and "3 (status=excluded)" in msg


def test_partial_mark_rolls_the_group_back(repos):
    """An entry reconciled between the check and the update would silently drop
    out of the group, leaving a 'balanced' match that no longer sums to zero."""
    entries = [_entry(1, "-100.00"), _entry(2, "60.00"), _entry(3, "40.00")]
    entry_repo, mg_repo = repos(entries, marked=2)

    with pytest.raises(ValueError) as exc:
        reconciliation_service.force_match(
            MagicMock(), entry_ids=[1, 2, 3], comment=None, user_id=1
        )
    assert "only 2 of 3" in str(exc.value)
    # The half-built group must not survive, and nothing may be émargé.
    assert entry_repo.reverted == mg_repo.created.id
    assert mg_repo.deleted == mg_repo.created.id
    assert entry_repo.moved is None


def test_duplicate_ids_cannot_fake_a_balance(repos):
    """Sending the same credit twice would otherwise offset a single debit."""
    entries = [_entry(1, "-100.00"), _entry(2, "100.00")]
    entry_repo, _ = repos(entries)

    reconciliation_service.force_match(
        MagicMock(), entry_ids=[1, 2, 2], comment=None, user_id=1
    )
    assert entry_repo.marked_ids == [1, 2]

    # And the same id repeated is not enough to balance on its own.
    entries = [_entry(1, "-50.00"), _entry(2, "25.00")]
    repos(entries)
    with pytest.raises(ValueError, match="sum is not zero"):
        reconciliation_service.force_match(
            MagicMock(), entry_ids=[1, 2, 2], comment=None, user_id=1
        )


def test_unbalanced_basket_is_still_refused(repos):
    entries = [_entry(1, "-364681616.94"), _entry(2, "169253242.00")]
    repos(entries)

    with pytest.raises(ValueError, match="sum is not zero"):
        reconciliation_service.force_match(
            MagicMock(), entry_ids=[1, 2], comment="justification", user_id=1
        )


def test_cross_flow_and_cross_currency_are_refused(repos):
    repos([_entry(1, "-100.00", flow_id=1), _entry(2, "100.00", flow_id=2)])
    with pytest.raises(ValueError, match="same flow"):
        reconciliation_service.force_match(
            MagicMock(), entry_ids=[1, 2], comment=None, user_id=1
        )

    repos([_entry(1, "-100.00", currency="EUR"), _entry(2, "100.00", currency="USD")])
    with pytest.raises(ValueError, match="same currency"):
        reconciliation_service.force_match(
            MagicMock(), entry_ids=[1, 2], comment=None, user_id=1
        )


def test_empty_basket_is_refused(repos):
    repos([])
    with pytest.raises(ValueError, match="at least one entry"):
        reconciliation_service.force_match(
            MagicMock(), entry_ids=[], comment=None, user_id=1
        )


# ---------------------------------------------------------------------------
# The basket's refresh path
# ---------------------------------------------------------------------------

def test_ids_filter_reaches_both_repository_queries(monkeypatch):
    """A basket refreshes by asking for its own ids.

    Two things have to hold or the basket cannot tell "reconciled meanwhile"
    from "gone": the ids must reach the count query as well as the list query
    (otherwise total_count contradicts the page), and `status` must stay None so
    _entry_models_for_status routes to the live AND the émargement table.
    """
    seen = {}

    class _Repo:
        def list_filtered(self, db, **kw):
            seen["list"] = kw
            return []

        def count_filtered(self, db, **kw):
            seen["count"] = kw
            return 0

    monkeypatch.setattr(svc_module, "reconciliation_entry_repository", _Repo())

    reconciliation_service.list_entries_filtered(
        MagicMock(), ids=[7, 8, 9], with_payment_statuses=False, limit=3
    )

    assert seen["list"]["ids"] == [7, 8, 9]
    assert seen["count"]["ids"] == [7, 8, 9]
    assert seen["list"]["status"] is None
