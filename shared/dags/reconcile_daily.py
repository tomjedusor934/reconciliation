"""Daily reconciliation engine — runs the auto matching."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from reco_common import task_reconcile

with DAG(
    dag_id="reconcile_daily",
    description="Run automatic reconciliation engine (group by flow+reco_id+currency, sum=0)",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["reconciliation", "engine"],
    default_args={
        "owner": "reconciliation",
        # A reconcile is never retried: the previous run may still be going
        # server side (sync endpoint, not cancellable) and the retry would just
        # be skipped by the advisory lock. Cf. auto_reconcile in
        # orchestrate_ingestion.
        "retries": 0,
        "execution_timeout": timedelta(minutes=70),
    },
) as dag:
    PythonOperator(task_id="run_auto_reconciliation", python_callable=task_reconcile)
