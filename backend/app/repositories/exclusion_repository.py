from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.exclusion import Exclusion


class ExclusionRepository:
    def create(self, db: Session, *, exclusion: Exclusion) -> Exclusion:
        db.add(exclusion)
        db.commit()
        db.refresh(exclusion)
        return exclusion

    def list_for_entry(
        self, db: Session, *, entry_id: int
    ) -> List[Exclusion]:
        return (
            db.query(Exclusion)
            .filter(Exclusion.entry_id == entry_id)
            .order_by(Exclusion.created_at.desc())
            .all()
        )

    def get(self, db: Session, *, id_: int) -> Optional[Exclusion]:
        return db.query(Exclusion).filter(Exclusion.id == id_).first()

    def get_active_for_entry(self, db: Session, *, entry_id: int) -> Optional[Exclusion]:
        """Return the most recent non-cancelled exclusion for an entry."""
        return (
            db.query(Exclusion)
            .filter(Exclusion.entry_id == entry_id, Exclusion.cancelled_at.is_(None))
            .order_by(Exclusion.created_at.desc())
            .first()
        )

    def cancel(
        self, db: Session, *, exclusion: Exclusion, user_id: Optional[int], commit: bool = True
    ) -> Exclusion:
        """Mark an exclusion as cancelled (unexcluded).

        ``commit=False`` lets the caller bundle this with the entry move into a
        single atomic transaction.
        """
        exclusion.cancelled_at = datetime.now(timezone.utc)
        exclusion.cancelled_by_user_id = user_id
        if commit:
            db.commit()
            db.refresh(exclusion)
        return exclusion


exclusion_repository = ExclusionRepository()
