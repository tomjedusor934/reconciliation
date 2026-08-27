"""The 'non rattachés' half of the RCP tool (see services/rcp_orphan_service.py).

Two things are locked here:
  * the key extraction, which is a PURE function and the whole reason a proposal
    can be audited — it must never turn a fragment of a longer token, or an
    amount in a free-text label, into a PaymentNumber;
  * the assembly of the report, with the database and the datamart stubbed out.

DB-free: the imports never touch app.main (which connects to Postgres at import).
"""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.flow import ParserType
from app.models.reconciliation_entry import EntryStatus
from app.schemas.rcp_link import RcpOrphanAnalyzeResponse
from app.services.rcp_link_service import ST_NO_TARGET, ST_PROPOSED, TARGET_LOT, TARGET_RECO
from app.services.rcp_orphan_service import (
    KIND_MOVEMENT,
    KIND_PO,
    RULE_REF_NO,
    RULE_TP_DIGITS,
    RULE_TP_MOVEMENT,
    RULE_TP_RETURN,
    ST_KEY_AMBIGUOUS,
    ST_NO_KEY,
    RcpOrphanService,
    orphan_keys,
)

NOW = datetime(2026, 7, 29, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# key extraction (pure)
# ---------------------------------------------------------------------------

def test_ref_no_wins_and_tolerates_the_double_hash():
    # ref_no is a field Finacle filled deliberately — it outranks anything read
    # out of the label, and a single rule always fires (never a merge of two).
    keys = orphan_keys("EXTOURNE DE VOTRE VIREMENT", "000009512882")
    assert [(k.value, k.kind, k.rule) for k in keys] == [
        ("000009512882", KIND_PO, RULE_REF_NO)
    ]
    keys = orphan_keys("NDRJ##AM14", "BKRTP##000009354866")
    assert keys[0].value == "000009354866"


def test_the_return_shape_is_read_whatever_the_prefix():
    keys = orphan_keys("Rev of BKRTP/NCP/O/000008721788/CARLO", None)
    assert (keys[0].value, keys[0].rule) == ("000008721788", RULE_TP_RETURN)
    # an alphanumeric PaymentNumber is a PaymentNumber too
    assert orphan_keys("Rev of BKRTP/NCP/O/C6G03XM0QS000001/EDITIONS", None)[0].value == (
        "C6G03XM0QS000001"
    )
    # RRS and RCP are return/reject segments as well
    assert orphan_keys("SDDXB/RRS/I/000008584082", None)[0].value == "000008584082"


def test_a_movement_named_in_free_text_is_a_movement_not_a_payment():
    keys = orphan_keys("RECTIF PF0008529", None)
    assert (keys[0].value, keys[0].kind, keys[0].rule) == (
        "PF0008529", KIND_MOVEMENT, RULE_TP_MOVEMENT
    )
    assert orphan_keys("Reversal of PF0043501##20260710", None)[0].value == "PF0043501"


def test_a_digit_run_is_only_read_whole():
    # 'SDDXBREJ/SDDXB260821000288645/…' — the digits are the tail of a
    # TransactionRef. Offering them as a PaymentNumber would be a mutilated key,
    # and the parser resolves that shape through std.Payment.TransactionRef.
    assert orphan_keys("SDDXBREJ/SDDXB260821000288645/TRANSACTION FORBIDDEN", None) == []
    keys = orphan_keys("RETOUR VIREMENT 000009776284", None)
    assert (keys[0].value, keys[0].rule) == ("000009776284", RULE_TP_DIGITS)


def test_the_manual_reversal_of_the_29_07_block_yields_no_single_key():
    # 'REVERSAL-NDRJ DR 364681616.94, CR 169253242, …' (+195 459 243,59) and its
    # twin carry only AMOUNTS. Two candidates, therefore an ambiguity to show —
    # never a 195 M€ credit silently dropped into a bucket.
    keys = orphan_keys("REVERSAL-NDRJ DR 364681616.94, CR 169253242, BKRTP-30997.09", None)
    assert len(keys) == 2
    assert orphan_keys("SALARY RECONCILIATION JULY 2026", None) == []
    assert orphan_keys("CORRECTION DOUBLE REJECTIONS 21.07.2026 - EUR 154,260.62", None) == []


def test_nothing_to_read_is_not_an_error():
    assert orphan_keys(None, None) == []
    assert orphan_keys("", "  ") == []


# ---------------------------------------------------------------------------
# report assembly (database and datamart stubbed)
# ---------------------------------------------------------------------------

def _entry(tp, ref_no=None, amount="-100.00", reco_id="Not Supported", source_hash="a" * 64):
    return SimpleNamespace(
        id=7, flow_id=16, source_hash=source_hash, reco_id=reco_id,
        account="0010130015001", currency="EUR", amount=Decimal(amount),
        direction="debit", value_date=NOW, operation_date=NOW,
        external_ref="PF0088381", transaction_particulars=tp, ref_no=ref_no,
        remarks_1=None, ingestion_run_id=9, status=EntryStatus.PENDING,
    )


def _source(parser=ParserType.FINACLE_BATCH_BOOKING_TRUE):
    return SimpleNamespace(id=163, code="finacle_db", parser_type=parser)


@pytest.fixture
def service(monkeypatch):
    svc = RcpOrphanService()
    monkeypatch.setattr(svc, "_lots_by_movement_ref", lambda db, *, flow_id, refs: {})
    return svc


def _run(svc, monkeypatch, rows, *, targets=None, lots_by_movement=None):
    from app.services import rcp_orphan_service as module

    monkeypatch.setattr(svc, "_orphan_entries", lambda db, *, flow_id, limit: rows)
    monkeypatch.setattr(
        module.rcp_link_service, "resolve_payments", lambda db, cid, pos, *a, **k: ({}, "")
    )
    monkeypatch.setattr(
        module.rcp_link_service, "targets_for_pos",
        lambda db, pos, payments, *, flow_id, flow_source_id, parser_type: targets or {},
    )
    monkeypatch.setattr(
        svc, "_lots_by_movement_ref",
        lambda db, *, flow_id, refs: lots_by_movement or {},
    )
    return svc.analyze(None, flow_id=16, connection_id=1)


def test_a_resolved_payment_number_becomes_a_lot_proposal(service, monkeypatch):
    rows = [(_entry("Rev of /NCP/O/000009324710/TRESORERIE", "000009324710"), _source())]
    targets = {
        "000009324710": {
            "target_id": "lot-uuid-1", "target_kind": TARGET_LOT, "label": "PAIR 2607…",
        }
    }
    report = _run(service, monkeypatch, rows, targets=targets)
    proposal = report["proposals"][0]
    assert proposal["status"] == ST_PROPOSED
    assert proposal["target_id"] == "lot-uuid-1"
    assert proposal["rule"] == RULE_REF_NO          # the evidence travels with it
    assert report["summary"][ST_PROPOSED] == 1
    RcpOrphanAnalyzeResponse.model_validate(report)  # the shape the UI is written against


def test_a_key_the_datamart_does_not_know_is_not_a_proposal(service, monkeypatch):
    rows = [(_entry("RETOUR VIREMENT 000009776284"), _source())]
    report = _run(service, monkeypatch, rows, targets={})
    assert report["proposals"][0]["status"] == ST_NO_TARGET
    assert report["proposals"][0]["target_id"] == ""


def test_a_movement_named_in_free_text_lands_in_its_lot(service, monkeypatch):
    rows = [(_entry("RECTIF PF0008529"), _source())]
    report = _run(
        service, monkeypatch, rows,
        lots_by_movement={"PF0008529": [("lot-uuid-9", "PF0008529#2")]},
    )
    proposal = report["proposals"][0]
    assert proposal["status"] == ST_PROPOSED and proposal["target_id"] == "lot-uuid-9"


def test_a_movement_named_in_several_lots_is_an_ambiguity(service, monkeypatch):
    rows = [(_entry("RECTIF PF0008529"), _source())]
    report = _run(
        service, monkeypatch, rows,
        lots_by_movement={"PF0008529": [("lot-a", "PF0008529#2"), ("lot-b", "PF0008529")]},
    )
    assert report["proposals"][0]["status"] == ST_KEY_AMBIGUOUS


def test_free_text_with_no_key_is_reported_as_such(service, monkeypatch):
    rows = [(_entry("SALARY RECONCILIATION JULY 2026", amount="169253242.00"), _source())]
    report = _run(service, monkeypatch, rows)
    proposal = report["proposals"][0]
    assert proposal["status"] == ST_NO_KEY and proposal["target_id"] == ""


def test_a_classic_flow_targets_a_reconciliation_key_not_a_lot(service, monkeypatch):
    rows = [(_entry("Rev of /NCP/O/PO1/x", "PO1"), _source(ParserType.FINACLE_DB))]
    targets = {"PO1": {"target_id": "PACS-ORIG-1", "target_kind": TARGET_RECO, "label": "PACS-ORIG-1"}}
    report = _run(service, monkeypatch, rows, targets=targets)
    assert report["proposals"][0]["target_kind"] == TARGET_RECO


# ---------------------------------------------------------------------------
# commit — the guard is the point
# ---------------------------------------------------------------------------

class _FakeQuery:
    def __init__(self, result): self._result = result
    def filter(self, *a, **k): return self
    def one_or_none(self): return self._result


class _FakeSession:
    """Answers each model with a fixed row; records whether it committed."""
    def __init__(self, rows): self._rows, self.committed, self.rolled_back = rows, False, False
    def query(self, model): return _FakeQuery(self._rows.get(model))
    def commit(self): self.committed = True
    def rollback(self): self.rolled_back = True


def _commit_item(source_hash="a" * 64, target_id="lot-uuid-1"):
    return SimpleNamespace(source_hash=source_hash, target_id=target_id, rule="", key="")


def test_commit_refuses_a_movement_that_is_already_in_a_lot(service):
    from app.models.reconciliation_entry import ReconciliationEntry

    entry = _entry("Rev of /NCP/O/PO1/x", "PO1", reco_id="lot-already-there")
    db = _FakeSession({ReconciliationEntry: entry})
    outcome = service._commit_one(db, item=_commit_item(), user_id=1)
    assert not outcome["applied"]
    assert "déjà rattaché" in outcome["error"]
    assert not db.committed and entry.reco_id == "lot-already-there"


def test_commit_refuses_a_movement_that_is_no_longer_pending(service):
    from app.models.reconciliation_entry import ReconciliationEntry

    entry = _entry("Rev of /NCP/O/PO1/x", "PO1")
    entry.status = SimpleNamespace(value="MATCHED")
    db = _FakeSession({ReconciliationEntry: entry})
    outcome = service._commit_one(db, item=_commit_item(), user_id=1)
    assert not outcome["applied"] and "PENDING" in outcome["error"]
    assert not db.committed


def test_commit_refuses_a_lot_belonging_to_another_source(service):
    from app.models.flow import Flow, FlowSource
    from app.models.ingestion_run import IngestionRun
    from app.models.movement_lot import MovementLot
    from app.models.reconciliation_entry import ReconciliationEntry

    entry = _entry("Rev of /NCP/O/PO1/x", "PO1")
    db = _FakeSession({
        ReconciliationEntry: entry,
        IngestionRun: SimpleNamespace(id=9, flow_source_id=163),
        FlowSource: _source(),
        Flow: SimpleNamespace(id=16, code="float_out"),
        MovementLot: SimpleNamespace(id="lot-uuid-1", flow_source_id=999),
    })
    outcome = service._commit_one(db, item=_commit_item(), user_id=1)
    assert not outcome["applied"] and "autre source" in outcome["error"]
    assert not db.committed
