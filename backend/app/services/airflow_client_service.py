"""Airflow client — minimal wrapper around the Airflow REST API.

Supports both Airflow 2 (Basic Auth) and Airflow 3 (JWT via /auth/token).
Configurable via env vars (see core/config.py): we can transparently switch from
the local Airflow (started in this docker-compose) to a corporate one by
setting AIRFLOW_USE_EXTERNAL=true and pointing AIRFLOW_API_URL to the remote.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from app.core.config import settings


class AirflowClientError(Exception):
    """Raised on Airflow API failures."""


class AirflowClientService:
    def __init__(self) -> None:
        self.base_url = settings.AIRFLOW_API_URL.rstrip("/")
        self._user = settings.AIRFLOW_API_USER
        self._password = settings.AIRFLOW_API_PASSWORD
        # Derive server root (scheme + host + port) for the /auth/token endpoint
        parsed = urlparse(self.base_url)
        self._server_root = f"{parsed.scheme}://{parsed.netloc}"
        # JWT cache
        self._jwt_token: Optional[str] = None
        self._jwt_expires_at: float = 0.0

    def _get_jwt_token(self) -> str:
        """Obtain (or return cached) JWT token from /auth/token."""
        if self._jwt_token and time.time() < self._jwt_expires_at - 60:
            return self._jwt_token
        url = f"{self._server_root}/auth/token"
        try:
            resp = httpx.post(
                url,
                json={"username": self._user, "password": self._password},
                timeout=10.0,
            )
        except httpx.HTTPError as exc:
            raise AirflowClientError(f"Airflow auth request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise AirflowClientError(f"Airflow auth {resp.status_code}: {resp.text}")
        data = resp.json()
        self._jwt_token = data["access_token"]
        # Airflow JWTs expire after ~24h by default; cache for 23h
        self._jwt_expires_at = time.time() + 23 * 3600
        return self._jwt_token

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self._get_jwt_token()}"}

    def _request(self, method: str, path: str, **kw) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = httpx.request(
                method, url, headers=self._auth_headers(), timeout=15.0, **kw
            )
        except httpx.HTTPError as exc:
            raise AirflowClientError(f"Airflow request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise AirflowClientError(f"Airflow {resp.status_code}: {resp.text}")
        return resp.json() if resp.content else {}

    def trigger_dag(self, dag_id: str, conf: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Trigger a DAG run. Airflow 3 requires logical_date in the body."""
        logical_date = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._request(
            "POST",
            f"/dags/{dag_id}/dagRuns",
            json={"conf": conf or {}, "logical_date": logical_date},
        )

    def get_dag_runs(self, dag_id: str, *, limit: int = 25) -> Dict[str, Any]:
        return self._request("GET", f"/dags/{dag_id}/dagRuns?limit={limit}")

    def health(self) -> Dict[str, Any]:
        return self._request("GET", "/monitor/health")


airflow_client_service = AirflowClientService()
