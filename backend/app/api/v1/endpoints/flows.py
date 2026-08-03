from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.models.user import User
from app.repositories.flow_repository import SourceHasMovementLots
from app.schemas.flow import FlowCreate, FlowResponse, FlowSourceResponse, FlowSourceUpdate, FlowUpdate
from app.services.flow_service import flow_service

router = APIRouter()


@router.get("/", response_model=List[FlowResponse])
def list_flows(
    only_active: bool = False,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return flow_service.list_flows(db, only_active=only_active)


@router.get("/{flow_id}", response_model=FlowResponse)
def get_flow(
    flow_id: int,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    flow = flow_service.get_flow(db, flow_id=flow_id)
    if not flow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Flow not found")
    return flow


@router.post("/", response_model=FlowResponse, status_code=status.HTTP_201_CREATED)
def create_flow(
    payload: FlowCreate,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    try:
        return flow_service.create_flow(db, payload=payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.put("/{flow_id}", response_model=FlowResponse)
def update_flow(
    flow_id: int,
    payload: FlowUpdate,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    try:
        return flow_service.update_flow(db, flow_id=flow_id, payload=payload)
    except SourceHasMovementLots as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


@router.delete("/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow(
    flow_id: int,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    try:
        flow_service.delete_flow(db, flow_id=flow_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))


# ---------- FlowSource sub-resource ----------

class ToggleBody(BaseModel):
    is_active: bool


@router.patch("/{flow_id}/sources/{source_id}/toggle", response_model=FlowSourceResponse)
def toggle_source(
    flow_id: int,
    source_id: int,
    body: ToggleBody,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_superuser),
):
    try:
        source = flow_service.toggle_source(db, source_id=source_id, is_active=body.is_active)
        if source.flow_id != flow_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Source not found for this flow")
        return source
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
