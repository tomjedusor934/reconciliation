from fastapi import APIRouter

from app.api.v1.endpoints import (
    admin,
    archive,
    audit,
    auth,
    dashboards,
    flows,
    ingestion_runs,
    lots,
    match_groups,
    rcp_reattribution,
    reconciliation_entries,
    reconciliation_runs,
    roles,
    settings,
    source_connections,
    splits,
    sso,
    tasks,
    tasks_lots,
    tasks_payment_status,
    users,
)

api_router = APIRouter()
api_router.include_router(auth.router, tags=["auth"], prefix="/auth")
api_router.include_router(admin.router, tags=["admin"], prefix="/admin")
api_router.include_router(users.router, tags=["users"], prefix="/users")
api_router.include_router(roles.router, tags=["roles"], prefix="/roles")
api_router.include_router(settings.router, tags=["settings"], prefix="/settings")
api_router.include_router(sso.router, tags=["sso"], prefix="/sso")

# Reconciliation
api_router.include_router(flows.router, tags=["reconciliation"], prefix="/flows")
api_router.include_router(
    reconciliation_entries.router,
    tags=["reconciliation"],
    prefix="/reconciliation-entries",
)
api_router.include_router(
    match_groups.router, tags=["reconciliation"], prefix="/match-groups"
)
api_router.include_router(lots.router, tags=["reconciliation"], prefix="/lots")
api_router.include_router(splits.router, tags=["reconciliation"], prefix="/splits")
api_router.include_router(
    ingestion_runs.router, tags=["reconciliation"], prefix="/ingestion-runs"
)
api_router.include_router(
    reconciliation_runs.router, tags=["reconciliation"], prefix="/reconciliation-runs"
)
api_router.include_router(
    dashboards.router, tags=["reconciliation"], prefix="/reconciliation-dashboard"
)
api_router.include_router(audit.router, tags=["audit"], prefix="/audit")
api_router.include_router(
    archive.router, tags=["archive"], prefix="/archive"
)
api_router.include_router(tasks.router, tags=["tasks"], prefix="/tasks")
api_router.include_router(tasks_lots.router, tags=["tasks"], prefix="/tasks")
api_router.include_router(tasks_payment_status.router, tags=["tasks"], prefix="/tasks")
api_router.include_router(
    source_connections.router, tags=["reconciliation"], prefix="/source-connections"
)
# Temporary operator tool — RCP return/reject reattribution (see
# app/services/rcp_link_service.py).
api_router.include_router(
    rcp_reattribution.router, tags=["reconciliation"], prefix="/rcp-reattribution"
)
