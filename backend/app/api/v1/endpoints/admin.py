"""Admin-only maintenance endpoints.

Currently exposes a reset of all ingestion/reconciliation data — the targeted
equivalent of a ``docker compose down -v`` that keeps the flux configuration
(flow / flow_source / source_connection) and every user/role/SSO/audit row.
"""
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_active_superuser, get_db
from app.core.config import settings
from app.models.flow import Flow
from app.models.user import User
from app.services.audit_service import audit_service

router = APIRouter()

# Fully-qualified tables wiped by the reset. Order is irrelevant for a single
# multi-table TRUNCATE, but the set must be closed over child foreign keys — it
# is: nothing outside this set references these tables. Protected tables
# (flow*, source_connection, user/role/sso, settings, audit.*) are deliberately
# absent so they are never touched.
INGESTION_TABLES = [
    "reco.reconciliation_entry",
    "reco.reconciliation_entry_emargement",
    "reco.match_group",
    "reco.reconciliation_run",
    "reco.ingestion_run",
    "reco.exclusion",
]

_PROD_ENVS = {"prod", "production"}


def _reset_allowed() -> bool:
    """The reset is disabled in production environments."""
    return settings.ENVIRONMENT.lower() not in _PROD_ENVS


@router.get("/environment")
def get_environment(
    current_user: User = Depends(get_current_active_superuser),
) -> Dict[str, Any]:
    """Expose the runtime environment so the UI can hide destructive
    maintenance actions in production."""
    return {
        "environment": settings.ENVIRONMENT,
        "reset_ingestion_allowed": _reset_allowed(),
    }


@router.post("/reset-ingestion")
def reset_ingestion(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> Dict[str, Any]:
    """Truncate all ingestion/reconciliation data (entries, émargement archive,
    matches, runs, exclusions) while preserving flux config and users.

    Disabled in production. ``TRUNCATE ... RESTART IDENTITY`` is used on purpose:
    it bypasses the per-row audit triggers (``reconciliation_entry`` holds
    ~100k-200k rows/run) and resets identity sequences for a truly fresh state.
    The action itself is audited via a single ``ui_action_log`` entry.
    """
    if not _reset_allowed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Réinitialisation des données interdite en production",
        )

    try:
        # Row counts captured before truncation (audit trail + UI feedback).
        counts: Dict[str, int] = {}
        for table in INGESTION_TABLES:
            counts[table] = db.execute(text(f"SELECT count(*) FROM {table}")).scalar() or 0

        db.execute(text(f"TRUNCATE {', '.join(INGESTION_TABLES)} RESTART IDENTITY"))

        # log_ui_action commits the session → TRUNCATE + audit row are atomic.
        audit_service.log_ui_action(
            db,
            user_id=current_user.id,
            action="admin.reset_ingestion",
            target_type="system",
            details={"counts": counts, "environment": settings.ENVIRONMENT},
        )
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Échec de la réinitialisation: {exc}",
        ) from exc

    return {
        "status": "success",
        "counts": counts,
        "total_deleted": sum(counts.values()),
    }


# Scoped deletes for a single-flow reset, in foreign-key-safe order. Unlike the
# global reset we cannot TRUNCATE (that wipes every flow), so each table is
# filtered by flow_id. ``reconciliation_run`` is intentionally absent: it carries
# no flow_id (shared engine traces) and cannot be attributed to one flow.
_FLOW_RESET_STATEMENTS = [
    # Exclusions reference entries by a logical entry_id (no FK), so they must be
    # removed first via a subquery over both entry tables (before the entries go).
    (
        "reco.exclusion",
        "DELETE FROM reco.exclusion WHERE entry_id IN ("
        " SELECT id FROM reco.reconciliation_entry WHERE flow_id = :fid"
        " UNION SELECT id FROM reco.reconciliation_entry_emargement WHERE flow_id = :fid)",
    ),
    # Entries before ingestion_run (reconciliation_entry.ingestion_run_id is a FK).
    ("reco.reconciliation_entry", "DELETE FROM reco.reconciliation_entry WHERE flow_id = :fid"),
    ("reco.reconciliation_entry_emargement", "DELETE FROM reco.reconciliation_entry_emargement WHERE flow_id = :fid"),
    ("reco.match_group", "DELETE FROM reco.match_group WHERE flow_id = :fid"),
    ("reco.ingestion_run", "DELETE FROM reco.ingestion_run WHERE flow_id = :fid"),
]


@router.post("/reset-ingestion/flow")
def reset_ingestion_flow(
    flow_id: int = Body(..., embed=True),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
) -> Dict[str, Any]:
    """Reset ingestion/reconciliation data for a SINGLE flow, leaving every other
    flow (and the shared reconciliation_run traces) intact.

    The targeted equivalent of :func:`reset_ingestion`. Disabled in production.
    Uses scoped ``DELETE``s (not ``TRUNCATE``) so only the selected flow is wiped;
    the high-volume entry tables aren't audited, so the deletes don't bloat the
    audit log, and the action is recorded as a single ``ui_action_log`` entry.
    """
    if not _reset_allowed():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Réinitialisation des données interdite en production",
        )

    flow = db.query(Flow).filter(Flow.id == flow_id).first()
    if flow is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Flux introuvable (id={flow_id})",
        )

    try:
        counts: Dict[str, int] = {}
        for table, stmt in _FLOW_RESET_STATEMENTS:
            result = db.execute(text(stmt), {"fid": flow_id})
            counts[table] = result.rowcount or 0

        # log_ui_action commits the session → deletes + audit row are atomic.
        audit_service.log_ui_action(
            db,
            user_id=current_user.id,
            action="admin.reset_ingestion_flow",
            target_type="flow",
            target_id=str(flow_id),
            details={"flow_code": flow.code, "counts": counts, "environment": settings.ENVIRONMENT},
        )
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Échec de la réinitialisation: {exc}",
        ) from exc

    return {
        "status": "success",
        "flow_id": flow_id,
        "counts": counts,
        "total_deleted": sum(counts.values()),
    }
