"""Internal payment-status endpoints called by the sync_payment_status DAG task.

Auth via X-Internal-Token like the other /tasks endpoints; separate module so
tasks.py doesn't keep growing (same convention as tasks_lots.py).
"""
from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.api.v1.endpoints.tasks import TaskResponse
from app.services.flow_service import flow_service
from app.services.payment_status_service import payment_status_service

router = APIRouter()


class PaymentStatusRowIn(BaseModel):
    # reconciliation group the payment belongs to (lot uuid for batch bookings);
    # the DAG dedupes (reco_id, po_id) before pushing.
    reco_id: str = Field(min_length=1, max_length=128)
    po_id: str = Field(min_length=1, max_length=64)
    status: Optional[str] = Field(default=None, max_length=32)
    # the payment's amount (std.Payment); None when the DAG could not resolve it.
    amount: Optional[Decimal] = None
    # the payment's creation timestamp (std.Payment.CreatedOn); same
    # nullability. Naive by contract — the DAG strips any offset so the stored
    # wall clock is the one the operational hour filter compares against.
    payment_timestamp: Optional[datetime] = None


class PaymentStatusBatchIn(BaseModel):
    flow_code: str
    rows: List[PaymentStatusRowIn] = Field(default_factory=list)


class PaymentStatusMovementOut(BaseModel):
    reco_id: Optional[str] = None  # reconciliation group / lot uuid (payment-status key)
    external_ref: str
    account: Optional[str] = None
    value_date: datetime
    operation_date: Optional[datetime] = None
    transaction_particulars: Optional[str] = None
    ref_no: Optional[str] = None
    remarks_1: Optional[str] = None
    initiating_channel: Optional[str] = None  # from payload_raw (MOSEL/Webripost Z6/Z9)


class PaymentStatusMovementsResponse(BaseModel):
    ok: bool
    movements: List[PaymentStatusMovementOut] = Field(default_factory=list)


def _get_flow(db: Session, flow_code: str):
    flow = flow_service.get_flow_by_code(db, code=flow_code)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    return flow


@router.post("/finacle/payment-status/batch", response_model=TaskResponse)
def push_payment_status_batch(
    payload: PaymentStatusBatchIn,
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """Upsert (entry, payment) statuses for one batch of movements."""
    flow = _get_flow(db, payload.flow_code)
    result = payment_status_service.apply_status_batch(db, flow=flow, rows=payload.rows)
    return TaskResponse(ok=True, data=result)


@router.get("/finacle/payment-status/movements", response_model=PaymentStatusMovementsResponse)
def list_payment_status_movements(
    flow_code: str,
    scope: str = Query(default="pending", pattern="^(pending|all)$"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=1000, le=2000),
    db: Session = Depends(deps.get_db),
    _: None = Depends(deps.verify_internal_token),
):
    """The flow's movements needing a payment-status sync: PENDING entries by
    default (statuses keep moving until reconciliation), everything (live +
    émargement) for the first-launch full sync. No datamart-window dependency:
    a payment pending for months stays covered."""
    flow = _get_flow(db, flow_code)
    movements = payment_status_service.list_movements(
        db, flow=flow, scope=scope, skip=skip, limit=limit
    )
    return PaymentStatusMovementsResponse(ok=True, movements=movements)
