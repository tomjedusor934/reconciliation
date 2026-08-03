from typing import Optional

from app.models.source_connection import SourceConnection
from app.repositories.base import BaseRepository
from app.schemas.source_connection import SourceConnectionCreate, SourceConnectionUpdate
from sqlalchemy.orm import Session


class SourceConnectionRepository(
    BaseRepository[SourceConnection, SourceConnectionCreate, SourceConnectionUpdate]
):
    def get_by_code(self, db: Session, *, code: str) -> Optional[SourceConnection]:
        return db.query(SourceConnection).filter(SourceConnection.code == code).first()


source_connection_repository = SourceConnectionRepository(SourceConnection)
