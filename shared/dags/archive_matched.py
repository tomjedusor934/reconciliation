"""Daily émargement sweep — moves remaining non-pending entries to the émargement table.

This is a safety net: the reconciliation service moves entries inline,
but if the process crashes between marking and moving, this DAG catches
up.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from reco_common import task_emargement

with DAG(
    dag_id="emargement_sweep",
    description="Move remaining matched/forced/excluded entries to the émargement table",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["reconciliation", "emargement"],
    default_args={
        "owner": "reconciliation",
        "retries": 1,
        "retry_delay": timedelta(minutes=10),
    },
) as dag:
    PythonOperator(task_id="sweep_to_emargement", python_callable=task_emargement)
