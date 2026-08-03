from typing import Optional

from pydantic import BaseModel, ConfigDict, EmailStr

from app.schemas.roles import RoleResponse


class UserBase(BaseModel):
    email: Optional[EmailStr] = None
    is_active: Optional[bool] = True
    is_superuser: bool = False
    full_name: Optional[str] = None
    roles: Optional[list[RoleResponse]] = []
    blocked: Optional[bool] = False
    count_tentative: Optional[int] = 0

class UserCreate(UserBase):
    email: EmailStr
    password: str
    role_ids: Optional[list[int]] = []

class UserUpdate(UserBase):
    password: Optional[str] = None
    current_password: Optional[str] = None
    role_ids: Optional[list[int]] = None

class UserResponse(UserBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
