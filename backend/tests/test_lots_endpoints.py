"""Endpoint contract tests for the lot routers (DB-free).

A throwaway FastAPI app mounts the real routers with every dependency
overridden (db → None, auth → stub) and the service monkeypatched, so the
tests exercise routing/validation/serialization without Postgres. app.main is
never imported (it connects to the DB at import time).
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import deps
from app.api.v1.endpoints import lots as lots_endpoint
from app.api.v1.endpoints import tasks_lots as tasks_lots_endpoint
from app.services.lot_service import CrossLotKeyConflict, lot_service

NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc)

LOT_SUMMARY = {
    "lot_id": "11111111-1111-4111-8111-111111111111",
    "flow_id": 1,
    "flow_source_id": 2,
    "currency": "EUR",
    "currencies": ["EUR"],
    "status": "pending",
    "is_balanced": False,
    "member_count": 3,
    "pending_count": 3,
    "matched_count": 0,
    "excluded_count": 0,
    "total_debit": Decimal("-100.00"),
    "total_credit": Decimal("60.00"),
    "net_amount": Decimal("-40.00"),
    "first_value_date": NOW,
    "last_value_date": NOW,
    "merge_conflict": False,
    "merged_into_lot_id": None,
    "created_at": NOW,
    "updated_at": NOW,
}


class _StubUser:
    id = 1
    is_active = True


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(lots_endpoint.router, prefix="/lots")
    app.include_router(tasks_lots_endpoint.router, prefix="/tasks")
    app.dependency_overrides[deps.get_db] = lambda: None
    app.dependency_overrides[deps.get_current_active_user] = lambda: _StubUser()
    app.dependency_overrides[deps.verify_internal_token] = lambda: None
    return TestClient(app)


def test_list_lots_shape_and_filter_passthrough(client, monkeypatch):
    captured = {}

    def fake_list_lots(db, **kwargs):
        captured.update(kwargs)
        return [dict(LOT_SUMMARY)], 1

    monkeypatch.setattr(lot_service, "list_lots", fake_list_lots)
    resp = client.get(
        "/lots/",
        params={
            "flow_id": 7, "status": "pending", "balanced": "false",
            "date_from": "2026-07-01", "date_to": "2026-07-10",
            "search": "MSG", "skip": 0, "limit": 25,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert body["items"][0]["lot_id"] == LOT_SUMMARY["lot_id"]
    assert body["items"][0]["status"] == "pending"
    assert captured["flow_id"] == 7
    assert captured["status"] == "pending"
    assert captured["balanced"] is False
    assert captured["search"] == "MSG"
    assert captured["limit"] == 25


def test_list_lots_rejects_unknown_status(client):
    resp = client.get("/lots/", params={"status": "bogus"})
    assert resp.status_code == 422


def test_get_lot_detail_and_404(client, monkeypatch):
    detail = {
        "lot": dict(LOT_SUMMARY),
        "members": [
            {
                "id": 10,
                "source_hash": "a" * 64,
                "movement_type": "SCTXB",
                "external_ref": "S1",
                "account": "0010130015001",
                "currency": "EUR",
                "amount": Decimal("-100.00"),
                "direction": "debit",
                "value_date": NOW,
                "operation_date": NOW,
                "transaction_particulars": "SCTXB/O/x",
                "ref_no": None,
                "remarks_1": "PACS1",
                "entry_status": "pending",
                "entry_id": 1000,
                "match_group_id": None,
            }
        ],
        "keys": [{"id": 5, "member_id": 10, "key_type": "PACS008", "key_value": "PACS1"}],
    }
    monkeypatch.setattr(
        lot_service, "get_lot_detail",
        lambda db, lot_id: detail if lot_id == LOT_SUMMARY["lot_id"] else None,
    )
    ok = client.get(f"/lots/{LOT_SUMMARY['lot_id']}")
    assert ok.status_code == 200
    assert ok.json()["members"][0]["entry_status"] == "pending"
    assert ok.json()["keys"][0]["key_type"] == "PACS008"
    assert client.get("/lots/unknown-lot").status_code == 404


def test_get_lot_detail_giant_lot_flags_members_omitted(client, monkeypatch):
    detail = {
        "lot": dict(LOT_SUMMARY, member_count=52000),
        "members": [],
        "keys": [],
        "members_included": False,
    }
    monkeypatch.setattr(lot_service, "get_lot_detail", lambda db, lot_id: detail)
    resp = client.get(f"/lots/{LOT_SUMMARY['lot_id']}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["members_included"] is False
    assert body["members"] == []
    assert body["lot"]["member_count"] == 52000


GRAPH = {
    "lot_id": "11111111-1111-4111-8111-111111111111",
    "key_types": [
        {"key_type": "PACS008", "distinct_count": 51994, "member_link_count": 52000}
    ],
    "groups": [
        {
            "movement_type": "NDGB",
            "direction": "credit",
            "member_count": 51990,
            "total_amount": Decimal("123456.78"),
            "pending_count": 51990,
            "matched_count": 0,
            "excluded_count": 0,
        },
        {
            "movement_type": "SWIFT",
            "direction": "debit",
            "member_count": 10,
            "total_amount": Decimal("-10.00"),
            "pending_count": 0,
            "matched_count": 10,
            "excluded_count": 0,
        },
    ],
    "edges": [
        {"movement_type": "NDGB", "direction": "credit", "key_type": "PACS008"}
    ],
    "meta": {
        "member_count": 52000,
        "type_counts": {"NDGB": 51990, "SWIFT": 10},
    },
}


def test_get_lot_graph_shape_and_404(client, monkeypatch):
    monkeypatch.setattr(
        lot_service, "get_lot_graph",
        lambda db, lot_id, key_type=None, key_value=None: (
            GRAPH if lot_id == GRAPH["lot_id"] else None
        ),
    )
    ok = client.get(f"/lots/{GRAPH['lot_id']}/graph")
    assert ok.status_code == 200
    body = ok.json()
    assert body["key_types"][0]["key_type"] == "PACS008"
    assert body["key_types"][0]["distinct_count"] == 51994
    assert body["groups"][0]["member_count"] == 51990
    assert body["groups"][0]["direction"] == "credit"
    assert body["edges"][0]["key_type"] == "PACS008"
    assert body["meta"]["type_counts"]["NDGB"] == 51990
    assert client.get("/lots/unknown-lot/graph").status_code == 404


def test_get_lot_graph_scoped_passthrough(client, monkeypatch):
    captured = {}

    def fake_graph(db, lot_id, key_type=None, key_value=None):
        captured.update(lot_id=lot_id, key_type=key_type, key_value=key_value)
        return GRAPH

    monkeypatch.setattr(lot_service, "get_lot_graph", fake_graph)
    resp = client.get(
        f"/lots/{GRAPH['lot_id']}/graph",
        params={"key_type": "PACS008", "key_value": "P1"},
    )
    assert resp.status_code == 200
    assert captured == {
        "lot_id": GRAPH["lot_id"],
        "key_type": "PACS008",
        "key_value": "P1",
    }


def test_list_lot_key_values_and_validation(client, monkeypatch):
    captured = {}

    def fake_key_values(db, **kwargs):
        if kwargs["lot_id"] != GRAPH["lot_id"]:
            return None
        captured.update(kwargs)
        return [{"key_value": "P1", "member_count": 2}], 51994

    monkeypatch.setattr(lot_service, "list_lot_key_values", fake_key_values)
    resp = client.get(
        f"/lots/{GRAPH['lot_id']}/keys",
        params={"key_type": "PACS008", "search": "P", "skip": 0, "limit": 100},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 51994
    assert body["items"][0]["key_value"] == "P1"
    assert captured["key_type"] == "PACS008"
    assert captured["search"] == "P"
    # key_type is required + pattern-validated
    assert client.get(f"/lots/{GRAPH['lot_id']}/keys").status_code == 422
    assert client.get(
        f"/lots/{GRAPH['lot_id']}/keys", params={"key_type": "NOPE"}
    ).status_code == 422
    assert client.get("/lots/unknown-lot/keys", params={"key_type": "PACS008"}).status_code == 404


def test_list_lot_members_filter_passthrough_and_404(client, monkeypatch):
    captured = {}

    def fake_list_members(db, **kwargs):
        if kwargs["lot_id"] != LOT_SUMMARY["lot_id"]:
            return None
        captured.update(kwargs)
        member = {
            "id": 10,
            "source_hash": "a" * 64,
            "movement_type": "NDGB",
            "currency": "EUR",
            "amount": Decimal("1.00"),
            "value_date": NOW,
            "entry_status": "pending",
        }
        return [member], 52000

    monkeypatch.setattr(lot_service, "list_lot_members", fake_list_members)
    resp = client.get(
        f"/lots/{LOT_SUMMARY['lot_id']}/members",
        params={
            "movement_type": "NDGB", "entry_status": "pending", "search": "REF",
            "key_type": "PACS008", "key_value": "P1", "skip": 200, "limit": 100,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 52000
    assert body["items"][0]["movement_type"] == "NDGB"
    assert captured["movement_type"] == "NDGB"
    assert captured["entry_status"] == "pending"
    assert captured["key_type"] == "PACS008"
    assert captured["key_value"] == "P1"
    assert captured["skip"] == 200
    assert captured["limit"] == 100
    assert client.get("/lots/unknown-lot/members").status_code == 404


def test_list_lot_members_validates_params(client):
    lot = LOT_SUMMARY["lot_id"]
    assert client.get(f"/lots/{lot}/members", params={"entry_status": "bogus"}).status_code == 422
    assert client.get(f"/lots/{lot}/members", params={"key_type": "NOPE"}).status_code == 422
    assert client.get(f"/lots/{lot}/members", params={"limit": 500}).status_code == 422
    assert client.get(f"/lots/{lot}/members", params={"skip": -1}).status_code == 422


def _batch_payload(**overrides):
    payload = {
        "flow_code": "float_account_outward",
        "source_code": "finacle_db",
        "lots": [{"lot_id": "11111111-1111-4111-8111-111111111111"}],
        "merges": [],
        "members": [
            {
                "lot_id": "11111111-1111-4111-8111-111111111111",
                "movement_type": "SCTXB",
                "external_ref": "S1",
                "account": "0010130015001",
                "currency": "EUR",
                "amount": "-100.00",
                "value_date": "2026-07-01T09:30:00",
                "direction": "debit",
                "keys": [{"key_type": "PACS008", "key_value": "PACS1"}],
            }
        ],
    }
    payload.update(overrides)
    return payload


class _FakeSource:
    def __init__(self, code):
        self.code = code


class _FakeFlow:
    def __init__(self):
        self.id = 1
        self.code = "float_account_outward"
        self.sources = [_FakeSource("finacle_db")]


def _patch_flow_lookup(monkeypatch, found=True):
    monkeypatch.setattr(
        tasks_lots_endpoint.flow_service,
        "get_flow_by_code",
        lambda db, code: _FakeFlow() if found else None,
    )


def test_lot_batch_ok(client, monkeypatch):
    _patch_flow_lookup(monkeypatch)
    result = {"lots_created": 1, "merges_applied": 0, "entries_relinked": 0,
              "members_inserted": 1, "members_updated": 0, "keys_added": 1}
    monkeypatch.setattr(lot_service, "apply_lot_batch", lambda db, **kw: result)
    resp = client.post("/tasks/finacle-bb/lots/batch", json=_batch_payload())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "detail": None, "data": result}


def test_lot_batch_conflict_maps_to_409(client, monkeypatch):
    _patch_flow_lookup(monkeypatch)

    def boom(db, **kw):
        raise CrossLotKeyConflict("key PACS008:PACS1 attached to two lots")

    monkeypatch.setattr(lot_service, "apply_lot_batch", boom)
    resp = client.post("/tasks/finacle-bb/lots/batch", json=_batch_payload())
    assert resp.status_code == 409
    assert "PACS1" in resp.json()["detail"]


def test_lot_batch_value_error_maps_to_400(client, monkeypatch):
    _patch_flow_lookup(monkeypatch)

    def boom(db, **kw):
        raise ValueError("members reference unknown lot(s)")

    monkeypatch.setattr(lot_service, "apply_lot_batch", boom)
    resp = client.post("/tasks/finacle-bb/lots/batch", json=_batch_payload())
    assert resp.status_code == 400


def test_lot_batch_unknown_flow_404(client, monkeypatch):
    _patch_flow_lookup(monkeypatch, found=False)
    resp = client.post("/tasks/finacle-bb/lots/batch", json=_batch_payload())
    assert resp.status_code == 404


def test_lot_batch_unknown_source_404(client, monkeypatch):
    _patch_flow_lookup(monkeypatch)
    resp = client.post(
        "/tasks/finacle-bb/lots/batch", json=_batch_payload(source_code="nope")
    )
    assert resp.status_code == 404


def test_key_map_requires_existing_source(client, monkeypatch):
    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    class _FakeDb:
        def query(self, *a, **k):
            return _Query()

    app_client = client
    app_client.app.dependency_overrides[deps.get_db] = lambda: _FakeDb()
    resp = app_client.get("/tasks/finacle-bb/lots/keys", params={"flow_source_id": 999})
    assert resp.status_code == 404
