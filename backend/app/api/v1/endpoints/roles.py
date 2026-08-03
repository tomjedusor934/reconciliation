from typing import Any, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.schemas.roles import RoleCreate, RoleResponse, RoleUpdate
from app.services.roles_services import role_service

router = APIRouter()

@router.post("/", response_model=RoleResponse)
def create_role(
    *,
    db: Session = Depends(deps.get_db),
    role_in: RoleCreate,
    current_user = Depends(deps.get_current_active_superuser),
) -> Any:
    """
    Create new role.
    """
    try:
        role = role_service.create_role(db, role_in=role_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return role

@router.get("/", response_model=List[RoleResponse])
def read_roles(
    db: Session = Depends(deps.get_db),
    skip: int = 0,
    limit: int = 100,
    current_user = Depends(deps.get_current_active_user),
) -> Any:
    """
    Retrieve roles.
    """
    return role_service.get_roles(db, skip=skip, limit=limit)


@router.get("/{role_id}", response_model=RoleResponse)
def read_role(
    role_id: int,
    db: Session = Depends(deps.get_db),
    current_user = Depends(deps.get_current_active_user),
) -> Any:
    """
    Get role by ID.
    """
    role = role_service.get_role(db, role_id=role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role

@router.put("/{role_id}", response_model=RoleResponse)
def update_role(
    *,
    db: Session = Depends(deps.get_db),
    role_id: int,
    role_in: RoleUpdate,
    current_user = Depends(deps.get_current_active_superuser),
) -> Any:
    """
    Update a role.
    """
    try:
        role = role_service.update_role(db, role_id=role_id, role_in=role_in)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return role

@router.delete("/{role_id}", response_model=RoleResponse)
def delete_role(
    *,
    db: Session = Depends(deps.get_db),
    role_id: int,
    current_user = Depends(deps.get_current_active_superuser),
) -> Any:
    """
    Delete a role.
    """
    try:
        role = role_service.delete_role(db, role_id=role_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return role
