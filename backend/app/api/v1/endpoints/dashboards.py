from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.filters import DeepSearchFilter, IngestionCalendarFilter
from app.models.user import User
from app.schemas.reconciliation import (
    DashboardResponse,
    DeepSearchResponse,
    IngestionCalendarResponse,
)
from app.services.dashboard_service import dashboard_service
from app.services.search_service import search_service

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


@router.get("/search", response_model=DeepSearchResponse)
def deep_search(
    filters: DeepSearchFilter = Depends(),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    """Resolve any identifier to everything attached to it — lots, movements
    (live AND émargé) and payments. Replaces the former /transversal, which only
    matched reco_id and only read the live table."""
    return search_service.search(db, query=filters.q, broad=filters.broad)
