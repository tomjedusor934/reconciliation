from typing import Optional

from sqlalchemy.orm import Session

from app.models.roles import Role
from app.repositories.base import BaseRepository
from app.schemas.roles import RoleCreate, RoleUpdate


class RoleRepository(BaseRepository[Role, RoleCreate, RoleUpdate]):
    def get_by_name(self, db: Session, *, name: str) -> Optional[Role]:
        return db.query(Role).filter(Role.name == name).first()


role_repository = RoleRepository(Role)
