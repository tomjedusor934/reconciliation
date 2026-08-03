from typing import Optional

from sqlalchemy.orm import Session

from app.models.settings import Settings
from app.repositories.base import BaseRepository
from app.schemas.settings import SettingCreate, SettingUpdate


class SettingRepository(BaseRepository[Settings, SettingCreate, SettingUpdate]):
    def get_by_key(self, db: Session, *, key: str) -> Optional[Settings]:
        """Récupère une setting spécifique"""
        return db.query(Settings).filter(Settings.key == key).first()

setting_repository = SettingRepository(Settings)
