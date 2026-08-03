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
