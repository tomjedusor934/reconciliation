from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.sso_identity import SSOIdentity
from app.models.sso_provider import SSOProvider
from app.repositories.base import BaseRepository
from app.schemas.sso import SSOProviderCreate, SSOProviderUpdate


class SSOProviderRepository(BaseRepository[SSOProvider, SSOProviderCreate, SSOProviderUpdate]):
    def get_by_name(self, db: Session, *, name: str) -> Optional[SSOProvider]:
        return db.query(SSOProvider).filter(SSOProvider.name == name).first()

    def list_enabled(self, db: Session) -> List[SSOProvider]:
        return (
            db.query(SSOProvider)
            .filter(SSOProvider.enabled.is_(True))
            .order_by(SSOProvider.order.asc(), SSOProvider.id.asc())
            .all()
        )

    def list_all(self, db: Session) -> List[SSOProvider]:
        return (
            db.query(SSOProvider)
            .order_by(SSOProvider.order.asc(), SSOProvider.id.asc())
            .all()
        )


class SSOIdentityRepository:
    def get(self, db: Session, *, provider_id: int, subject: str) -> Optional[SSOIdentity]:
        return (
            db.query(SSOIdentity)
            .filter(
                SSOIdentity.provider_id == provider_id,
                SSOIdentity.subject == subject,
            )
            .first()
        )

    def create(self, db: Session, *, user_id: int, provider_id: int, subject: str, email: Optional[str]) -> SSOIdentity:
        obj = SSOIdentity(user_id=user_id, provider_id=provider_id, subject=subject, email=email)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj


sso_provider_repository = SSOProviderRepository(SSOProvider)
sso_identity_repository = SSOIdentityRepository()
