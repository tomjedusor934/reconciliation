from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.models.user import User
from app.schemas.reconciliation import ReconciliationRunResponse
from app.services.reconciliation_service import reconciliation_service

router = APIRouter()


@router.get("/", response_model=List[ReconciliationRunResponse])
def list_reconciliation_runs(
    skip: int = 0,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return reconciliation_service.list_runs(db, skip=skip, limit=limit)
