from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class SSOIdentity(Base):
    __tablename__ = "user_sso_identities"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    provider_id = Column(Integer, ForeignKey("sso_providers.id", ondelete="CASCADE"), nullable=False, index=True)
    subject = Column(String(255), nullable=False, index=True)  # provider's stable user id (sub claim)
    email = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", back_populates="sso_identities")
    provider = relationship("SSOProvider", back_populates="identities")

    __table_args__ = (
        UniqueConstraint("provider_id", "subject", name="uq_sso_identity_provider_subject"),
    )
