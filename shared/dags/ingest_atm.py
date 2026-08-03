"""ATM ingestion DAG (priority flow). Polls inbox every 15 minutes."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from airflow import DAG
from airflow.operators.python import PythonOperator
from reco_common import task_ingest, task_reconcile

default_args = {
    "owner": "reconciliation",
    "depends_on_past": False,
    "retries": 2,
    "retry_delay": timedelta(minutes=2),
}

dag = DAG(
    dag_id="ingest_atm",
    description="ATM Cobol/MOSEL files ingestion (priority flow)",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=True,
    tags=["reconciliation", "ingest", "atm"],
    default_args=default_args,
)

with dag:
    ingest_files = PythonOperator(
        task_id="ingest_inbox_files",
        python_callable=task_ingest,
        op_kwargs={"flow_code": "atm"},
    )

    auto_reconcile = PythonOperator(
        task_id="auto_reconcile",
        python_callable=task_reconcile,
        # The retries=2 in default_args is for ingest_files, not for a
        # non-idempotent batch — cf. orchestrate_ingestion.auto_reconcile.
        retries=0,
        execution_timeout=timedelta(minutes=70),
    )

    ingest_files >> auto_reconcile
