from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.match_group import MatchGroup


class MatchGroupRepository:
    def create(self, db: Session, *, mg: MatchGroup) -> MatchGroup:
        db.add(mg)
        db.commit()
        db.refresh(mg)
        return mg

    def delete(self, db: Session, *, match_group_id: int) -> int:
        """Drop a group. Only used to undo a force that could not mark all of
        its entries — a group with no members would otherwise linger."""
        count = (
            db.query(MatchGroup)
            .filter(MatchGroup.id == match_group_id)
            .delete(synchronize_session=False)
        )
        db.commit()
        return count

    def list_filtered(
        self,
        db: Session,
        *,
        flow_id: Optional[int] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[MatchGroup]:
        q = db.query(MatchGroup)
        if flow_id is not None:
            q = q.filter(MatchGroup.flow_id == flow_id)
        return (
            q.order_by(MatchGroup.created_at.desc()).offset(skip).limit(limit).all()
        )


match_group_repository = MatchGroupRepository()
