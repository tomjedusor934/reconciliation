"""Contract tests for the flow-update error mapping (DB-free).

The behavioral fix itself (sources reconciled by code, ids preserved, account
rows replaced) is exercised against a real DB in dev; here we lock the API
contract: SourceHasMovementLots → 409, plain ValueError → 404.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.v1 import deps
from app.api.v1.endpoints import flows as flows_endpoint
from app.repositories.flow_repository import SourceHasMovementLots
from app.services.flow_service import flow_service


class _StubUser:
    id = 1
    is_active = True
    is_superuser = True


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(flows_endpoint.router, prefix="/flows")
    app.dependency_overrides[deps.get_db] = lambda: None
    app.dependency_overrides[deps.get_current_active_superuser] = lambda: _StubUser()
    return TestClient(app)


def test_update_flow_source_with_lots_maps_to_409(client, monkeypatch):
    def boom(db, *, flow_id, payload):
        raise SourceHasMovementLots(
            "source 'finacle_db' still has movement lots attached — "
            "deactivate it instead of removing it"
        )

    monkeypatch.setattr(flow_service, "update_flow", boom)
    resp = client.put("/flows/16", json={"is_active": False})
    assert resp.status_code == 409
    assert "movement lots" in resp.json()["detail"]


def test_update_flow_not_found_maps_to_404(client, monkeypatch):
    def boom(db, *, flow_id, payload):
        raise ValueError("Flow not found")

    monkeypatch.setattr(flow_service, "update_flow", boom)
    assert client.put("/flows/999", json={"is_active": False}).status_code == 404
