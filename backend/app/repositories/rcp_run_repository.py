"""Persistence of RCP reattribution runs (see app/models/rcp_run.py).

Writes happen from the job's worker THREAD, so every call opens and closes its
own short-lived session: the batch's own session may be mid-transaction, or
already broken by the exception we are recording.
"""
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.rcp_run import KIND_ANALYZE, RcpRun

# The one definition of "can be committed" — a new status must not silently
# stop being counted here.
from app.services.rcp_link_service import ACTIONABLE

logger = logging.getLogger(__name__)


def summarize(kind: str, result: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Counters worth showing in a list without opening the payload.

    An analysis counts what it looked at and what it can act on; a commit counts
    what it actually wrote.
    """
    counters = {"movements": 0, "actionable": 0, "applied": 0, "failed": 0}
    if not result:
        return counters
    if kind == KIND_ANALYZE:
        proposals = result.get("proposals") or []
        counters["movements"] = len(proposals)
        counters["actionable"] = sum(
            1 for p in proposals if p.get("status") in ACTIONABLE
        )
    else:
        counters["movements"] = len(result.get("results") or [])
        counters["applied"] = int(result.get("applied") or 0)
        counters["failed"] = int(result.get("failed") or 0)
    return counters


class RcpRunRepository:
    # ---- writes (from the worker thread) --------------------------------
    def open_run(
        self, *, run_id: str, kind: str, status: str, user_id: Optional[int],
        label: str, started_at: datetime,
    ) -> None:
        self._write(
            lambda db: db.add(
                RcpRun(
                    id=run_id, kind=kind, status=status, phase="", user_id=user_id,
                    label=label[:512], started_at=started_at,
                )
            )
        )

    def close_run(
        self, *, run_id: str, status: str, phase: str, error: str,
        result: Optional[Dict[str, Any]], finished_at: datetime,
    ) -> None:
        def apply(db: Session) -> None:
            row = db.query(RcpRun).filter(RcpRun.id == run_id).one_or_none()
            if row is None:
                return
            row.status = status
            row.phase = (phase or "")[:128]
            row.error = error or ""
            row.result = result
            row.finished_at = finished_at
            for key, value in summarize(row.kind, result).items():
                setattr(row, key, value)

        self._write(apply)

    @staticmethod
    def _write(apply) -> None:
        """Persisting a run must never take the run down with it: a failure here
        is logged and swallowed — the operator still has the live result."""
        db = SessionLocal()
        try:
            apply(db)
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("[rcp-run] persistence failed")
        finally:
            db.close()

    # ---- reads (from the request) ---------------------------------------
    def get(self, db: Session, run_id: str) -> Optional[RcpRun]:
        return db.query(RcpRun).filter(RcpRun.id == run_id).one_or_none()

    def list_recent(self, db: Session, *, limit: int = 20) -> List[RcpRun]:
        """History list — never loads ``result``: one row is 2.6 MB of JSON."""
        return (
            db.query(
                RcpRun.id, RcpRun.kind, RcpRun.status, RcpRun.phase, RcpRun.user_id,
                RcpRun.label, RcpRun.movements, RcpRun.actionable, RcpRun.applied,
                RcpRun.failed, RcpRun.error, RcpRun.started_at, RcpRun.finished_at,
            )
            .order_by(RcpRun.started_at.desc())
            .limit(limit)
            .all()
        )

    @staticmethod
    def to_public(row: Any, *, with_result: bool = False) -> Dict[str, Any]:
        payload = {
            "job_id": row.id,
            "kind": row.kind,
            "status": row.status,
            "phase": row.phase or "",
            "done": 0,
            "total": 0,
            "error": row.error or "",
            "label": row.label or "",
            "movements": row.movements or 0,
            "actionable": row.actionable or 0,
            "applied": row.applied or 0,
            "failed": row.failed or 0,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            # A persisted run has no live commentary — logs are not stored.
            "logs": [],
            "result": None,
        }
        if with_result:
            payload["result"] = row.result
        return payload

    def prune(self, *, keep: int = 50) -> None:
        """Keep the history bounded; the payload is what costs."""
        def apply(db: Session) -> None:
            keep_ids = [
                r[0]
                for r in db.query(RcpRun.id)
                .order_by(RcpRun.started_at.desc())
                .limit(keep)
                .all()
            ]
            if keep_ids:
                db.query(RcpRun).filter(~RcpRun.id.in_(keep_ids)).delete(
                    synchronize_session=False
                )

        self._write(apply)


rcp_run_repository = RcpRunRepository()
