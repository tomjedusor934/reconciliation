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
from app.api.v1.endpoints import splits as splits_endpoint
from app.api.v1.endpoints import tasks_lots as tasks_lots_endpoint
from app.services.lot_service import lot_service
from app.services.split_service import split_service

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
    "bucket_kind": "PAIR",
    "bucket_pacs008": "PACS1",
    "bucket_msgid": "MSGA",
    "bucket_po": None,
    "bucket_ref": None,
    "synthetic_only": False,
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
    app.include_router(splits_endpoint.router, prefix="/splits")
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
        {"key_type": "PACS008", "distinct_count": 1, "member_link_count": 51994},
        {"key_type": "MSGID", "distinct_count": 12, "member_link_count": 51990},
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
            "pending_payment_amount": Decimal("0"),
        },
        {
            "movement_type": "SWIFT",
            "direction": "debit",
            "member_count": 10,
            "total_amount": Decimal("-10.00"),
            "pending_count": 0,
            "matched_count": 10,
            "excluded_count": 0,
            "pending_payment_amount": Decimal("0"),
        },
    ],
    "edges": [
        {"movement_type": "NDGB", "direction": "credit", "key_type": "PACS008"},
    ],
    "meta": {
        "member_count": 52000,
        "type_counts": {"NDGB": 51990, "SWIFT": 10},
        "pending_payment_amount": Decimal("999.00"),
        "pending_payment_count": 3,
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
    assert body["groups"][0]["member_count"] == 51990
    assert body["edges"][0]["key_type"] == "PACS008"
    assert body["meta"]["pending_payment_count"] == 3
    assert client.get("/lots/unknown-lot/graph").status_code == 404


def test_get_lot_graph_scopes_to_a_key_value(client, monkeypatch):
    captured = {}

    def _graph(db, lot_id, key_type=None, key_value=None):
        captured.update(lot_id=lot_id, key_type=key_type, key_value=key_value)
        return GRAPH

    monkeypatch.setattr(lot_service, "get_lot_graph", _graph)
    resp = client.get(
        f"/lots/{GRAPH['lot_id']}/graph", params={"key_type": "MSGID", "key_value": "MSGA"}
    )
    assert resp.status_code == 200
    assert captured["key_type"] == "MSGID" and captured["key_value"] == "MSGA"
    assert client.get(
        f"/lots/{GRAPH['lot_id']}/graph", params={"key_type": "NOPE"}
    ).status_code == 422


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


LOT_ID = "11111111-1111-4111-8111-111111111111"


def _batch_payload(**overrides):
    payload = {
        "flow_code": "float_account_outward",
        "source_code": "finacle_db",
        "lots": [
            {
                "lot_id": LOT_ID,
                "bucket_kind": "PAIR",
                "bucket_pacs008": "PACS1",
                "bucket_msgid": "MSGA",
            }
        ],
        "members": [
            {
                "lot_id": LOT_ID,
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


def _split_payload(**overrides):
    payload = {
        "flow_code": "float_account_outward",
        "source_code": "finacle_db",
        "run_id": 42,
        "groups": [
            {
                "claim_key_type": "PACS008",
                "claim_key_value": "PACS1",
                "account": "0010130015001",
                "currency": "EUR",
                "value_date": "2026-07-01T09:30:00",
                "event_type": "TR",
                "parents": [
                    {
                        "movement_type": "SCTXB",
                        "external_ref": "S1",
                        "account": "0010130015001",
                        "currency": "EUR",
                        "amount": "-1000.00",
                        "direction": "debit",
                        "value_date": "2026-07-01T09:30:00",
                        "payment_count": 3,
                        "payment_amount": "-1000.00",
                        "shared_key_movements": 1,
                    }
                ],
                "children": [
                    {
                        "external_ref": "KEY:PACS1~aaaaaaaa~aaaaaaaaaa",
                        "lot_id": LOT_ID,
                        "amount": "-700.00",
                        "direction": "debit",
                        "payment_count": 2,
                        "bucket_kind": "PAIR",
                        "bucket_pacs008": "PACS1",
                        "bucket_msgid": "MSGA",
                    },
                    {
                        "external_ref": "KEY:PACS1~aaaaaaaa~bbbbbbbbbb",
                        "lot_id": "22222222-2222-4222-8222-222222222222",
                        "amount": "-300.00",
                        "direction": "debit",
                        "payment_count": 1,
                        "bucket_kind": "PAIR",
                        "bucket_pacs008": "PACS1",
                        "bucket_msgid": "MSGB",
                    },
                ],
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
    result = {"lots_created": 1, "members_inserted": 1, "members_updated": 0,
              "keys_added": 1}
    monkeypatch.setattr(lot_service, "apply_lot_batch", lambda db, **kw: result)
    resp = client.post("/tasks/finacle-bb/lots/batch", json=_batch_payload())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "detail": None, "data": result}


def test_lot_batch_carries_the_bucket_identity_through(client, monkeypatch):
    """The bucket identity is what the lot row records; dropping it silently
    would leave every lot looking LEGACY."""
    _patch_flow_lookup(monkeypatch)
    captured = {}

    def _apply(db, **kw):
        captured.update(kw)
        return {"lots_created": 1, "members_inserted": 1, "members_updated": 0,
                "keys_added": 1}

    monkeypatch.setattr(lot_service, "apply_lot_batch", _apply)
    assert client.post("/tasks/finacle-bb/lots/batch", json=_batch_payload()).status_code == 200
    lot = captured["lots"][0]
    assert (lot.bucket_kind, lot.bucket_pacs008, lot.bucket_msgid) == ("PAIR", "PACS1", "MSGA")
    # Absent components arrive as '' rather than None — uq_movement_lot_bucket
    # would not bite on NULLs.
    assert lot.bucket_po == "" and lot.bucket_ref == ""


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


# ---------------------------------------------------------------------------
# splits
# ---------------------------------------------------------------------------

def test_split_batch_ok(client, monkeypatch):
    _patch_flow_lookup(monkeypatch)
    result = {"groups": 1, "parents_inserted": 1, "parents_updated": 0,
              "ghosts_inserted": 2, "ghosts_updated": 0, "ghosts_skipped": 0,
              "movements_withdrawn": 1, "parents_emarged": 0, "ghosts_reaped": 0}
    monkeypatch.setattr(split_service, "apply_split_batch", lambda db, **kw: result)
    resp = client.post("/tasks/finacle-bb/splits/batch", json=_split_payload())
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "detail": None, "data": result}


def test_split_batch_forwards_run_id_and_groups(client, monkeypatch):
    _patch_flow_lookup(monkeypatch)
    captured = {}

    def _apply(db, **kw):
        captured.update(kw)
        return {}

    monkeypatch.setattr(split_service, "apply_split_batch", _apply)
    assert client.post("/tasks/finacle-bb/splits/batch", json=_split_payload()).status_code == 200
    assert captured["run_id"] == 42
    group = captured["groups"][0]
    assert (group.claim_key_type, group.claim_key_value) == ("PACS008", "PACS1")
    assert len(group.parents) == 1 and len(group.children) == 2
    # Ghosts are the buckets' exact payment sums — with the payments matching
    # the booking here, they add up to the group's parents.
    assert sum(c.amount for c in group.children) == group.parents[0].amount


def test_split_batch_unknown_flow_404(client, monkeypatch):
    _patch_flow_lookup(monkeypatch, found=False)
    assert client.post(
        "/tasks/finacle-bb/splits/batch", json=_split_payload()
    ).status_code == 404


def test_get_split_returns_parent_group_and_children(client, monkeypatch):
    detail = {
        "parent": {
            "source_hash": "h" * 64,
            "flow_id": 1,
            "movement_type": "SCTXB",
            "external_ref": "S1",
            "account": "0010130015001",
            "currency": "EUR",
            "amount": Decimal("-1000.00"),
            "direction": "debit",
            "value_date": NOW,
            "operation_date": NOW,
            "transaction_particulars": "SCTXB/O/x",
            "ref_no": None,
            "remarks_1": "PACS1",
            "payment_count": 3,
            "claim_key_type": "PACS008",
            "claim_key_value": "PACS1",
            "parent_emarged": False,
        },
        "group": {
            "claim_key_type": "PACS008",
            "claim_key_value": "PACS1",
            "canonical_source_hash": "h" * 64,
            "parents": [
                {
                    "source_hash": "h" * 64,
                    "movement_type": "SCTXB",
                    "external_ref": "S1",
                    "amount": Decimal("-1000.00"),
                    "currency": "EUR",
                    "value_date": NOW,
                    "parent_emarged": False,
                }
            ],
            "parent_total": Decimal("-1000.00"),
            "children_total": Decimal("-990.00"),
            "delta": Decimal("-10.00"),
            "payment_amount": Decimal("-990.00"),
        },
        "children": [
            {
                "entry_id": 7,
                "source_hash": "c" * 64,
                "lot_id": LOT_ID,
                "amount": Decimal("-990.00"),
                "currency": "EUR",
                "direction": "debit",
                "value_date": NOW,
                "external_ref": "KEY:PACS1~aaaaaaaa~aaaaaaaaaa",
                "entry_status": "pending",
                "match_group_id": None,
                "payment_count": 3,
                "bucket_kind": "PAIR",
                "bucket_pacs008": "PACS1",
                "bucket_msgid": "MSGA",
                "bucket_po": None,
                "bucket_ref": None,
                "synthetic_only": False,
            }
        ],
    }
    monkeypatch.setattr(split_service, "get_split", lambda db, **kw: detail)
    resp = client.get(f"/splits/{'h' * 64}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["parent"]["external_ref"] == "S1"
    assert body["children"][0]["bucket_msgid"] == "MSGA"
    assert body["group"]["delta"] == "-10.00"
    assert body["group"]["parents"][0]["source_hash"] == "h" * 64


def test_get_split_unknown_404(client, monkeypatch):
    monkeypatch.setattr(split_service, "get_split", lambda db, **kw: None)
    assert client.get(f"/splits/{'z' * 64}").status_code == 404
