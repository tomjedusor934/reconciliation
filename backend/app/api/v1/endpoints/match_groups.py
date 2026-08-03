from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.models.user import User
from app.schemas.reconciliation import (
    ForceMatchRequest,
    MatchGroupResponse,
)
from app.services.audit_service import audit_service
from app.services.reconciliation_service import reconciliation_service

router = APIRouter()


@router.get("/", response_model=List[MatchGroupResponse])
def list_match_groups(
    flow_id: Optional[int] = None,
    skip: int = 0,
    limit: int = Query(default=100, le=1000),
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    return reconciliation_service.list_match_groups(db, flow_id=flow_id, skip=skip, limit=limit)


@router.post("/force", response_model=MatchGroupResponse, status_code=status.HTTP_201_CREATED)
def force_match(
    payload: ForceMatchRequest,
    db: Session = Depends(deps.get_db),
    user: User = Depends(deps.get_current_active_user),
):
    try:
        mg = reconciliation_service.force_match(
            db, entry_ids=payload.entry_ids, comment=payload.comment, user_id=user.id
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    audit_service.log_ui_action(
        db,
        user_id=user.id,
        action="match_group.force",
        target_type="match_group",
        target_id=str(mg.id),
        details={"entry_ids": payload.entry_ids, "comment": payload.comment},
    )
    return mg
