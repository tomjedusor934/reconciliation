"""Sync reconciliation DAGs into external Airflow on startup.

When AIRFLOW_USE_EXTERNAL=true, this service:
  1. Verifies connectivity to the Airflow API.
  2. Waits until the dag-processor discovers our DAGs.
  3. Ensures the orchestrator DAG is unpaused (it is the only scheduled entry point).

Our DAG files live in shared/dags/ which is mounted into the Airflow container
via a host-volume bind.  Airflow's dag-processor scans the dags_folder and picks
them up automatically — no file copy is needed.
"""
from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# DAGs we expect Airflow to discover (must match dag_id values in shared/dags/)
EXPECTED_DAGS = [
    "orchestrate_ingestion",
    "ingest_atm",
    "ingest_ip",
    "ingest_float_ip",
    "ingest_webripost",
    "ingest_other_payments",
    "ingest_finacle_bb",
    "ingest_wero",
    "reconcile_daily",
    "emargement_sweep",
]

# Only the orchestrator should be unpaused
DAGS_TO_UNPAUSE = ["orchestrate_ingestion"]


class AirflowSyncService:
    """One-shot service that runs at backend startup."""

    def __init__(self) -> None:
        self.base_url = settings.AIRFLOW_API_URL.rstrip("/")
        self.user = settings.AIRFLOW_API_USER
        self.password = settings.AIRFLOW_API_PASSWORD
        self._token: Optional[str] = None

    # ------------------------------------------------------------------
    # Auth (Airflow 3.x JWT)
    # ------------------------------------------------------------------
    def _get_token(self) -> str:
        """Obtain a JWT access token from Airflow 3.x."""
        if self._token:
            return self._token
        auth_url = self.base_url.rsplit("/api/", 1)[0] + "/auth/token"
        resp = httpx.post(
            auth_url,
            json={"username": self.user, "password": self.password},
            timeout=10.0,
        )
        resp.raise_for_status()
        self._token = resp.json()["access_token"]
        return self._token

    def _headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------
    def _get(self, path: str) -> dict:
        url = f"{self.base_url}{path}"
        resp = httpx.get(url, headers=self._headers(), timeout=15.0)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def _patch(self, path: str, json_body: dict) -> dict:
        url = f"{self.base_url}{path}"
        resp = httpx.patch(url, headers=self._headers(), json=json_body, timeout=15.0)
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def sync(self, max_wait: int = 120, poll_interval: int = 10) -> None:
        """Run the full sync sequence.  Called once at startup."""
        if not settings.AIRFLOW_USE_EXTERNAL:
            logger.info("AIRFLOW_USE_EXTERNAL=false — skipping DAG sync")
            return

        # Step 1: Verify connectivity
        if not self._wait_for_airflow(max_wait=30):
            logger.warning("Airflow not reachable — DAG sync skipped (will retry on next restart)")
            return

        logger.info("Airflow connection OK — syncing DAGs")

        # Step 2: Wait for our DAGs to appear
        missing = self._wait_for_dags(max_wait=max_wait, poll_interval=poll_interval)
        if missing:
            logger.warning("Some DAGs not yet discovered by Airflow: %s", missing)
        else:
            logger.info("All %d reconciliation DAGs discovered in Airflow", len(EXPECTED_DAGS))

        # Step 3: Unpause the orchestrator
        self._unpause_dags()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _wait_for_airflow(self, max_wait: int = 30) -> bool:
        """Block until Airflow API responds or max_wait seconds elapse."""
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            try:
                self._get_token()
                return True
            except Exception:
                logger.debug("Airflow not ready yet, retrying...")
                time.sleep(3)
        return False

    def _check_dag_exists(self, dag_id: str) -> bool:
        """Check if a single DAG exists via direct API call."""
        try:
            self._get(f"/dags/{dag_id}")
            return True
        except Exception:
            return False

    def _wait_for_dags(self, max_wait: int = 120, poll_interval: int = 10) -> List[str]:
        """Poll Airflow until all expected DAGs are discovered.

        Uses individual DAG lookups instead of the list endpoint because
        Airflow 3.x's ``/dags`` list only returns active DAGs (parsed by
        the dag-processor).  Our DAGs may be registered in the metadata DB
        but not yet active, and the ``tags`` query parameter is broken in
        Airflow 3.x API v2.
        """
        deadline = time.monotonic() + max_wait
        while time.monotonic() < deadline:
            try:
                found = [d for d in EXPECTED_DAGS if self._check_dag_exists(d)]
                missing = [d for d in EXPECTED_DAGS if d not in found]
                if not missing:
                    return []
                logger.info(
                    "Waiting for DAGs: %d/%d found (missing: %s)",
                    len(found), len(EXPECTED_DAGS), ", ".join(missing),
                )
            except Exception as exc:
                logger.debug("Error polling DAGs: %s", exc)
            time.sleep(poll_interval)

        # Return whatever is still missing
        return [d for d in EXPECTED_DAGS if not self._check_dag_exists(d)]

    def _unpause_dags(self) -> None:
        """Ensure the orchestrator DAG is unpaused."""
        for dag_id in DAGS_TO_UNPAUSE:
            try:
                self._patch(f"/dags/{dag_id}", {"is_paused": False})
                logger.info("Unpaused DAG: %s", dag_id)
            except Exception as exc:
                logger.warning("Failed to unpause %s: %s", dag_id, exc)


airflow_sync_service = AirflowSyncService()
