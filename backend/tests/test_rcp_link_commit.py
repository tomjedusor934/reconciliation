"""The write path: what a commit refuses, and what it hands to the two services.

The commit does not invent a mechanism — it builds the payloads the
batch-booking DAG already pushes (a claim group with its parent and its ghosts,
then the lot members) and lets ``split_service`` / ``lot_service`` apply them.
So what needs locking is the payload contract and the guards that decide a
movement is still safe to split.

Both flows are exercised here. A batch-booking movement is split into LOTS; a
classic bulk movement is split onto RECONCILIATION KEYS and produces no lot
member at all. Which branch runs is re-derived from the movement's own source,
never read off the payload.
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.flow import Flow, FlowSource, ParserType
from app.models.ingestion_run import IngestionRun
from app.models.movement_lot import MovementLot
from app.models.movement_split import MovementSplit
from app.models.reconciliation_entry import EntryStatus, ReconciliationEntry
from app.services import rcp_link_service as module
from app.services.rcp_link_service import CLAIM_TYPE, TARGET_LOT, TARGET_RECO, RcpLinkService

NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


class _FakeQuery:
    def __init__(self, result):
        self._result = result

    def filter(self, *args, **kwargs):
        return self

    def distinct(self):
        return self

    def all(self):
        return list(self._result or [])

    def one_or_none(self):
        rows = list(self._result or [])
        return rows[0] if rows else None


class _FakeSession:
    """Answers ``query(Model)`` from a {model: rows} map — enough for the
    commit's re-validation, which only ever loads by primary key."""

    def __init__(self, rows):
        self.rows = rows

    def query(self, *entities):
        return _FakeQuery(self.rows.get(entities[0], []))

    def rollback(self):
        pass


def _entry(status=EntryStatus.PENDING, amount="5000.00", particulars="SCTXB/I/BLK1"):
    booked = Decimal(amount)
    return SimpleNamespace(
        id=1, flow_id=16, ingestion_run_id=9, source_hash="a" * 64, reco_id="lot-self",
        account="0010130015001", currency="EUR", amount=booked,
        direction=SimpleNamespace(value="debit" if booked < 0 else "credit"),
        value_date=NOW, operation_date=NOW,
        external_ref="PF0045040", transaction_particulars=particulars, ref_no=None,
        remarks_1="BLK1", transaction_id="PF0045040", event_type=None,
        payload_raw={"x": 1}, status=status,
    )


def _split_parent(msgid="BLK1", amount="5000.00", flow_source_id=4,
                  particulars="SCTXB/I/BLK1"):
    """A movement already replaced by its ghosts — all that is left of it."""
    booked = Decimal(amount)
    return SimpleNamespace(
        source_hash="a" * 64, flow_id=16, flow_source_id=flow_source_id,
        movement_type="SCTXB", external_ref="PF0045040", account="0010130015001",
        currency="EUR", amount=booked, direction="debit" if booked < 0 else "credit",
        value_date=NOW, operation_date=NOW, transaction_particulars=particulars,
        ref_no=None, remarks_1=msgid, payload_raw={"x": 1},
        claim_key_type=CLAIM_TYPE, claim_key_value=msgid.upper(),
    )


def _lot(lot_id="lot-a", flow_source_id=4, pacs="PACS1", msgid="MSG1"):
    return SimpleNamespace(
        id=lot_id, flow_source_id=flow_source_id, bucket_kind="PAIR",
        bucket_pacs008=pacs, bucket_msgid=msgid, bucket_po="", bucket_ref="",
        currency="EUR", status="active", merged_into_lot_id=None,
    )


def _session(entry=None, lots=None, flow_source_id=4,
             parser=ParserType.FINACLE_BATCH_BOOKING_TRUE, split=None):
    return _FakeSession({
        ReconciliationEntry: [entry] if entry else [],
        IngestionRun: [SimpleNamespace(id=9, flow_source_id=flow_source_id)],
        FlowSource: [SimpleNamespace(id=flow_source_id, code="finacle_db", parser_type=parser)],
        Flow: [SimpleNamespace(id=16, code="float_account_outward")],
        MovementLot: lots if lots is not None else [],
        MovementSplit: [split] if split else [],
    })


def _target(target_id, amount, pos):
    return SimpleNamespace(
        target_id=target_id, amount=Decimal(amount), payment_count=len(pos), pos=pos
    )


def _item(targets=None, msgid="BLK1", source_hash="a" * 64):
    return SimpleNamespace(
        msgid=msgid,
        entry_source_hash=source_hash,
        targets=targets if targets is not None else [
            _target("lot-a", "3000.00", ["000008957379"]),
            _target("lot-b", "2000.00", ["000008957555"]),
        ],
    )


@pytest.fixture
def captured(monkeypatch):
    """Intercepts the two service calls instead of touching a database."""
    calls = {}

    def fake_split(db, *, flow, source, groups, run_id=None):
        calls["split"] = {"flow": flow, "source": source, "groups": groups, "run_id": run_id}
        return {"parents_emarged": 0, "ghosts_inserted": len(groups[0].children)}

    def fake_lots(db, *, flow, source, lots, members):
        calls["lots"] = {"lots": lots, "members": members}
        return {"members_inserted": len(members)}

    monkeypatch.setattr(module.split_service, "apply_split_batch", fake_split)
    monkeypatch.setattr(module.lot_service, "apply_lot_batch", fake_lots)
    monkeypatch.setattr(
        module.audit_service, "log_ui_action",
        lambda db, **kwargs: calls.setdefault("audit", kwargs),
    )
    return calls


# ── guards ──────────────────────────────────────────────────────────

def test_a_movement_that_left_the_live_table_is_refused():
    result = RcpLinkService()._commit_one(_session(), item=_item(), user_id=1)

    assert result["applied"] is False
    assert "introuvable" in result["error"]


def test_a_non_pending_movement_is_refused():
    """withdraw_parent_movements only deletes PENDING rows — committing a
    matched one would create the ghosts on top of the movement."""
    entry = _entry(status=EntryStatus.MATCHED)

    result = RcpLinkService()._commit_one(_session(entry, [_lot()]), item=_item(), user_id=1)

    assert result["applied"] is False
    assert "PENDING" in result["error"]


def test_a_movement_that_no_longer_quotes_the_msgid_is_refused():
    entry = _entry(particulars="SCTXB/I/SOMETHING-ELSE")

    result = RcpLinkService()._commit_one(_session(entry, [_lot()]), item=_item(), user_id=1)

    assert result["applied"] is False
    assert "msgid" in result["error"]


def test_a_lot_from_another_source_is_refused():
    entry = _entry()
    session = _session(entry, [_lot("lot-a", flow_source_id=99), _lot("lot-b")])

    result = RcpLinkService()._commit_one(session, item=_item(), user_id=1)

    assert result["applied"] is False
    assert "autre source" in result["error"]


def test_ghosts_exceeding_the_booked_amount_are_refused():
    entry = _entry(amount="1000.00")
    session = _session(entry, [_lot("lot-a"), _lot("lot-b", pacs="PACS2")])

    result = RcpLinkService()._commit_one(session, item=_item(), user_id=1)

    assert result["applied"] is False
    assert "dépassent" in result["error"]


# ── batch-booking flow: ghosts in lots ──────────────────────────────

def test_commit_builds_one_claim_group_with_a_ghost_per_lot(captured):
    entry = _entry()
    session = _session(entry, [_lot("lot-a"), _lot("lot-b", pacs="PACS2")])

    result = RcpLinkService()._commit_one(session, item=_item(), user_id=7)

    assert result["applied"] is True
    group = captured["split"]["groups"][0]
    # The claim type is ours alone: it can never merge with a DAG group.
    assert (group.claim_key_type, group.claim_key_value) == (CLAIM_TYPE, "BLK1")
    assert len(group.parents) == 1
    assert group.parents[0].external_ref == "PF0045040"
    assert group.parents[0].amount == Decimal("5000.00")
    assert [c.lot_id for c in group.children] == ["lot-a", "lot-b"]
    assert sum(c.amount for c in group.children) == Decimal("5000.00")
    # The run that ingested the parent also owns its ghosts.
    assert captured["split"]["run_id"] == 9
    assert [t["target_kind"] for t in result["targets"]] == [TARGET_LOT, TARGET_LOT]


def test_ghost_entry_and_lot_member_share_one_identity(captured):
    """The backend hashes both from external_ref + the canonical parent, so the
    member and the entry must carry the very same ref — otherwise the lot holds
    a member no entry backs."""
    session = _session(_entry(), [_lot("lot-a"), _lot("lot-b", pacs="PACS2")])

    RcpLinkService()._commit_one(session, item=_item(), user_id=7)

    ghost_refs = [c.external_ref for c in captured["split"]["groups"][0].children]
    member_refs = [m.external_ref for m in captured["lots"]["members"]]
    assert ghost_refs == member_refs
    assert all(ref.startswith("KEY:BLK1~") for ref in ghost_refs)
    # No new lot is ever created: the targets already exist.
    assert captured["lots"]["lots"] == []
    assert all(m.claim_key_type == CLAIM_TYPE for m in captured["lots"]["members"])


def test_ghosts_take_the_sign_of_the_booking_whatever_the_client_sent(captured):
    """A debit movement must produce debit ghosts even though the client only
    ever sends the magnitude the files add up to."""
    session = _session(_entry(amount="-5000.00"), [_lot("lot-a"), _lot("lot-b", pacs="PACS2")])

    RcpLinkService()._commit_one(session, item=_item(), user_id=7)

    children = captured["split"]["groups"][0].children
    assert [c.amount for c in children] == [Decimal("-3000.00"), Decimal("-2000.00")]
    assert all(c.direction == "debit" for c in children)  # copied from the parent


def test_a_partial_split_is_allowed_and_recorded(captured):
    """Some payments never found their target: the ghosts fall short, and the
    claim-group reconciliation will tag the gap."""
    session = _session(_entry(), [_lot("lot-a")])
    item = _item(targets=[_target("lot-a", "3000.00", ["P1"])])

    result = RcpLinkService()._commit_one(session, item=item, user_id=7)

    assert result["applied"] is True
    assert result["ghost_total"] == Decimal("3000.00")
    assert result["booked_amount"] == Decimal("5000.00")
    assert captured["audit"]["action"] == "rcp_reattribution_commit"
    assert captured["audit"]["details"]["ghost_total"] == "3000.00"


def test_member_keys_point_the_returned_payments_at_the_target_lot(captured):
    session = _session(_entry(), [_lot("lot-a"), _lot("lot-b", pacs="PACS2")])

    RcpLinkService()._commit_one(session, item=_item(), user_id=7)

    first = captured["lots"]["members"][0]
    assert sorted((k.key_type, k.key_value) for k in first.keys) == [
        ("MSGID", "MSG1"), ("PACS008", "PACS1"), ("PO", "000008957379"),
    ]


# ── classic bulk flow: ghosts on reconciliation keys ────────────────

def _classic_session(entry, parser=ParserType.FINACLE_DB):
    return _session(entry, lots=[], parser=parser)


def _classic_item():
    return _item(targets=[
        _target("20260721.500.21387506194869200", "3000.00", ["000008957379"]),
        _target("ZSDD20260721GENODE6120260716_003842", "2000.00", ["000008957555"]),
    ])


def test_a_classic_movement_is_split_onto_reco_keys_without_any_lot(captured, monkeypatch):
    """No lot exists on that flow: apply_split_batch alone does the whole job,
    because it writes reco_id = child.lot_id on its ghosts."""
    service = RcpLinkService()
    monkeypatch.setattr(
        service, "known_reco_ids", lambda db, *, flow_id, reco_ids: set(reco_ids)
    )
    session = _classic_session(_entry(particulars="SCTXB/O/BLK1"))

    result = service._commit_one(session, item=_classic_item(), user_id=7)

    assert result["applied"] is True
    children = captured["split"]["groups"][0].children
    assert [c.lot_id for c in children] == [
        "20260721.500.21387506194869200", "ZSDD20260721GENODE6120260716_003842",
    ]
    assert all(c.bucket_kind == "" and c.bucket_pacs008 == "" for c in children)
    # THE point of the classic branch: no lot member, no lot batch.
    assert "lots" not in captured
    assert [t["target_kind"] for t in result["targets"]] == [TARGET_RECO, TARGET_RECO]


def test_classic_ghost_refs_stay_distinct_and_stable(captured, monkeypatch):
    service = RcpLinkService()
    monkeypatch.setattr(
        service, "known_reco_ids", lambda db, *, flow_id, reco_ids: set(reco_ids)
    )

    service._commit_one(_classic_session(_entry()), item=_classic_item(), user_id=7)
    first = [c.external_ref for c in captured["split"]["groups"][0].children]
    service._commit_one(_classic_session(_entry()), item=_classic_item(), user_id=7)
    again = [c.external_ref for c in captured["split"]["groups"][0].children]

    assert first == again          # re-committing upserts the same ghosts
    assert len(set(first)) == 2    # one per destination
    assert all(len(ref) <= 128 for ref in first)


def test_split_child_accepts_a_key_longer_than_a_uuid():
    """``SplitChildIn.lot_id`` used to be bounded at 36 (a uuid). A classic
    reconciliation key is a PACS008/MessageID — observed at 35 characters in
    prod, with nothing guaranteeing it stops there — so the bound now follows
    ``reconciliation_entry.reco_id``."""
    from app.schemas.split import SplitChildIn

    child = SplitChildIn(external_ref="KEY:x", lot_id="P" * 100, amount=Decimal("1"))

    assert len(child.lot_id) == 100


def test_a_reco_key_nobody_carries_is_refused(monkeypatch):
    """The classic analogue of "the lot does not exist": a ghost on a key no
    entry of the flow uses would sit alone forever."""
    service = RcpLinkService()
    monkeypatch.setattr(service, "known_reco_ids", lambda db, *, flow_id, reco_ids: set())

    result = service._commit_one(_classic_session(_entry()), item=_classic_item(), user_id=7)

    assert result["applied"] is False
    assert "clé(s) reco" in result["error"]


def test_the_branch_comes_from_the_source_not_from_the_payload(captured, monkeypatch):
    """Same payload, two sources: the parser decides. A payload naming lot ids
    on a classic source produces reco-key ghosts and no member."""
    service = RcpLinkService()
    monkeypatch.setattr(
        service, "known_reco_ids", lambda db, *, flow_id, reco_ids: set(reco_ids)
    )
    session = _classic_session(_entry())

    result = service._commit_one(session, item=_item(), user_id=7)

    assert result["applied"] is True
    assert [t["target_kind"] for t in result["targets"]] == [TARGET_RECO, TARGET_RECO]
    assert "lots" not in captured


def test_commit_reports_each_movement_independently(captured):
    """One bad movement must not stop the batch."""
    session = _session(_entry(), [_lot("lot-a"), _lot("lot-b", pacs="PACS2")])

    report = RcpLinkService().commit(
        session, items=[_item(), _item(msgid="BLK2", source_hash="b" * 64)], user_id=7
    )

    assert report["applied"] == 1
    assert report["failed"] == 1
    assert [r["msgid"] for r in report["results"]] == ["BLK1", "BLK2"]


# ── replay: re-committing a movement that no longer has a live row ───

@pytest.fixture
def no_ghost_event_type(monkeypatch):
    """The group's ghosts, as ``_group_event_type`` reads them back."""
    monkeypatch.setattr(
        module.movement_split_repository, "list_children",
        lambda db, *, parent_hash: [SimpleNamespace(event_type="TR")],
    )


def test_a_committed_movement_is_replayed_from_its_split_parent(captured, no_ghost_event_type):
    """After a commit the real movement is gone AND the re-ingestion guard keeps
    it gone, so ``movement_split`` is the only description left of it."""
    session = _session(
        entry=None, split=_split_parent(), lots=[_lot("lot-a"), _lot("lot-b", pacs="PACS2")]
    )

    result = RcpLinkService()._commit_one(session, item=_item(), user_id=7)

    assert result["applied"] is True
    parent = captured["split"]["groups"][0].parents[0]
    assert parent.amount == Decimal("5000.00")
    assert parent.external_ref == "PF0045040"
    assert parent.movement_type == "SCTXB"
    # No ingestion run behind a replay — the ghosts keep the one that made them.
    assert captured["split"]["run_id"] is None
    assert captured["split"]["groups"][0].event_type == "TR"


def test_a_replay_re_emits_the_very_same_ghost_identities(captured, no_ghost_event_type):
    """This is what makes a replay an UPDATE and not a duplicate: a ghost is
    named after (claim, bucket) alone, so the second pass upserts the first
    pass's rows even with corrected amounts."""
    service = RcpLinkService()
    lots = [_lot("lot-a"), _lot("lot-b", pacs="PACS2")]

    service._commit_one(_session(_entry(), lots), item=_item(), user_id=7)
    first = [c.external_ref for c in captured["split"]["groups"][0].children]

    corrected = _item(targets=[
        _target("lot-a", "2900.00", ["000008957379"]),
        _target("lot-b", "2000.00", ["000008957555"]),
    ])
    service._commit_one(
        _session(entry=None, split=_split_parent(), lots=lots), item=corrected, user_id=7
    )
    second = captured["split"]["groups"][0].children

    assert [c.external_ref for c in second] == first
    assert [c.amount for c in second] == [Decimal("2900.00"), Decimal("2000.00")]


def test_a_split_parent_of_another_claim_is_refused(no_ghost_event_type):
    """The hash is addressed by the payload; the claim it carries is not."""
    session = _session(entry=None, split=_split_parent(msgid="BLK9"), lots=[_lot("lot-a")])

    result = RcpLinkService()._commit_one(
        session, item=_item(targets=[_target("lot-a", "5000.00", ["PO1"])]), user_id=7
    )

    assert result["applied"] is False
    assert "autre msgid" in result["error"]


def test_a_replayed_movement_keeps_its_own_booked_amount(no_ghost_event_type):
    """The sign and the ceiling come from the split parent, not from the client."""
    session = _session(
        entry=None, split=_split_parent(amount="-5000.00"), lots=[_lot("lot-a")]
    )

    result = RcpLinkService()._commit_one(
        session, item=_item(targets=[_target("lot-a", "6000.00", ["PO1"])]), user_id=7
    )

    assert result["applied"] is False
    assert "dépassent le montant booké" in result["error"]


def test_payment_count_survives_a_pos_list_dropped_by_the_cap(captured):
    """Analyze empties ``pos`` above MAX_PO_KEYS (40 250 ids would otherwise
    travel in the report); the count must not follow it to zero."""
    big = SimpleNamespace(
        target_id="lot-a", amount=Decimal("5000.00"), payment_count=1550, pos=[]
    )
    session = _session(_entry(), [_lot("lot-a")])

    result = RcpLinkService()._commit_one(session, item=_item(targets=[big]), user_id=7)

    assert result["applied"] is True
    assert result["targets"][0]["payment_count"] == 1550
    assert captured["split"]["groups"][0].children[0].payment_count == 1550
    # Nothing to index when the list is gone — same rule as _member_keys.
    assert [
        (k.key_type, k.key_value) for k in captured["lots"]["members"][0].keys
    ] == [("MSGID", "MSG1"), ("PACS008", "PACS1")]
