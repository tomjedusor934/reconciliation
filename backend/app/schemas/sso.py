from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field


class SSOProviderBase(BaseModel):
    name: str = Field(..., max_length=64, description="Slug-like unique identifier (used in URLs)")
    display_name: str = Field(..., max_length=128)
    provider_type: str
    client_id: str
    authorization_url: Optional[str] = None
    token_url: Optional[str] = None
    userinfo_url: Optional[str] = None
    jwks_url: Optional[str] = None
    issuer: Optional[str] = None
    scopes: str = "openid profile email"
    tenant_id: Optional[str] = None
    icon: Optional[str] = None
    button_color: Optional[str] = None
    enabled: bool = True
    order: int = 0


class SSOProviderCreate(SSOProviderBase):
    client_secret: Optional[str] = None


class SSOProviderUpdate(BaseModel):
    display_name: Optional[str] = None
    provider_type: Optional[str] = None
    client_id: Optional[str] = None
    client_secret: Optional[str] = None  # leave None to keep existing
    authorization_url: Optional[str] = None
    token_url: Optional[str] = None
    userinfo_url: Optional[str] = None
    jwks_url: Optional[str] = None
    issuer: Optional[str] = None
    scopes: Optional[str] = None
    tenant_id: Optional[str] = None
    icon: Optional[str] = None
    button_color: Optional[str] = None
    enabled: Optional[bool] = None
    order: Optional[int] = None


class SSOProviderResponse(SSOProviderBase):
    id: int
    has_secret: bool

    model_config = ConfigDict(from_attributes=True)


class SSOPublicProvider(BaseModel):
    """Public payload exposed on the login page (no secrets)."""
    name: str
    display_name: str
    provider_type: str
    icon: Optional[str] = None
    button_color: Optional[str] = None
    order: int = 0

    model_config = ConfigDict(from_attributes=True)


class SSOPublicConfig(BaseModel):
    sso_enabled: bool
    sso_force: bool
    password_login_enabled: bool
    providers: List[SSOPublicProvider]


class SSOSettings(BaseModel):
    sso_enabled: bool = False
    sso_force: bool = False
    sso_create_account_on_login: bool = False
    sso_default_role_id: Optional[int] = None


class SSOSettingsUpdate(BaseModel):
    sso_enabled: Optional[bool] = None
    sso_force: Optional[bool] = None
    sso_create_account_on_login: Optional[bool] = None
    sso_default_role_id: Optional[int] = None


class SSOPresetField(BaseModel):
    name: str
    label: str
    required: bool = False
    placeholder: Optional[str] = None


class SSOPreset(BaseModel):
    type: str
    label: str
    default_scopes: str
    fields: List[SSOPresetField]
