from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.filters import EntryListFilter
from app.models.user import User
from app.schemas.reconciliation import (
    ExclusionCreate,
    ExclusionResponse,
    PaginatedEntriesResponse,
    ReconciliationEntryResponse,
    UnexcludeRequest,
)
from app.services.audit_service import audit_service
from app.services.reconciliation_service import reconciliation_service

router = APIRouter()


@router.get("/", response_model=PaginatedEntriesResponse)
def list_entries(
    filters: EntryListFilter = Depends(),
    skip: int = 0,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    items, total_count = reconciliation_service.list_entries_filtered(
        db,
        ids=filters.ids,
        flow_id=filters.flow_id,
        status=filters.status,
        reco_id=filters.reco_id,
        amount_min=filters.amount_min,
        amount_max=filters.amount_max,
        payment_statuses=filters.payment_statuses,
        payment_timestamp_from=filters.payment_timestamp_from,
        payment_timestamp_to=filters.payment_timestamp_to,
        account=filters.account,
        date_from=filters.date_from,
        date_to=filters.date_to,
        search=filters.search,
        skip=skip,
        limit=limit,
    )
    return PaginatedEntriesResponse(items=items, total_count=total_count)


@router.get("/payment-status-options", response_model=List[str])
def payment_status_options(
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    """Distinct payment-status values ('?' = unknown) — populates the
    operational view's payment-status filter."""
    return reconciliation_service.distinct_payment_statuses(db)


@router.get("/export")
def export_entries_excel(
    filters: EntryListFilter = Depends(),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    """Export every entry matching the current filters as an .xlsx file.

    Same filters as the list endpoint, but fetches all matching rows (not just
    the paginated batches) — mirrors the archive snapshot export.
    """
    output = reconciliation_service.export_entries_excel(db, filters=filters)
    filename = f"operational_entries_{date.today()}.xlsx"
    return StreamingResponse(
        output,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/exclude", response_model=ExclusionResponse, status_code=status.HTTP_201_CREATED)
def exclude_entry(
    payload: ExclusionCreate,
    db: Session = Depends(deps.get_db),
    user: User = Depends(deps.get_current_active_user),
):
    try:
        excl = reconciliation_service.exclude(
            db,
            entry_id=payload.entry_id,
            reason=payload.reason,
            user_id=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    audit_service.log_ui_action(
        db,
        user_id=user.id,
        action="entry.exclude",
        target_type="reconciliation_entry",
        target_id=str(payload.entry_id),
        details={"reason": payload.reason},
    )
    return excl


@router.post("/unexclude", response_model=ExclusionResponse)
def unexclude_entry(
    payload: UnexcludeRequest,
    db: Session = Depends(deps.get_db),
    user: User = Depends(deps.get_current_active_user),
):
    try:
        excl = reconciliation_service.unexclude(
            db,
            entry_id=payload.entry_id,
            reason=payload.reason,
            user_id=user.id,
        )
    except ValueError as exc:
        #details =reconciliation_service.get_unexcluded_entries(db)  # check if entry exists
        #same but for exclusions
        details = reconciliation_service.get_unexcluded_entries(db)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(details))

    audit_service.log_ui_action(
        db,
        user_id=user.id,
        action="entry.unexclude",
        target_type="reconciliation_entry",
        target_id=str(payload.entry_id),
        details={"reason": payload.reason},
    )
    return excl
