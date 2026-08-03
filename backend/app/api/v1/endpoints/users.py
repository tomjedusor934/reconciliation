from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.models.roles import AccessLevelEnum
from app.models.user import User
from app.schemas.user import UserCreate, UserResponse, UserUpdate
from app.services.settings_services import settings_service
from app.services.user_service import user_service

router = APIRouter()


@router.get("/me/permissions", response_model=Dict[str, str])
def read_user_permissions(
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    """
    Get all authorized routes for the current user based on their roles.
    Returns a dictionary mapping path -> access_level.
    """
    permissions = {}

    # Priority: ALL > EDIT > NONE
    priority = {
        AccessLevelEnum.ALL: 3,
        AccessLevelEnum.EDIT: 2,
        AccessLevelEnum.NONE: 0
    }

    for role in current_user.roles:
        for page in role.accessible_pages:
            path = page.path
            level = page.access_level

            # We explicitly include NONE because it implies Read-Only access (View but no Edit/Delete)

            if path not in permissions:
                permissions[path] = level
            else:
                # Upgrade if current level is higher than existing
                current_prio = priority.get(level, 0)
                existing_prio = priority.get(permissions[path], 0)

                if current_prio > existing_prio:
                    permissions[path] = level

    return permissions

@router.get("/", response_model=List[UserResponse])
def read_users(
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(deps.get_current_active_superuser),
) -> Any:
    users = user_service.get_users(db, skip=skip, limit=limit) # Need to implement get_users in service
    return users

@router.post("/", response_model=UserResponse)
def create_user(
    *,
    db: Session = Depends(deps.get_db),
    user_in: UserCreate,
    current_user: User = Depends(deps.get_current_active_superuser),
) -> Any:
    user = user_service.get_user_by_email(db, email=user_in.email)
    if user:
        raise HTTPException(
            status_code=400,
            detail="The user with this email already exists in the system",
        )

    validation_result = settings_service.validate_password(db, user_in.password)
    if not validation_result["is_valid"]:
        raise HTTPException(
            status_code=400,
            detail=f"Password does not meet requirements: {', '.join(validation_result['errors'])}",
        )

    user = user_service.create_user(db, user_in=user_in)
    return user

@router.get("/me", response_model=UserResponse)
def read_user_me(
    db: Session = Depends(deps.get_db),
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    return current_user

@router.put("/me", response_model=UserResponse)
def update_user_me(
    *,
    db: Session = Depends(deps.get_db),
    user_in: UserUpdate,
    current_user: User = Depends(deps.get_current_active_user),
) -> Any:
    try:
        user = user_service.update_user(db, db_user=current_user, user_in=user_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return user

@router.get("/{user_id}", response_model=UserResponse)
def read_user_by_id(
    user_id: int,
    current_user: User = Depends(deps.get_current_active_user),
    db: Session = Depends(deps.get_db),
) -> Any:
    user = user_service.get_user(db, user_id=user_id)
    if not user:
        raise HTTPException(
            status_code=404,
            detail="The user with this id does not exist in the system",
        )
    if user == current_user:
        return user
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="The user doesn't have enough privileges"
        )
    return user

@router.put("/{user_id}", response_model=UserResponse)
def update_user(
    *,
    db: Session = Depends(deps.get_db),
    user_id: int,
    user_in: UserUpdate,
    current_user: User = Depends(deps.get_current_active_superuser),
) -> Any:
    user = user_service.get_user(db, user_id=user_id)
    if not user:
        raise HTTPException(
            status_code=404,
            detail="The user with this id does not exist in the system",
        )

    try:
        user = user_service.update_user(db, db_user=user, user_in=user_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return user

@router.delete("/{user_id}", response_model=UserResponse)
def delete_user(
    *,
    db: Session = Depends(deps.get_db),
    user_id: int,
    current_user: User = Depends(deps.get_current_active_superuser),
) -> Any:
    user = user_service.get_user(db, user_id=user_id)
    if not user:
        raise HTTPException(
            status_code=404,
            detail="The user with this id does not exist in the system",
        )
    try:
        return user_service.delete_user(db, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{user_id}/roles/{role_id}", response_model=UserResponse)
def assign_role_to_user(
    *,
    db: Session = Depends(deps.get_db),
    user_id: int,
    role_id: int,
    current_user: User = Depends(deps.get_current_active_superuser),
) -> Any:
    user = user_service.add_role_to_user(db, user_id=user_id, role_id=role_id)
    if not user:
        raise HTTPException(status_code=404, detail="User or Role not found")
    return user


@router.post("/{user_id}/unblock", response_model=UserResponse)
def unblock_user(
    *,
    db: Session = Depends(deps.get_db),
    user_id: int,
    current_user: User = Depends(deps.get_current_active_superuser),
) -> Any:
    """Débloque un utilisateur bloqué (admin uniquement)."""
    try:
        return user_service.unblock_user(db, user_id=user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
