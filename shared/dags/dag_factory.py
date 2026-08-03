"""Factory: build a file-ingestion DAG for a given flow.

Used by all flow-specific DAG files (ingest_atm.py, ingest_mt940.py, ...).
Each instantiation creates one DAG that polls the inbox of the flow every
X minutes. Finacle extraction has its own dedicated DAG (ingest_finacle.py).
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta
from typing import Optional

from airflow import DAG
from airflow.operators.python import PythonOperator

from reco_common import task_ingest


def build_ingestion_dag(
    *,
    flow_code: str,
    schedule: str = "*/15 * * * *",
    is_paused: bool = False,
    description: Optional[str] = None,
) -> DAG:
    default_args = {
        "owner": "reconciliation",
        "depends_on_past": False,
        "retries": 2,
        "retry_delay": timedelta(minutes=2),
    }
    dag = DAG(
        dag_id=f"ingest_{flow_code}",
        description=description or f"Ingest inbox files for flow '{flow_code}'",
        schedule=schedule,
        start_date=datetime(2025, 1, 1),
        catchup=False,
        max_active_runs=1,
        is_paused_upon_creation=is_paused,
        tags=["reconciliation", "ingest", flow_code],
        default_args=default_args,
    )

    with dag:
        PythonOperator(
            task_id="ingest_inbox_files",
            python_callable=task_ingest,
            op_kwargs={"flow_code": flow_code},
        )

    return dag
