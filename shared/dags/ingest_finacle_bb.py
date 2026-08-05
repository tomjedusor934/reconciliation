"""Dedicated Finacle BATCH BOOKING TRUE ingestion DAG.

Extracts movements from the datamart (std.Movement, MSSQL) for every active
finacle_db source whose parser_type is ``finacle_batch_booking_true``, buckets
them by (PACS008 × MSGID) against std.Payment and splits the ones spanning
several buckets into ghost movements (see reco_datamart_bb), pushes the entries
with ``reco_id = bucket uuid`` through the regular finacle run lifecycle, then
persists the split parents and the buckets/members/keys backend-side.

Paused at creation: unpause once the `datamart` Airflow connection is set and
at least one batch-booking source is active. The orchestrator DAG
(``reconciliation``) runs the same logic as one of its steps.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator
from reco_datamart_bb import run_finacle_bb_ingestion

default_args = {
    "owner": "reconciliation",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
}

dag = DAG(
    dag_id="ingest_finacle_bb",
    description="Extract batch-booking Finacle movements, cluster them into lots, push to backend",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["reconciliation", "ingest", "finacle", "batch-booking"],
    default_args=default_args,
)


def _run(**context):
    return run_finacle_bb_ingestion(dag_run_id=context["run_id"])


with dag:
    PythonOperator(
        task_id="ingest_finacle_bb_sources",
        python_callable=_run,
    )
