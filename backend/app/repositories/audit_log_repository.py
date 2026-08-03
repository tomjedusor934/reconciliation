from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog, UIActionLog


class AuditLogRepository:
    def list_audit(
        self,
        db: Session,
        *,
        table_name: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[AuditLog]:
        q = db.query(AuditLog)
        if table_name:
            q = q.filter(AuditLog.table_name == table_name)
        return q.order_by(AuditLog.ts.desc()).offset(skip).limit(limit).all()

    def list_ui_actions(
        self,
        db: Session,
        *,
        action: Optional[str] = None,
        user_id: Optional[int] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[UIActionLog]:
        q = db.query(UIActionLog)
        if action:
            q = q.filter(UIActionLog.action == action)
        if user_id is not None:
            q = q.filter(UIActionLog.user_id == user_id)
        return q.order_by(UIActionLog.ts.desc()).offset(skip).limit(limit).all()

    def log_ui_action(
        self,
        db: Session,
        *,
        user_id: Optional[int],
        action: str,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        details: Optional[dict] = None,
    ) -> UIActionLog:
        from datetime import timezone

        log = UIActionLog(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
            ts=datetime.now(timezone.utc),
        )
        db.add(log)
        db.commit()
        db.refresh(log)
        return log


audit_log_repository = AuditLogRepository()
