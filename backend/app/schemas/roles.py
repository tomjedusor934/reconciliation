from typing import List, Optional

from pydantic import BaseModel, ConfigDict

from app.models.roles import AccessLevelEnum


class AccessiblePageSchema(BaseModel):
    path: str
    access_level: AccessLevelEnum

class RolesBase(BaseModel):
    name: str
    description: Optional[str] = None
    accessible_pages: List[AccessiblePageSchema] = []

class RoleCreate(RolesBase):
    pass

class RoleUpdate(RolesBase):
    name: Optional[str] = None
    description: Optional[str] = None
    accessible_pages: Optional[List[AccessiblePageSchema]] = None
    pass

class RoleResponse(RolesBase):
    id: int

    model_config = ConfigDict(from_attributes=True)
