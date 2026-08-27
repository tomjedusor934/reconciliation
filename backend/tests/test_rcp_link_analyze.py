"""End-to-end shape of the analyze report, with the database stubbed out.

Parsing and control are covered elsewhere; what this locks is the assembly:
which status a movement ends up with, and that the whole report still validates
against ``RcpAnalyzeResponse`` — the schema the UI is written against.
"""
import io
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from openpyxl import Workbook

from app.models.flow import ParserType
from app.models.reconciliation_entry import EntryStatus
from app.schemas.rcp_link import RcpAnalyzeResponse
from app.services.rcp_link_parser import BUCKET_PAIR
from app.services.rcp_link_service import (
    CLAIM_TYPE,
    ENTRY_STATUS_REPLACED,
    ST_EMARGED,
    ST_ENTRY_AMBIGUOUS,
    ST_NO_DUMP_ROWS,
    ST_NO_TARGET,
    ST_PROPOSED,
    ST_TO_REPLAY,
    TARGET_LOT,
    TARGET_RECO,
    RcpLinkService,
)

NOW = datetime(2026, 7, 17, tzinfo=timezone.utc)


def _link_workbook(rows):
    book = Workbook()
    sheet = book.active
    sheet.append([
        "spdate", "paysysid", "serviceid", "direction",
        "num_0f_records", "settlementamt", "trandate", "tranid", "msgid",
    ])
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _entry(msgid, amount="5000.00", status=EntryStatus.PENDING):
    return SimpleNamespace(
        id=1, flow_id=16, source_hash="a" * 64, reco_id="lot-self",
        account="0010130015001", currency="EUR", amount=Decimal(amount),
        direction=SimpleNamespace(value="credit"), value_date=NOW, operation_date=NOW,
        external_ref="PF0045040", transaction_particulars=f"SCTXB/I/{msgid}",
        ref_no=None, remarks_1=msgid, transaction_id="PF0045040",
        event_type=None, payload_raw={}, ingestion_run_id=9, status=status,
    )


def _source(parser=ParserType.FINACLE_BATCH_BOOKING_TRUE, id=4, code="finacle_db"):
    return SimpleNamespace(id=id, code=code, parser_type=parser)


@pytest.fixture
def service(monkeypatch):
    """A service whose every database/datamart call is stubbed; each test
    overrides only what it cares about."""
    svc = RcpLinkService()
    monkeypatch.setattr(svc, "_find_entries", lambda db, model, msgids, flow_id, *a, **k: {})
    monkeypatch.setattr(svc, "_committed_claims", lambda db, msgids: {})
    monkeypatch.setattr(svc, "resolve_payments", lambda db, cid, pos, *a, **k: ({}, ""))
    monkeypatch.setattr(
        svc, "targets_for_pos",
        lambda db, pos, payments, *, flow_id, flow_source_id, parser_type: {},
    )
    monkeypatch.setattr(svc, "_fill_flow_codes", lambda db, proposals: None)
    return svc


def _analyze(service, movements, dump, **kwargs):
    return service.analyze(
        None,
        link_file=("link.xlsx", _link_workbook(movements)),
        dump_files=[("return_o.csv", dump)],
        **kwargs,
    )


DUMP = (
    b"entitySrlNum;origEntityId;returnSttlmAmt;msgid\n"
    b"SRL1;000008957379;3000.00;BLK1\n"
    b"SRL2;000008957555;2000.00;BLK1\n"
)
MOVEMENT = ["2026-07-17", "SCTXB", "RCP", "I", 2, 5000, "2026-07-17", "PF0045040", "BLK1"]


def test_report_validates_against_the_schema(service):
    report = _analyze(service, [MOVEMENT], DUMP)

    parsed = RcpAnalyzeResponse.model_validate(report)
    assert parsed.control_summary["OK"] == 1
    assert parsed.link_file.rows == 1
    assert parsed.dump_files[0].rows_with_msgid == 2


def test_a_movement_with_targets_is_proposed(service, monkeypatch):
    entry = _entry("BLK1")
    monkeypatch.setattr(
        service, "_find_entries",
        lambda db, model, msgids, flow_id, *a, **k: (
            {"BLK1": [(entry, _source())]} if model.__name__ == "ReconciliationEntry" else {}
        ),
    )
    monkeypatch.setattr(
        service, "targets_for_pos",
        lambda db, pos, payments, *, flow_id, flow_source_id, parser_type: {
            "000008957379": {
                "target_id": "lot-a", "target_kind": TARGET_LOT, "bucket_kind": BUCKET_PAIR,
                "bucket_pacs008": "PACS1", "bucket_msgid": "MSG1", "bucket_po": "",
                "bucket_ref": "", "label": "PAIR:PACS1|MSG1", "resolved_via": "datamart",
            },
            "000008957555": {
                "target_id": "lot-b", "target_kind": TARGET_LOT, "bucket_kind": BUCKET_PAIR,
                "bucket_pacs008": "PACS2", "bucket_msgid": "MSG1", "bucket_po": "",
                "bucket_ref": "", "label": "PAIR:PACS2|MSG1", "resolved_via": "lot_key",
            },
        },
    )

    proposal = _analyze(service, [MOVEMENT], DUMP)["proposals"][0]

    assert proposal["status"] == ST_PROPOSED
    assert proposal["control_status"] == "OK"
    assert [t["target_id"] for t in proposal["targets"]] == ["lot-a", "lot-b"]
    assert proposal["target_kind"] == TARGET_LOT
    assert proposal["resolved_amount"] == Decimal("5000.00")
    # The whole booked amount is redistributed — the split is exact.
    assert proposal["resolved_amount"] == abs(proposal["entry"]["amount"])


def test_a_movement_whose_payments_find_no_lot_is_not_proposed(service, monkeypatch):
    entry = _entry("BLK1")
    monkeypatch.setattr(
        service, "_find_entries",
        lambda db, model, msgids, flow_id, *a, **k: (
            {"BLK1": [(entry, _source())]} if model.__name__ == "ReconciliationEntry" else {}
        ),
    )

    proposal = _analyze(service, [MOVEMENT], DUMP)["proposals"][0]

    assert proposal["status"] == ST_NO_TARGET
    assert len(proposal["unresolved_payments"]) == 2


def test_an_emarged_movement_is_a_warning_not_an_action(service, monkeypatch):
    """Émargé history is never rewritten — the tool reports and stops."""
    entry = _entry("BLK1")
    monkeypatch.setattr(
        service, "_find_entries",
        lambda db, model, msgids, flow_id, *a, **k: (
            {} if model.__name__ == "ReconciliationEntry" else {"BLK1": [(entry, _source())]}
        ),
    )

    proposal = _analyze(service, [MOVEMENT], DUMP)["proposals"][0]

    assert proposal["status"] == ST_EMARGED
    assert proposal["targets"] == []


def test_two_live_movements_for_one_msgid_are_ambiguous(service, monkeypatch):
    monkeypatch.setattr(
        service, "_find_entries",
        lambda db, model, msgids, flow_id, *a, **k: (
            {"BLK1": [(_entry("BLK1"), _source()), (_entry("BLK1"), _source())]}
            if model.__name__ == "ReconciliationEntry" else {}
        ),
    )

    proposal = _analyze(service, [MOVEMENT], DUMP)["proposals"][0]

    assert proposal["status"] == ST_ENTRY_AMBIGUOUS
    assert len(proposal["candidates"]) == 2


def test_a_movement_with_no_dump_row_stops_before_any_lookup(service):
    orphan = ["2026-07-17", "SCTXB", "RCP", "I", 1, 100, "2026-07-17", "PF1", "BLK9"]

    proposal = _analyze(service, [orphan], DUMP)["proposals"][0]

    assert proposal["status"] == ST_NO_DUMP_ROWS
    assert proposal["control_status"] == "NOT_FOUND"


def test_datamart_failure_is_reported_not_swallowed(service, monkeypatch):
    monkeypatch.setattr(
        service, "resolve_payments", lambda db, cid, pos, *a, **k: ({}, "datamart injoignable: boom")
    )

    report = _analyze(service, [MOVEMENT], DUMP, connection_id=1)

    assert "boom" in report["datamart_error"]


def test_the_endpoint_starts_a_job_instead_of_running_the_batch(monkeypatch):
    """The upload contract the UI is built on — a single link file, N payment
    dumps, connection/flow as form fields — and the answer is a JOB, not the
    report: running the batch inside the request is what produced the 504.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.v1 import deps
    from app.api.v1.endpoints import rcp_reattribution
    from app.models.user import User
    from app.services import rcp_job_service as job_module

    captured = {}

    def fake_start(kind, work, **kwargs):
        captured["kind"] = kind
        captured["work"] = work
        captured.update(kwargs)
        return job_module.Job(id="job-1", kind=kind)

    monkeypatch.setattr(rcp_reattribution.rcp_job_service, "start", fake_start)
    monkeypatch.setattr(
        rcp_reattribution.rcp_link_service, "analyze",
        lambda db, **kwargs: captured.setdefault("analyze", kwargs) or {"ok": True},
    )
    app = FastAPI()
    app.include_router(rcp_reattribution.router, prefix="/rcp")
    app.dependency_overrides[deps.get_db] = lambda: None
    app.dependency_overrides[deps.get_current_active_superuser] = lambda: User(
        id=1, email="ops@example.com"
    )
    client = TestClient(app)
    workbook = _link_workbook([MOVEMENT])

    response = client.post(
        "/rcp/analyze",
        files=[
            ("link_file", ("SP_LINK_REPORT.xlsx", workbook)),
            ("dump_files", ("return_o.csv", b"msgid\nBLK1\n")),
            ("dump_files", ("reject_o.csv", b"msgid\nBLK2\n")),
        ],
        data={"connection_id": "3", "flow_id": "16"},
    )

    assert response.status_code == 202
    assert response.json()["job_id"] == "job-1"
    assert response.json()["status"] == "running"
    assert captured["kind"] == "analyze"
    # The run is labelled with what it was fed, so the history list is readable.
    assert "SP_LINK_REPORT.xlsx" in captured["label"]
    assert captured["user_id"] == 1

    # The files must be READ in the request: the UploadFile is closed once the
    # response is sent, so the worker could never read it afterwards.
    captured["work"](None, lambda *a, **k: None)
    assert [name for name, _ in captured["analyze"]["dump_files"]] == [
        "return_o.csv", "reject_o.csv",
    ]
    assert captured["analyze"]["connection_id"] == 3
    assert captured["analyze"]["flow_id"] == 16

    # Without a payment dump there is nothing to match against.
    refused = client.post("/rcp/analyze", files=[("link_file", ("x.xlsx", workbook))])
    assert refused.status_code == 400


def test_the_two_flows_coexist_in_one_report(monkeypatch, service):
    """The same run carries a batch-booking movement and a classic one; each
    proposal says which flow it came from and what it targets. Nothing here
    reads the msgid shape or the direction."""
    bb_entry, classic_entry = _entry("BLK1"), _entry("NUM1")
    sources = {
        "BLK1": _source(ParserType.FINACLE_BATCH_BOOKING_TRUE, id=4, code="finacle_db"),
        "NUM1": _source(ParserType.FINACLE_DB, id=5, code="finacle_db"),
    }
    monkeypatch.setattr(
        service, "_find_entries",
        lambda db, model, msgids, flow_id, *a, **k: (
            {"BLK1": [(bb_entry, sources["BLK1"])], "NUM1": [(classic_entry, sources["NUM1"])]}
            if model.__name__ == "ReconciliationEntry" else {}
        ),
    )
    monkeypatch.setattr(
        service, "targets_for_pos",
        lambda db, pos, payments, *, flow_id, flow_source_id, parser_type: (
            {"000008957379": {"target_id": "lot-a", "target_kind": TARGET_LOT,
                              "label": "PAIR:P|M", "resolved_via": "datamart"},
             "000008957555": {"target_id": "lot-a", "target_kind": TARGET_LOT,
                              "label": "PAIR:P|M", "resolved_via": "datamart"}}
            if parser_type == ParserType.FINACLE_BATCH_BOOKING_TRUE
            else {"000008957379": {"target_id": "PACS-X", "target_kind": TARGET_RECO,
                                   "label": "PACS-X", "resolved_via": "datamart"},
                  "000008957555": {"target_id": "PACS-X", "target_kind": TARGET_RECO,
                                   "label": "PACS-X", "resolved_via": "datamart"}}
        ),
    )
    classic = ["2026-07-16", "SCTXB", "RCP", "O", 2, 5000, "2026-07-16", "PF2", "NUM1"]
    dump = DUMP + (
        b"SRL3;000008957379;3000.00;NUM1\n"
        b"SRL4;000008957555;2000.00;NUM1\n"
    )

    proposals = _analyze(service, [MOVEMENT, classic], dump)["proposals"]

    by_msgid = {p["msgid"]: p for p in proposals}
    assert by_msgid["BLK1"]["target_kind"] == TARGET_LOT
    assert by_msgid["BLK1"]["targets"][0]["target_id"] == "lot-a"
    assert by_msgid["NUM1"]["target_kind"] == TARGET_RECO
    assert by_msgid["NUM1"]["targets"][0]["target_id"] == "PACS-X"
    assert all(p["status"] == ST_PROPOSED for p in proposals)
    assert by_msgid["NUM1"]["source_code"] == "finacle_db"


def test_a_movement_on_a_non_finacle_parser_is_not_touched(monkeypatch, service):
    """Defensive: a source that is neither batch-booking nor classic finacle
    has no known target semantics, so the tool says so instead of guessing."""
    monkeypatch.setattr(
        service, "_find_entries",
        lambda db, model, msgids, flow_id, *a, **k: (
            {"BLK1": [(_entry("BLK1"), _source(ParserType.MT940))]}
            if model.__name__ == "ReconciliationEntry" else {}
        ),
    )

    proposal = _analyze(service, [MOVEMENT], DUMP)["proposals"][0]

    assert proposal["status"] == "FLOW_UNSUPPORTED"
    assert proposal["targets"] == []


def test_the_workbook_breakdown_reports_every_service(service):
    """RCC/RRS/WCC are treated like RCP; NCP is counted and dropped."""
    rows = [
        ["2026-07-17", "SCTXB", "RCP", "I", 2, 5000, "d", "PF1", "BLK1"],
        ["2026-07-17", "SCTXB", "RRS", "O", 1, 100, "d", "PF2", "RRS1"],
        ["2026-07-17", "SCTXB", "RCC", "O", 1, 50, "d", "PF3", "RCC1"],
        ["2026-07-17", "SCTXB", "WCC", "O", 1, 25, "d", "PF4", "WCC1"],
        ["2026-07-17", "SCTXB", "NCP", "I", 9, 999, "d", "PF5", "NCP1"],
    ]

    report = _analyze(service, rows, DUMP)

    assert report["link_file"]["services"] == {"RCP": 1, "RRS": 1, "RCC": 1, "WCC": 1, "NCP": 1}
    assert {p["msgid"] for p in report["proposals"]} == {"BLK1", "RRS1", "RCC1", "WCC1"}
    assert {p["service_id"] for p in report["proposals"]} == {"RCP", "RRS", "RCC", "WCC"}


def _lot_targets(db, pos, payments, *, flow_id, flow_source_id, parser_type):
    return {
        "000008957379": {
            "target_id": "lot-a", "target_kind": TARGET_LOT, "bucket_kind": BUCKET_PAIR,
            "bucket_pacs008": "PACS1", "bucket_msgid": "MSG1", "bucket_po": "",
            "bucket_ref": "", "label": "PAIR:PACS1|MSG1", "resolved_via": "datamart",
        },
        "000008957555": {
            "target_id": "lot-b", "target_kind": TARGET_LOT, "bucket_kind": BUCKET_PAIR,
            "bucket_pacs008": "PACS2", "bucket_msgid": "MSG1", "bucket_po": "",
            "bucket_ref": "", "label": "PAIR:PACS2|MSG1", "resolved_via": "datamart",
        },
    }


def test_a_committed_movement_is_proposed_for_replay(service, monkeypatch):
    """The re-ingestion guard keeps the movement withdrawn, so the analysis
    reads it back from ``movement_split`` instead of declaring it untouchable."""
    parent = SimpleNamespace(
        source_hash="a" * 64, flow_id=16, flow_source_id=4, movement_type="SCTXB",
        external_ref="PF0045040", account="0010130015001", currency="EUR",
        amount=Decimal("5000.00"), direction="credit", value_date=NOW,
        operation_date=NOW, transaction_particulars="SCTXB/I/BLK1", ref_no=None,
        remarks_1="BLK1", payload_raw={}, claim_key_type=CLAIM_TYPE,
        claim_key_value="BLK1",
    )
    monkeypatch.setattr(service, "_committed_claims", lambda db, msgids: {"BLK1": parent})
    monkeypatch.setattr(service, "_sources_by_id", lambda db, ids: {4: _source()})
    monkeypatch.setattr(service, "targets_for_pos", _lot_targets)

    proposal = _analyze(service, [MOVEMENT], DUMP)["proposals"][0]

    assert proposal["status"] == ST_TO_REPLAY
    assert [t["target_id"] for t in proposal["targets"]] == ["lot-a", "lot-b"]
    assert proposal["resolved_amount"] == Decimal("5000.00")
    # The movement is described, without a live row to point at.
    assert proposal["entry"]["id"] is None
    assert proposal["entry"]["status"] == ENTRY_STATUS_REPLACED
    assert proposal["entry"]["source_hash"] == "a" * 64
    assert proposal["target_kind"] == TARGET_LOT
    RcpAnalyzeResponse(**_analyze(service, [MOVEMENT], DUMP))


def test_a_committed_movement_whose_source_vanished_is_not_replayable(service, monkeypatch):
    parent = SimpleNamespace(
        source_hash="a" * 64, flow_id=16, flow_source_id=99,
        transaction_particulars="SCTXB/I/BLK1", claim_key_value="BLK1",
    )
    monkeypatch.setattr(service, "_committed_claims", lambda db, msgids: {"BLK1": parent})
    monkeypatch.setattr(service, "_sources_by_id", lambda db, ids: {})

    proposal = _analyze(service, [MOVEMENT], DUMP)["proposals"][0]

    assert proposal["status"] == "ALREADY_COMMITTED"
    assert proposal["targets"] == []


def test_the_report_says_how_many_duplicate_rows_it_dropped(service):
    """Six cumulative daily extracts ship the same rows again and again; the
    operator has to see the collapse confirmed."""
    report = service.analyze(
        None,
        link_file=("link.xlsx", _link_workbook([MOVEMENT])),
        dump_files=[("d1.csv", DUMP), ("d2.csv", DUMP)],
    )

    assert report["summary"]["dump_rows_read"] == 4
    assert report["summary"]["dump_duplicates"] == 2
    assert report["controls"][0]["found_count"] == 2
