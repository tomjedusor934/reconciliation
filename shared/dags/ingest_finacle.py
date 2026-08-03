"""Dedicated Finacle ingestion DAG.

Extracts movements from the datamart (std.Movement, MSSQL) for every active
finacle_db source — filtered on the source's reference accounts and the
backend-provided watermark — formats them to the reconciliation entry schema
and pushes them to the backend in batches. The backend persists the entries
(source_hash dedup) and logs the IngestionRun.

Paused at creation: unpause once the `datamart` Airflow connection is set and
at least one finacle source is active.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from reco_datamart import run_finacle_ingestion

default_args = {
    "owner": "reconciliation",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
}

dag = DAG(
    dag_id="ingest_finacle",
    description="Extract Finacle movements from the datamart and push to backend",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["reconciliation", "ingest", "finacle"],
    default_args=default_args,
)


def _run(**context):
    return run_finacle_ingestion(dag_run_id=context["run_id"])


with dag:
    PythonOperator(
        task_id="ingest_finacle_sources",
        python_callable=_run,
    )
