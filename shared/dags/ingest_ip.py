"""IP (instant payments) — MT940 ingestion. Paused at creation (parser TODO)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airflow import DAG  # noqa: F401 — required for Airflow DAG discovery
from dag_factory import build_ingestion_dag

dag = build_ingestion_dag(
    flow_code="ip",
    schedule=None,
    is_paused=True,
    description="MT940 IP ingestion (Finacle extract: see ingest_finacle DAG)",
)
