from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.reconciliation_run import ReconciliationRun


class ReconciliationRunRepository:
    def create(self, db: Session, *, run: ReconciliationRun) -> ReconciliationRun:
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def update(self, db: Session, *, run: ReconciliationRun, **fields) -> ReconciliationRun:
        for k, v in fields.items():
            setattr(run, k, v)
        db.add(run)
        db.commit()
        db.refresh(run)
        return run

    def latest(self, db: Session) -> Optional[ReconciliationRun]:
        return (
            db.query(ReconciliationRun)
            .order_by(ReconciliationRun.started_at.desc())
            .first()
        )

    def list(self, db: Session, *, skip: int = 0, limit: int = 100) -> List[ReconciliationRun]:
        return (
            db.query(ReconciliationRun)
            .order_by(ReconciliationRun.started_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )


reconciliation_run_repository = ReconciliationRunRepository()
