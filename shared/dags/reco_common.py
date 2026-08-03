"""Common helpers for reconciliation DAGs.

Every DAG calls back into the reconciliation backend (FastAPI) using the
internal token. The backend URL & token are injected via env vars from
docker-compose (`RECO_BACKEND_URL`, `RECO_BACKEND_INTERNAL_TOKEN`) so we can
swap the local Airflow for a corporate one transparently.

When running in an external Airflow without these env vars, the values are
read from Airflow Variables (`reco_backend_url`, `reco_backend_internal_token`).
"""
import os
from typing import Any, Dict, Optional

import requests

API_PREFIX = os.environ.get("RECO_BACKEND_API_PREFIX", "/api/v1")

# Reconcile and the émargement sweep are batches, not requests: the default 600 s
# was shorter than the run itself, so requests gave up, Airflow retried, and a
# 2nd run_auto started alongside the 1st — which FastAPI cannot cancel (sync
# endpoint in the threadpool). That is what deadlocked on 2026-07-16.
RECONCILE_TIMEOUT = int(os.environ.get("RECO_RECONCILE_TIMEOUT", "3600"))


def _get_backend_url() -> str:
    val = os.environ.get("RECO_BACKEND_URL")
    if val:
        return val.rstrip("/")
    try:
        from airflow.models import Variable
        return Variable.get("reco_backend_url", default_var="http://backend:8000").rstrip("/")
    except Exception:
        return "http://backend:8000"


def _get_internal_token() -> str:
    val = os.environ.get("RECO_BACKEND_INTERNAL_TOKEN")
    if val:
        return val
    try:
        from airflow.models import Variable
        return Variable.get("reco_backend_internal_token", default_var="change-me-internal-token")
    except Exception:
        return "change-me-internal-token"


def call_backend(
    method: str,
    path: str,
    *,
    json: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 600,
) -> Dict[str, Any]:
    url = f"{_get_backend_url()}{API_PREFIX}{path}"
    headers = {"X-Internal-Token": _get_internal_token()}
    resp = requests.request(
        method, url, json=json, params=params, headers=headers, timeout=timeout
    )
    resp.raise_for_status()
    if resp.content:
        return resp.json()
    return {}


def task_ingest(flow_code: str) -> Dict[str, Any]:
    return call_backend("POST", f"/tasks/ingest/{flow_code}")


def task_reconcile() -> Dict[str, Any]:
    return call_backend("POST", "/tasks/reconcile", timeout=RECONCILE_TIMEOUT)


def task_emargement() -> Dict[str, Any]:
    """Sweep remaining non-pending entries from live to émargement table."""
    return call_backend("POST", "/tasks/emargement", timeout=RECONCILE_TIMEOUT)


def task_list_active_flows() -> Dict[str, Any]:
    """Return active flows with source type info for the orchestrator."""
    return call_backend("POST", "/tasks/active-flows")


# ---------------------------------------------------------------------------
# Finacle ingestion — the DAG extracts from the datamart and pushes batches;
# the backend only persists entries and logs the run.
# ---------------------------------------------------------------------------

def task_list_finacle_sources() -> Dict[str, Any]:
    """Active finacle_db sources (accounts + watermark) of active flows."""
    return call_backend("GET", "/tasks/finacle/sources")

def finacle_list_unresolved(flow_code: str, source_code: str) -> Dict[str, Any]:
    """Live PENDING finacle entries with an unresolved reco_id (NULL / 'Not
    Supported') for this source — re-enriched and re-pushed each run."""
    return call_backend(
        "GET",
        "/tasks/finacle/unresolved",
        params={"flow_code": flow_code, "source_code": source_code},
    )


def finacle_start_run(flow_code: str, source_code: str, dag_run_id: Optional[str] = None) -> int:
    """Open a RUNNING ingestion run on the backend; returns its id."""
    result = call_backend(
        "POST",
        "/tasks/finacle/runs",
        json={"flow_code": flow_code, "source_code": source_code, "dag_run_id": dag_run_id},
    )
    return result["data"]["run_id"]


def finacle_post_batch(run_id: int, entries: list, errors: list) -> Dict[str, Any]:
    """Push one batch of formatted entries (JSON-safe dicts) to the backend."""
    return call_backend(
        "POST",
        f"/tasks/finacle/runs/{run_id}/batch",
        json={"entries": entries, "errors": errors},
    )


def finacle_get_bulk_keys(flow_code: str, source_code: str) -> Dict[str, str]:
    """{po_id -> reco_id} of the bulk bookings already detected, so compute_reco_id
    keys a bulk member correctly from pass 2 on — including movements arriving long
    after the run that discovered the bulk."""
    result = call_backend(
        "GET",
        "/tasks/finacle/bulk-keys",
        params={"flow_code": flow_code, "source_code": source_code},
    )
    return result.get("keys") or {}


def finacle_post_bulk_keys(
    flow_code: str,
    source_code: str,
    keys: Dict[str, str],
    init_modules: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Persist the {po_id -> reco_id} map AND re-key in place the flow's live
    PENDING entries still stored under a member po_id (finacle *and* file sources).
    ``init_modules`` ({po_id -> std.Payment.InitModule}) is traceability only.
    No re-push — source_hash untouched."""
    return call_backend(
        "POST",
        "/tasks/finacle/bulk-keys",
        json={
            "flow_code": flow_code,
            "source_code": source_code,
            "keys": keys,
            "init_modules": init_modules or {},
        },
    )


def finacle_complete_run(
    run_id: int, failed: bool = False, error: Optional[str] = None
) -> Dict[str, Any]:
    """Close the run (SUCCESS / PARTIAL / FAILED on the backend side)."""
    return call_backend(
        "POST",
        f"/tasks/finacle/runs/{run_id}/complete",
        json={"failed": failed, "error": error},
    )


# ---------------------------------------------------------------------------
# Finacle BATCH BOOKING TRUE — the DAG clusters movements into lots (shared
# PACS008/MSGID/PO keys) and persists the lots/members/keys backend-side.
# Entries themselves go through the regular /tasks/finacle/runs lifecycle.
# ---------------------------------------------------------------------------

def finacle_bb_get_key_map(flow_source_id: int) -> Dict[str, Any]:
    """key -> lot map of every ACTIVE lot of the source; seeds the union-find
    so late movements join lots created in previous runs."""
    return call_backend(
        "GET",
        "/tasks/finacle-bb/lots/keys",
        params={"flow_source_id": flow_source_id},
    )


def finacle_bb_post_lot_batch(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Push one clustering batch {flow_code, source_code, lots, merges, members}
    — applied atomically by the backend (409 = stale key map, re-run)."""
    return call_backend("POST", "/tasks/finacle-bb/lots/batch", json=payload)


# ---------------------------------------------------------------------------
# Payment statuses — the DAG resolves each movement's payments in std.Payment
# and pushes (entry identity, po_id, Status) rows; the operational view shows
# a per-movement aggregate.
# ---------------------------------------------------------------------------

def finacle_post_payment_status_batch(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Push one batch {flow_code, rows:[{external_ref, account, value_date,
    operation_date, po_id, status}]} — the backend resolves entry ids via the
    finacle source_hash."""
    return call_backend("POST", "/tasks/finacle/payment-status/batch", json=payload)


def finacle_get_payment_status_movements(
    flow_code: str, *, scope: str = "pending", skip: int = 0, limit: int = 2000
) -> Dict[str, Any]:
    """One page of the flow's app movements needing a status sync ('pending'
    = live PENDING entries, 'all' = first-launch full sync)."""
    return call_backend(
        "GET",
        "/tasks/finacle/payment-status/movements",
        params={"flow_code": flow_code, "scope": scope, "skip": skip, "limit": limit},
    )


# ---------------------------------------------------------------------------
# MT940 ingestion — the backend hands the raw file content to the DAG, which
# parses the :61:/+21 lines and resolves reco_id against the datamart, then
# pushes the entries back (like Finacle, but extraction is the 3_1_0 DAG logic).
# ---------------------------------------------------------------------------

def mt940_list_sources() -> Dict[str, Any]:
    """Active file sources flagged extract_via_dag (MT940) of active flows."""
    return call_backend("GET", "/tasks/mt940/sources")


def mt940_list_inbox(flow_code: str, source_code: str) -> Dict[str, Any]:
    """File names sitting in an MT940 source's inbox."""
    return call_backend(
        "GET",
        "/tasks/mt940/inbox",
        params={"flow_code": flow_code, "source_code": source_code},
    )


def mt940_open_run(flow_code: str, source_code: str, file_name: str) -> Dict[str, Any]:
    """Open a RUNNING run for one file; returns {"run_id": int, "content": str}."""
    result = call_backend(
        "POST",
        "/tasks/mt940/runs",
        json={"flow_code": flow_code, "source_code": source_code, "file_name": file_name},
    )
    return result["data"]


def mt940_post_batch(run_id: int, entries: list, errors: list) -> Dict[str, Any]:
    """Push one batch of formatted MT940 entries (JSON-safe dicts) to the backend."""
    return call_backend(
        "POST",
        f"/tasks/mt940/runs/{run_id}/batch",
        json={"entries": entries, "errors": errors},
    )


def mt940_complete_run(
    run_id: int, file_name: str, failed: bool = False, error: Optional[str] = None
) -> Dict[str, Any]:
    """Close the run and move the file to processed/error on the backend side."""
    return call_backend(
        "POST",
        f"/tasks/mt940/runs/{run_id}/complete",
        json={"file_name": file_name, "failed": failed, "error": error},
    )


def mt940_list_unresolved(flow_code: str, source_code: str) -> Dict[str, Any]:
    """remarks_1 keys of the source's live PENDING entries with unresolved
    reco_id — the DAG retries the std.Payment join on them each run."""
    return call_backend(
        "GET",
        "/tasks/mt940/unresolved",
        params={"flow_code": flow_code, "source_code": source_code},
    )


def mt940_post_resolved(flow_code: str, source_code: str, resolved: Dict[str, str]) -> Dict[str, Any]:
    """Push {remarks_1 key -> reco_id} pairs; the backend fills reco_id in place
    on the matching PENDING entries (no re-push, source_hash untouched)."""
    return call_backend(
        "POST",
        "/tasks/mt940/resolve",
        json={"flow_code": flow_code, "source_code": source_code, "resolved": resolved},
    )
