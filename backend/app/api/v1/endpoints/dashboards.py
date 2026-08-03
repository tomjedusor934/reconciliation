from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.filters import IngestionCalendarFilter, TransversalFilter
from app.models.user import User
from app.schemas.reconciliation import (
    DashboardResponse,
    IngestionCalendarResponse,
    TransversalGroup,
)
from app.services.dashboard_service import dashboard_service

router = APIRouter()


@router.get("/", response_model=DashboardResponse)
def get_dashboard(
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return dashboard_service.get_dashboard(db)


@router.get("/ingestion-calendar", response_model=IngestionCalendarResponse)
def get_ingestion_calendar(
    filters: IngestionCalendarFilter = Depends(),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return dashboard_service.ingestion_calendar(
        db, flow_id=filters.flow_id, year=filters.year, month=filters.month
    )


@router.get("/transversal", response_model=List[TransversalGroup])
def get_transversal(
    filters: TransversalFilter = Depends(),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return dashboard_service.transversal(db, reco_id=filters.reco_id)
