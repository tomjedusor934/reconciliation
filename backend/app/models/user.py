from datetime import datetime, timezone

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.orm import relationship

from app.db.base import Base


class User(Base):
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=True)
    full_name = Column(String, index=True)
    is_active = Column(Boolean(), default=True)
    is_superuser = Column(Boolean(), default=False)
    password_updated_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    blocked = Column(Boolean(), default=False)
    count_tentative = Column(Integer, default=0)

    roles = relationship("Role",
                         secondary="user_roles",
                         back_populates="users",
                         lazy='selectin'# selectin charge les rôles avec la requête principale
                        )

    sso_identities = relationship(
        "SSOIdentity",
        back_populates="user",
        cascade="all, delete-orphan",
    )
