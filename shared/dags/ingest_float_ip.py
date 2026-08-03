"""Float IP — Finacle internals only. Defined but inactive at go-live (per spec)."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from airflow import DAG  # noqa: F401 — required for Airflow DAG discovery
from dag_factory import build_ingestion_dag

dag = build_ingestion_dag(
    flow_code="float_ip",
    schedule=None,
    is_paused=True,
    description="Float IP — Finacle internals only (inactive at go-live)",
)
