"""Endpoint contract tests for the payment-status internal router (DB-free).

Same harness as test_lots_endpoints: a throwaway FastAPI app mounts the real
router with dependencies overridden and the service monkeypatched — no
Postgres, app.main never imported.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import deps
from app.api.v1.endpoints import tasks_payment_status as ps_endpoint
from app.services.payment_status_service import payment_status_service


class _FakeFlow:
    id = 1
    code = "float_account_outward"


@pytest.fixture()
def client(monkeypatch):
    app = FastAPI()
    app.include_router(ps_endpoint.router, prefix="/tasks")
    app.dependency_overrides[deps.get_db] = lambda: None
    app.dependency_overrides[deps.verify_internal_token] = lambda: None
    monkeypatch.setattr(
        ps_endpoint.flow_service,
        "get_flow_by_code",
        lambda db, code: _FakeFlow() if code == _FakeFlow.code else None,
    )
    return TestClient(app)


def _batch_payload(**overrides):
    payload = {
        "flow_code": "float_account_outward",
        "rows": [
            {
                "reco_id": "3a0c9e10-lot-uuid-1",
                "po_id": "000008056151",
                "status": "ACC",
                "amount": "1234.56",
            },
            {
                "reco_id": "7b1d2f20-lot-uuid-2",
                "po_id": "000008245621",
                "status": None,
            },
        ],
    }
    payload.update(overrides)
    return payload


def test_batch_ok_passthrough(client, monkeypatch):
    captured = {}

    def fake_apply(db, *, flow, rows):
        captured["flow_code"] = flow.code
        captured["rows"] = rows
        return {"inserted": 1, "updated": 1, "unchanged": 0, "skipped": 0}

    monkeypatch.setattr(payment_status_service, "apply_status_batch", fake_apply)
    resp = client.post("/tasks/finacle/payment-status/batch", json=_batch_payload())
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True, "detail": None,
        "data": {"inserted": 1, "updated": 1, "unchanged": 0, "skipped": 0},
    }
    assert captured["flow_code"] == "float_account_outward"
    assert len(captured["rows"]) == 2
    assert captured["rows"][0].reco_id == "3a0c9e10-lot-uuid-1"
    assert captured["rows"][0].po_id == "000008056151"
    assert captured["rows"][0].status == "ACC"
    assert captured["rows"][1].amount is None
    assert captured["rows"][1].status is None


def test_batch_unknown_flow_404(client):
    resp = client.post(
        "/tasks/finacle/payment-status/batch", json=_batch_payload(flow_code="nope")
    )
    assert resp.status_code == 404


def test_batch_validates_rows(client):
    bad = _batch_payload()
    bad["rows"][0]["po_id"] = ""
    assert client.post("/tasks/finacle/payment-status/batch", json=bad).status_code == 422
    bad = _batch_payload()
    del bad["rows"][0]["reco_id"]
    assert client.post("/tasks/finacle/payment-status/batch", json=bad).status_code == 422


def test_movements_passthrough_and_404(client, monkeypatch):
    captured = {}

    def fake_list(db, *, flow, scope, skip, limit):
        captured.update(flow_code=flow.code, scope=scope, skip=skip, limit=limit)
        return [
            {
                "reco_id": "lot-uuid-1",
                "external_ref": "T1#2",
                "account": "001",
                "value_date": "2026-07-06T00:00:00",
                "operation_date": None,
                "transaction_particulars": "SCTXB/NCP/O/PO1/x",
                "ref_no": "PO1",
                "remarks_1": "LU12",
                "initiating_channel": None,
            }
        ]

    monkeypatch.setattr(payment_status_service, "list_movements", fake_list)
    ok = client.get(
        "/tasks/finacle/payment-status/movements",
        params={"flow_code": "float_account_outward", "scope": "all",
                "skip": 2000, "limit": 500},
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["ok"] is True
    assert body["movements"][0]["reco_id"] == "lot-uuid-1"
    assert body["movements"][0]["external_ref"] == "T1#2"
    assert body["movements"][0]["ref_no"] == "PO1"
    assert captured == {"flow_code": "float_account_outward", "scope": "all",
                        "skip": 2000, "limit": 500}
    missing = client.get(
        "/tasks/finacle/payment-status/movements", params={"flow_code": "nope"}
    )
    assert missing.status_code == 404


def test_movements_validates_params(client):
    base = "/tasks/finacle/payment-status/movements"
    assert client.get(base, params={"flow_code": "float_account_outward",
                                    "scope": "bogus"}).status_code == 422
    assert client.get(base, params={"flow_code": "float_account_outward",
                                    "limit": 5000}).status_code == 422
