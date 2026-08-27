"""Public movement-split endpoints — a ghost walked back to its real movement."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.models.user import User
from app.schemas.split import SplitDetailResponse
from app.services.split_service import split_service

router = APIRouter()


@router.get("/{parent_source_hash}", response_model=SplitDetailResponse)
def get_split(
    parent_source_hash: str,
    db: Session = Depends(deps.get_db),
    _: User = Depends(deps.get_current_active_user),
):
    """The real movement behind a ghost, its claim group (every parent sharing
    the aggregate key), the group's ghosts, and whether the group adds up.

    Children are read from both the live and the émargement tables, so a split
    stays readable long after its ghosts have been matched.
    """
    detail = split_service.get_split(db, source_hash=parent_source_hash)
    if detail is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Split not found")
    return detail
