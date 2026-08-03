from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import func

from app.models.user import User
from app.repositories.base import BaseRepository
from app.schemas.user import UserCreate, UserUpdate


class UserRepository(BaseRepository[User, UserCreate, UserUpdate]):
    def get_by_email(self, db: Session, *, email: str) -> Optional[User]:
        # Case-insensitive (and whitespace-tolerant) so login/SSO/signup match
        # regardless of how the email was typed or stored.
        normalized = (email or "").strip().lower()
        return db.query(User).filter(func.lower(User.email) == normalized).first()

    def get_roles(self, db: Session, *, user_id: int):
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            return user.roles
        return None

user_repository = UserRepository(User)
