from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.filters import DataAuditFilter, UIAuditFilter
from app.models.user import User
from app.schemas.reconciliation import AuditLogResponse, UIActionLogResponse
from app.services.audit_service import audit_service

router = APIRouter()


@router.get("/data", response_model=List[AuditLogResponse])
def list_data_audit(
    filters: DataAuditFilter = Depends(),
    skip: int = 0,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return audit_service.list_data_audit(db, table_name=filters.table_name, skip=skip, limit=limit)


@router.get("/ui-actions", response_model=List[UIActionLogResponse])
def list_ui_audit(
    filters: UIAuditFilter = Depends(),
    skip: int = 0,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return audit_service.list_ui_actions(
        db, action=filters.action, user_id=filters.user_id, skip=skip, limit=limit
    )
