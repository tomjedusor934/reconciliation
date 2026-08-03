"""Archive endpoints — snapshot at a date + daily activity + Excel export."""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.filters import ActivityFilter, ExportFilter, SnapshotFilter
from app.models.user import User
from app.schemas.time_machine import DailyActivityResponse, SnapshotResponse
from app.services.time_machine_service import archive_service

router = APIRouter()


@router.get("/snapshot", response_model=SnapshotResponse)
def get_snapshot(
    filters: SnapshotFilter = Depends(),
    skip: int = 0,
    limit: int = Query(default=200, le=5000),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return archive_service.get_snapshot(
        db,
        snapshot_date=filters.snapshot_date,
        flow_id=filters.flow_id,
        status=filters.status,
        skip=skip,
        limit=limit,
    )


@router.get("/activity", response_model=DailyActivityResponse)
def get_daily_activity(
    filters: ActivityFilter = Depends(),
    skip: int = 0,
    limit: int = Query(default=200, le=5000),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return archive_service.get_daily_activity(
        db,
        activity_date=filters.activity_date,
        flow_id=filters.flow_id,
        user_id=filters.user_id,
        skip=skip,
        limit=limit,
    )


@router.get("/export")
def export_snapshot_excel(
    filters: ExportFilter = Depends(),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    output = archive_service.export_snapshot_excel(
        db,
        snapshot_date=filters.snapshot_date,
        flow_id=filters.flow_id,
    )
    filename = f"archive_snapshot_{filters.snapshot_date}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
