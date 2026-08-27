"""Dedicated WERO ingestion DAG.

Extracts the three legs of the WERO payment reconciliation from the datamart —
the WERO table, std.Payment (filtered on InitModule) and std.[Return] — for
every active finacle_db source whose parser_type is ``wero``, and pushes them
through the regular finacle run lifecycle with the end-to-end reference as
reco_id, WERO side credit and Finacle side debit, so the standard sum-to-zero
engine reconciles them (see reco_wero).

Unlike the other datamart DAGs this one never reads std.Movement: it is a
payment reconciliation, not an accounting one.

Paused at creation: unpause once the `datamart` Airflow connection is set, the
datamart identifiers in the source's parser_config are confirmed, and the WERO
source is active. The orchestrator DAG (``reconciliation``) runs the same logic
as one of its steps.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator
from reco_wero import run_wero_ingestion

default_args = {
    "owner": "reconciliation",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
}

dag = DAG(
    dag_id="ingest_wero",
    description="Extract WERO / std.Payment / std.[Return] legs and push to backend",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["reconciliation", "ingest", "wero", "payment"],
    default_args=default_args,
)


def _run(**context):
    return run_wero_ingestion(dag_run_id=context["run_id"])


with dag:
    PythonOperator(
        task_id="ingest_wero_sources",
        python_callable=_run,
    )
