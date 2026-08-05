"""Public movement-lot endpoints (Finacle Batch Booking True lot visualization)."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.filters import LotListFilter
from app.models.user import User
from app.schemas.lot import (
    LotDetailResponse,
    LotGraphResponse,
    PaginatedLotKeyValuesResponse,
    PaginatedLotMembersResponse,
    PaginatedLotsResponse,
)
from app.services.lot_service import lot_service

router = APIRouter()


@router.get("/", response_model=PaginatedLotsResponse)
def list_lots(
    filters: LotListFilter = Depends(),
    skip: int = 0,
    limit: int = Query(default=50, le=500),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    items, total = lot_service.list_lots(
        db,
        flow_id=filters.flow_id,
        status=filters.status,
        balanced=filters.balanced,
        date_from=filters.date_from,
        date_to=filters.date_to,
        search=filters.search,
        bucket_kind=filters.bucket_kind,
        synthetic_only=filters.synthetic_only,
        payment_gap=filters.payment_gap,
        skip=skip,
        limit=limit,
    )
    return PaginatedLotsResponse(items=items, total_count=total)


@router.get("/{lot_id}", response_model=LotDetailResponse)
def get_lot(
    lot_id: str,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    detail = lot_service.get_lot_detail(db, lot_id=lot_id)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lot not found")
    return detail


@router.get("/{lot_id}/graph", response_model=LotGraphResponse)
def get_lot_graph(
    lot_id: str,
    key_type: Optional[str] = Query(default=None, pattern="^(PACS008|MSGID|PO)$"),
    key_value: Optional[str] = None,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    """Mega-aggregate graph for lots too big to render one node per movement.
    Pass key_type + key_value to scope the movement nodes to a single key value
    (the related-nodes view opened from the drawer)."""
    graph = lot_service.get_lot_graph(
        db, lot_id=lot_id, key_type=key_type, key_value=key_value
    )
    if graph is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lot not found")
    return graph


@router.get("/{lot_id}/keys", response_model=PaginatedLotKeyValuesResponse)
def list_lot_key_values(
    lot_id: str,
    key_type: str = Query(pattern="^(PACS008|MSGID|PO)$"),
    search: Optional[str] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=200),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    """Distinct values of a key type (paginated) — the drawer opened when a
    link node is clicked."""
    result = lot_service.list_lot_key_values(
        db, lot_id=lot_id, key_type=key_type, search=search, skip=skip, limit=limit
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lot not found")
    items, total = result
    return PaginatedLotKeyValuesResponse(items=items, total_count=total)


@router.get("/{lot_id}/members", response_model=PaginatedLotMembersResponse)
def list_lot_members(
    lot_id: str,
    movement_type: Optional[str] = None,
    direction: Optional[str] = Query(default=None, pattern="^(debit|credit)$"),
    entry_status: Optional[str] = Query(
        default=None, pattern="^(pending|matched|forced|excluded)$"
    ),
    search: Optional[str] = None,
    key_type: Optional[str] = Query(default=None, pattern="^(PACS008|MSGID|PO)$"),
    key_value: Optional[str] = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=100, le=200),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    result = lot_service.list_lot_members(
        db,
        lot_id=lot_id, skip=skip, limit=limit,
        movement_type=movement_type, direction=direction,
        entry_status=entry_status, search=search,
        key_type=key_type, key_value=key_value,
    )
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lot not found")
    items, total = result
    return PaginatedLotMembersResponse(items=items, total_count=total)
