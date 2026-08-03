from typing import List

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.filters import IngestionRunFilter
from app.models.user import User
from app.schemas.reconciliation import IngestionRunResponse
from app.services.ingestion_service import ingestion_service

router = APIRouter()


@router.get("/", response_model=List[IngestionRunResponse])
def list_ingestion_runs(
    filters: IngestionRunFilter = Depends(),
    skip: int = 0,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return ingestion_service.list_runs(
        db, flow_id=filters.flow_id, status=filters.status, skip=skip, limit=limit
    )
