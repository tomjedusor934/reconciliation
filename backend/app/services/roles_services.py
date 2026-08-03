from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.roles import AccessiblePage, Role
from app.repositories.roles_repository import role_repository
from app.schemas.roles import RoleCreate, RoleUpdate


class RoleService:
    def create_role(self, db: Session, role_in: RoleCreate) -> Role:
        """
        Crée un nouveau rôle s'il n'existe pas déjà.
        """
        existing_role = role_repository.get_by_name(db, name=role_in.name)
        if existing_role:
            raise ValueError(f"Le rôle '{role_in.name}' existe déjà.")

        # Extract accessible_pages to handle them separately
        role_data = role_in.model_dump(exclude={"accessible_pages"})
        accessible_pages = role_in.accessible_pages

        role = Role(**role_data)
        db.add(role)
        db.commit()
        db.refresh(role)

        if accessible_pages:
            for page_data in accessible_pages:
                page = AccessiblePage(
                    path=page_data.path,
                    access_level=page_data.access_level,
                    role_id=role.id
                )
                db.add(page)
            db.commit()
            db.refresh(role)

        return role

    def get_role(self, db: Session, role_id: int) -> Optional[Role]:
        """
        Récupère un rôle par son ID.
        """
        return role_repository.get(db, id=role_id)

    def get_role_by_name(self, db: Session, name: str) -> Optional[Role]:
        """
        Récupère un rôle par son nom.
        """
        return role_repository.get_by_name(db, name=name)

    def get_roles(self, db: Session, skip: int = 0, limit: int = 100) -> List[Role]:
        """
        Récupère la liste des rôles (paginée).
        """
        return role_repository.get_multi(db, skip=skip, limit=limit)

    def update_role(self, db: Session, role_id: int, role_in: RoleUpdate) -> Role:
        """
        Met à jour un rôle existant.
        """
        role = self.get_role(db, role_id)
        if not role:
            raise ValueError("Rôle introuvable")

        # Handle accessible_pages
        update_data = role_in.model_dump(exclude_unset=True)
        accessible_pages = update_data.pop("accessible_pages", None)

        role = role_repository.update(db, db_obj=role, obj_in=update_data)

        if accessible_pages is not None:
            # Clear existing pages
            db.query(AccessiblePage).filter(AccessiblePage.role_id == role.id).delete()

            # Add new pages
            for page_data in accessible_pages:
                page = AccessiblePage(
                    path=page_data["path"],
                    access_level=page_data["access_level"],
                    role_id=role.id
                )
                db.add(page)

            db.commit()
            db.refresh(role)

        return role

    def delete_role(self, db: Session, role_id: int) -> Role:
        """
        Supprime un rôle.
        """
        role = self.get_role(db, role_id)
        if not role:
            raise ValueError("Rôle introuvable")
        return role_repository.remove(db, id=role_id)

role_service = RoleService()
