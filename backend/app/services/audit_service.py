from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog, UIActionLog
from app.repositories.audit_log_repository import audit_log_repository


class AuditService:
    def list_data_audit(
        self, db: Session, *, table_name: Optional[str] = None, skip: int = 0, limit: int = 100
    ) -> List[AuditLog]:
        return audit_log_repository.list_audit(db, table_name=table_name, skip=skip, limit=limit)

    def list_ui_actions(
        self,
        db: Session,
        *,
        action: Optional[str] = None,
        user_id: Optional[int] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> List[UIActionLog]:
        return audit_log_repository.list_ui_actions(
            db, action=action, user_id=user_id, skip=skip, limit=limit
        )

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
        return audit_log_repository.log_ui_action(
            db,
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            details=details,
        )


audit_service = AuditService()
