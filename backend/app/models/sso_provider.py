import enum

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base import Base


class SSOProviderType(str, enum.Enum):
    GOOGLE = "google"
    AZURE = "azure"
    GITHUB = "github"
    OKTA = "okta"
    GENERIC_OIDC = "generic_oidc"


class SSOProvider(Base):
    __tablename__ = "sso_providers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(64), unique=True, nullable=False, index=True)
    display_name = Column(String(128), nullable=False)
    provider_type = Column(String(32), nullable=False, default=SSOProviderType.GENERIC_OIDC.value)

    client_id = Column(String, nullable=False)
    client_secret_encrypted = Column(Text, nullable=True)

    # OIDC / OAuth2 endpoints (auto-filled for known presets, manual for generic_oidc)
    authorization_url = Column(String, nullable=True)
    token_url = Column(String, nullable=True)
    userinfo_url = Column(String, nullable=True)
    jwks_url = Column(String, nullable=True)
    issuer = Column(String, nullable=True)

    scopes = Column(String, nullable=False, default="openid profile email")

    # Provider-specific
    tenant_id = Column(String, nullable=True)  # Azure AD

    # UI
    icon = Column(Text, nullable=True)  # SVG markup or URL
    button_color = Column(String(32), nullable=True)

    enabled = Column(Boolean, default=True, nullable=False)
    order = Column(Integer, default=0, nullable=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    identities = relationship(
        "SSOIdentity",
        back_populates="provider",
        cascade="all, delete-orphan",
    )
