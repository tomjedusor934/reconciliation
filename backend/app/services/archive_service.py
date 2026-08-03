"""Émargement service — moves remaining matched entries to the émargement table.

Typically used as a safety net via the daily DAG to catch any matched entries
that were not moved inline (e.g., due to a process crash between mark and move).
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.locks import LockKey, try_advisory_lock
from app.services.reconciliation_service import ReconciliationAlreadyRunning


class EmargementService:
    def sweep_matched(self, db: Session) -> int:
        """Move any matched/forced/excluded entries still sitting in the live
        table to the émargement table.  This is a catch-all safety net.

        Takes the same lock as run_auto: this DELETE has no indexable predicate,
        so it sweeps reconciliation_entry in physical order — a THIRD lock order
        over the table, on top of the two that already deadlock against each
        other. The `reconciliation` DAG chains auto_reconcile >> sweep_emargement,
        but the `emargement_sweep` DAG is independent and can land mid-run.
        Being skipped costs nothing: the sweep is idempotent and will come back.
        """
        with try_advisory_lock(LockKey.RECONCILIATION_ENTRY_WRITER) as acquired:
            if not acquired:
                raise ReconciliationAlreadyRunning(
                    "a reconciliation run is in progress — sweep skipped"
                )
            return self._sweep_matched_locked(db)

    def _sweep_matched_locked(self, db: Session) -> int:
        sql = text("""
            WITH moved AS (
                DELETE FROM reco.reconciliation_entry
                WHERE status IN ('MATCHED', 'FORCED', 'EXCLUDED')
                RETURNING *
            )
            INSERT INTO reco.reconciliation_entry_emargement (
                id, flow_id, ingestion_run_id, reco_id, account, currency, amount,
                direction, value_date, operation_date, event_type, external_ref,
                file_name, payload_raw, source_hash, status, match_group_id, matched_at,
                emarged_at, created_at, updated_at
            )
            SELECT id, flow_id, ingestion_run_id, reco_id, account, currency, amount,
                   direction, value_date, operation_date, event_type, external_ref,
                   file_name, payload_raw, source_hash, status, match_group_id, matched_at,
                   now(), created_at, updated_at
            FROM moved
            ON CONFLICT (source_hash) DO NOTHING
        """)
        result = db.execute(sql)
        moved = result.rowcount or 0
        db.commit()
        return moved


emargement_service = EmargementService()
