"""Orchestrator DAG — triggered externally to ingest all active flows then reconcile.

This DAG is designed to be triggered by an external service via the Airflow API:
    POST /api/v1/dags/orchestrate_ingestion/dagRuns  {"conf": {}}

It dynamically discovers all active flows and their source types, then runs
file ingestion for each, the Finacle datamart extraction (same logic as the
dedicated ingest_finacle DAG), and finally auto-reconciliation.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.python import PythonOperator

from reco_common import (
    task_ingest,
    task_list_active_flows,
    task_reconcile,
    task_emargement,
)
from reco_datamart import run_finacle_ingestion
from reco_datamart_bb import run_finacle_bb_ingestion
from reco_payment_status import run_payment_status_sync
from mt940_extract import run_mt940_ingestion

default_args = {
    "owner": "reconciliation",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=1),
}

dag = DAG(
    dag_id="reconciliation",
    description="Orchestrator: ingest all active flows then reconcile (external trigger)",
    schedule=None,
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    is_paused_upon_creation=False,
    tags=["reconciliation", "orchestrator", "ingest"],
    default_args=default_args,
)


def _ingest_mt940(**context):
    """MT940 extraction for all active extract_via_dag sources: the backend hands
    the raw file content, the DAG parses + resolves reco_id against the datamart,
    then pushes entries back. Skips gracefully if no ODBC driver / no MT940 source.
    """
    print(f"[orchestrate_ingestion] Starting MT940 ingestion for run_id {context['run_id']}")
    try:
        return run_mt940_ingestion(dag_run_id=context["run_id"])
    except Exception as exc:
        exc_str = str(exc)
        if "No supported MSSQL ODBC driver" in exc_str or "file not found" in exc_str:
            print(f"[orchestrate_ingestion] MT940 step skipped: {exc_str}")
            return {"skipped": True, "reason": "odbc_driver_not_found"}
        raise


def _ingest_all_flows(**context):
    """Discover active flows and ingest each one's file sources."""
    result = task_list_active_flows()
    flows = result.get("flows", [])
    summary = {"file_ingested": [], "errors": []}

    for flow_info in flows:
        code = flow_info["flow_code"]
        try:
            if flow_info["has_file_sources"]:
                result = task_ingest(flow_code=code)
                print(f"[orchestrate_ingestion] Ingested flow {code}: {result}")
                summary["file_ingested"].append(code)
        except Exception as exc:
            summary["errors"].append({"flow": code, "type": "file", "error": str(exc)})

    context["ti"].xcom_push(key="ingestion_summary", value=summary)
    print(f"Ingestion complete: {len(summary['file_ingested'])} file, "
          f"{len(summary['errors'])} errors")
    if summary["errors"]:
        print(f"Errors: {summary['errors']}")


def _ingest_finacle(**context):
    """Datamart extraction for all active finacle sources (shared with ingest_finacle).

    Possible overlap with a scheduled ingest_finacle run is absorbed by the
    backend's source_hash dedup.

    Skips gracefully if no MSSQL ODBC driver is available or no Finacle sources
    are configured — the dedicated ingest_finacle DAG should be used in that case.
    """
    try:
        return run_finacle_ingestion(dag_run_id=context["run_id"])
    except Exception as exc:
        exc_str = str(exc)
        if "No supported MSSQL ODBC driver" in exc_str or "file not found" in exc_str:
            print(
                f"[orchestrate_ingestion] Finacle step skipped: {exc_str}"
            )
            return {"skipped": True, "reason": "odbc_driver_not_found"}
        raise


def _ingest_finacle_bb(**context):
    """Datamart extraction for BATCH BOOKING TRUE finacle sources ((PACS008 ×
    MSGID) bucketing — shared with the dedicated ingest_finacle_bb DAG).

    Skips gracefully if no MSSQL ODBC driver is available; a missing BB source
    is already a clean no-op inside run_finacle_bb_ingestion.
    """
    try:
        return run_finacle_bb_ingestion(dag_run_id=context["run_id"])
    except Exception as exc:
        exc_str = str(exc)
        if "No supported MSSQL ODBC driver" in exc_str or "file not found" in exc_str:
            print(f"[orchestrate_ingestion] Finacle BB step skipped: {exc_str}")
            return {"skipped": True, "reason": "odbc_driver_not_found"}
        raise


def _sync_payment_status(**context):
    """std.Payment status sync for every finacle movement (payment-status
    column of the operational view). Runs AFTER the finacle ingests so the
    backend can attach rows to ingested entries.

    Skips gracefully if no MSSQL ODBC driver is available.
    """
    try:
        return run_payment_status_sync(dag_run_id=context["run_id"])
    except Exception as exc:
        exc_str = str(exc)
        if "No supported MSSQL ODBC driver" in exc_str or "file not found" in exc_str:
            print(f"[orchestrate_ingestion] Payment status step skipped: {exc_str}")
            return {"skipped": True, "reason": "odbc_driver_not_found"}
        raise


with dag:
    ingest_all = PythonOperator(
        task_id="ingest_all_active_flows",
        python_callable=_ingest_all_flows,
    )

    ingest_finacle = PythonOperator(
        task_id="ingest_finacle_sources",
        python_callable=_ingest_finacle,
    )

    ingest_finacle_bb = PythonOperator(
        task_id="ingest_finacle_bb",
        python_callable=_ingest_finacle_bb,
    )

    ingest_mt940 = PythonOperator(
        task_id="ingest_mt940_files",
        python_callable=_ingest_mt940,
    )

    sync_payment_status = PythonOperator(
        task_id="sync_payment_status",
        python_callable=_sync_payment_status,
    )

    auto_reconcile = PythonOperator(
        task_id="auto_reconcile",
        python_callable=task_reconcile,
        # Retrying a reconcile is never useful: either the run succeeded server
        # side, or it is still going (the retry gets skipped by the advisory
        # lock), or it genuinely failed and the retry fails the same way. The
        # retries=1 in default_args is there for ingestion (network/ODBC), not
        # for a non-idempotent batch — it is what started the 2nd concurrent
        # run_auto behind the 2026-07-16 deadlock. Red on a real failure is the
        # signal we want.
        retries=0,
        # Backstop: let Airflow kill the task rather than let it hang, should the
        # HTTP timeout never return. Above RECONCILE_TIMEOUT (3600 s).
        execution_timeout=timedelta(minutes=70),
    )

    sweep_emargement = PythonOperator(
        task_id="sweep_emargement",
        python_callable=task_emargement,
        # Keeps its retry: the sweep is idempotent, and a retry landing on a held
        # lock is skipped cleanly.
        execution_timeout=timedelta(minutes=70),
    )

    # Finacle datamart extraction first (so MT940 reco_id resolution / the backend
    # pre-match sweep can lean on ingested finacle entries), then the batch-booking
    # lot clustering, then MT940 file extraction, then the remaining file flows,
    # then the payment-status sync (needs the finacle entries ingested), then
    # reconcile + sweep.
    ingest_finacle >> ingest_finacle_bb >> ingest_mt940 >> ingest_all >> sync_payment_status >> auto_reconcile >> sweep_emargement
