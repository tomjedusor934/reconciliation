from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.security import get_password_hash, verify_password
from app.models.user import User
from app.repositories.roles_repository import role_repository
from app.repositories.user_repository import user_repository
from app.schemas.user import UserCreate, UserUpdate


class UserService:
    def create_user(self, db: Session, user_in: UserCreate) -> User:
        user_data = user_in.model_dump()
        password = user_data.pop("password")
        role_ids = user_data.pop("role_ids", [])

        user_data["hashed_password"] = get_password_hash(password)
        user_data["password_updated_at"] = datetime.now(timezone.utc)

        db_obj = User(**user_data)

        if role_ids:
            for role_id in role_ids:
                role = role_repository.get(db, id=role_id)
                if role:
                    db_obj.roles.append(role)

        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def get_user_by_email(self, db: Session, email: str) -> User:
        return user_repository.get_by_email(db, email=email)

    def get_user(self, db: Session, user_id: int) -> User:
        return user_repository.get(db, id=user_id)

    def get_users(self, db: Session, skip: int = 0, limit: int = 100) -> list[User]:
        return user_repository.get_multi(db, skip=skip, limit=limit)

    def update_user(self, db: Session, db_user: User, user_in: UserUpdate) -> User:
        user_data = user_in.model_dump(exclude_unset=True)
        role_ids = user_data.pop("role_ids", None)
        user_data.pop("roles", None)
        current_password = user_data.pop("current_password", None)

        if user_in.password:
            # Verify current password if trying to change password
            if current_password:
                if not verify_password(current_password, db_user.hashed_password):
                    raise ValueError("Current password is incorrect")
            user_data["hashed_password"] = get_password_hash(user_data.pop("password"))
            user_data["password_updated_at"] = datetime.now(timezone.utc)

        if role_ids is not None:
            db_user.roles = []
            for role_id in role_ids:
                role = role_repository.get(db, id=role_id)
                if role:
                    db_user.roles.append(role)

        return user_repository.update(db, db_obj=db_user, obj_in=user_data)

    def add_role_to_user(self, db: Session, user_id: int, role_id: int) -> User:
        """
        Assigne un rôle à un utilisateur.
        Gère automatiquement la table d'association via la relation SQLAlchemy.
        """
        user = self.get_user(db, user_id)
        if not user:
            raise ValueError("Utilisateur introuvable")

        role = role_repository.get(db, id=role_id)
        if not role:
            raise ValueError("Rôle introuvable")

        if role not in user.roles:
            user.roles.append(role)
            db.commit()
            db.refresh(user)

        return user

    def remove_role_from_user(self, db: Session, user_id: int, role_id: int) -> User:
        """
        Retire un rôle d'un utilisateur.
        """
        user = self.get_user(db, user_id)
        if not user:
            raise ValueError("Utilisateur introuvable")

        role = role_repository.get(db, id=role_id)
        if not role:
            raise ValueError("Rôle introuvable")

        if role in user.roles:
            user.roles.remove(role)
            db.commit()
            db.refresh(user)

        return user

    def delete_user(self, db: Session, user_id: int) -> User:
        user = self.get_user(db, user_id)
        if not user:
            raise ValueError("Utilisateur introuvable")
        if user.is_superuser:
            raise ValueError("Impossible de supprimer un super-utilisateur")
        return user_repository.remove(db, id=user_id)

    def unblock_user(self, db: Session, user_id: int) -> User:
        """
        Débloque un utilisateur : remet blocked à False et count_tentative à 0.
        """
        user = self.get_user(db, user_id)
        if not user:
            raise ValueError("Utilisateur introuvable")
        if not user.blocked:
            raise ValueError("Cet utilisateur n'est pas bloqué.")
        user.blocked = False
        user.count_tentative = 0
        db.add(user)
        db.commit()
        db.refresh(user)
        return user

user_service = UserService()
