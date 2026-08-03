"""Webripost — XLSX ingestion, paused at creation."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airflow import DAG  # noqa: F401 — required for Airflow DAG discovery
from dag_factory import build_ingestion_dag

dag = build_ingestion_dag(
    flow_code="webripost",
    schedule=None,
    is_paused=True,
    description="Webripost CSV ingestion (Finacle extract: see ingest_finacle DAG)",
)
