"""SSO orchestration service.

Handles provider CRUD, global SSO settings, OAuth2/OIDC authorize URL
construction, and the callback flow that exchanges the auth code for tokens
and provisions / links the local user.
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from jose import jwt
from sqlalchemy.orm import Session

from app.core.crypto import decrypt, encrypt
from app.models.settings import Settings
from app.models.sso_provider import SSOProvider, SSOProviderType
from app.models.user import User
from app.repositories.settings_repository import setting_repository
from app.repositories.sso_provider_repository import (
    sso_identity_repository,
    sso_provider_repository,
)
from app.repositories.user_repository import user_repository
from app.schemas.sso import (
    SSOPreset,
    SSOPresetField,
    SSOProviderCreate,
    SSOProviderUpdate,
    SSOSettings,
    SSOSettingsUpdate,
)

logger = logging.getLogger(__name__)


class SSOError(Exception):
    """Raised when an SSO flow cannot complete (config missing, IdP error, no provisioning, ...)."""

    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Provider presets
# ---------------------------------------------------------------------------

# Each preset describes default endpoints + the additional fields that the
# admin must fill in (besides client_id/client_secret).
PRESETS: Dict[str, Dict[str, Any]] = {
    SSOProviderType.GOOGLE.value: {
        "label": "Google",
        "default_scopes": "openid email profile",
        "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "jwks_url": "https://www.googleapis.com/oauth2/v3/certs",
        "issuer": "https://accounts.google.com",
        "fields": [],
    },
    SSOProviderType.AZURE.value: {
        "label": "Azure AD / Microsoft Entra ID",
        "default_scopes": "openid email profile",
        # endpoints templated with tenant_id at runtime
        "fields": [
            {"name": "tenant_id", "label": "Tenant ID", "required": True, "placeholder": "common, organizations, or a GUID"},
        ],
    },
    SSOProviderType.GITHUB.value: {
        "label": "GitHub",
        "default_scopes": "read:user user:email",
        "authorization_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "fields": [],
    },
    SSOProviderType.OKTA.value: {
        "label": "Okta",
        "default_scopes": "openid email profile",
        "fields": [
            {"name": "issuer", "label": "Okta Issuer", "required": True, "placeholder": "https://your-org.okta.com/oauth2/default"},
        ],
    },
    SSOProviderType.GENERIC_OIDC.value: {
        "label": "Generic OIDC / OAuth2",
        "default_scopes": "openid email profile",
        "fields": [
            {"name": "issuer", "label": "Issuer", "required": False},
            {"name": "authorization_url", "label": "Authorization URL", "required": True},
            {"name": "token_url", "label": "Token URL", "required": True},
            {"name": "userinfo_url", "label": "UserInfo URL", "required": True},
            {"name": "jwks_url", "label": "JWKS URL", "required": False},
        ],
    },
}


# ---------------------------------------------------------------------------
# Settings keys (stored in the existing key/value `settings` table)
# ---------------------------------------------------------------------------
SSO_SETTING_KEYS = {
    "sso_enabled": ("false", "Enable SSO login"),
    "sso_force": ("false", "Force SSO (disable email/password login)"),
    "sso_create_account_on_login": ("false", "Auto-create local users on first SSO login"),
    "sso_default_role_id": ("", "Default role id assigned to auto-created SSO users"),
}


def _coerce_bool(value: Optional[str]) -> bool:
    return str(value).strip().lower() == "true"


def _coerce_optional_int(value: Optional[str]) -> Optional[int]:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


class SSOService:
    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------
    def ensure_default_settings(self, db: Session) -> None:
        for key, (value, description) in SSO_SETTING_KEYS.items():
            existing = setting_repository.get_by_key(db, key=key)
            if not existing:
                db.add(Settings(key=key, value=value, description=description))
        db.commit()

    def get_settings(self, db: Session) -> SSOSettings:
        return SSOSettings(
            sso_enabled=_coerce_bool(self._get_raw(db, "sso_enabled")),
            sso_force=_coerce_bool(self._get_raw(db, "sso_force")),
            sso_create_account_on_login=_coerce_bool(self._get_raw(db, "sso_create_account_on_login")),
            sso_default_role_id=_coerce_optional_int(self._get_raw(db, "sso_default_role_id")),
        )

    def update_settings(self, db: Session, payload: SSOSettingsUpdate) -> SSOSettings:
        data = payload.model_dump(exclude_unset=True)
        for key, value in data.items():
            if key not in SSO_SETTING_KEYS:
                continue
            str_value = "" if value is None else (str(value).lower() if isinstance(value, bool) else str(value))
            setting = setting_repository.get_by_key(db, key=key)
            if setting:
                setting.value = str_value
                db.add(setting)
            else:
                db.add(Settings(key=key, value=str_value, description=SSO_SETTING_KEYS[key][1]))
        db.commit()
        return self.get_settings(db)

    def is_password_login_enabled(self, db: Session) -> bool:
        s = self.get_settings(db)
        return not (s.sso_enabled and s.sso_force)

    def _get_raw(self, db: Session, key: str) -> Optional[str]:
        s = setting_repository.get_by_key(db, key=key)
        return s.value if s else None

    # ------------------------------------------------------------------
    # Providers CRUD
    # ------------------------------------------------------------------
    def list_providers_admin(self, db: Session) -> List[SSOProvider]:
        return sso_provider_repository.list_all(db)

    def list_public_enabled_providers(self, db: Session) -> List[SSOProvider]:
        s = self.get_settings(db)
        if not s.sso_enabled:
            return []
        return sso_provider_repository.list_enabled(db)

    def get_provider(self, db: Session, provider_id: int) -> Optional[SSOProvider]:
        return sso_provider_repository.get(db, id=provider_id)

    def get_provider_by_name(self, db: Session, name: str) -> Optional[SSOProvider]:
        return sso_provider_repository.get_by_name(db, name=name)

    def create_provider(self, db: Session, payload: SSOProviderCreate) -> SSOProvider:
        if sso_provider_repository.get_by_name(db, name=payload.name):
            raise SSOError("name_taken", f"A provider named '{payload.name}' already exists.", status_code=400)
        data = payload.model_dump()
        client_secret = data.pop("client_secret", None)
        provider = SSOProvider(**data)
        if client_secret:
            provider.client_secret_encrypted = encrypt(client_secret)
        db.add(provider)
        db.commit()
        db.refresh(provider)
        return provider

    def update_provider(self, db: Session, provider: SSOProvider, payload: SSOProviderUpdate) -> SSOProvider:
        data = payload.model_dump(exclude_unset=True)
        client_secret = data.pop("client_secret", None)
        for field, value in data.items():
            setattr(provider, field, value)
        # Only overwrite the secret if a non-empty new value is supplied.
        if client_secret is not None and client_secret != "":
            provider.client_secret_encrypted = encrypt(client_secret)
        db.add(provider)
        db.commit()
        db.refresh(provider)
        return provider

    def delete_provider(self, db: Session, provider: SSOProvider) -> None:
        db.delete(provider)
        db.commit()

    def get_presets(self) -> List[SSOPreset]:
        result: List[SSOPreset] = []
        for ptype, preset in PRESETS.items():
            result.append(
                SSOPreset(
                    type=ptype,
                    label=preset["label"],
                    default_scopes=preset["default_scopes"],
                    fields=[SSOPresetField(**f) for f in preset["fields"]],
                )
            )
        return result

    # ------------------------------------------------------------------
    # OAuth2 / OIDC flow
    # ------------------------------------------------------------------
    def _resolve_endpoints(self, provider: SSOProvider) -> Dict[str, Optional[str]]:
        """Return effective endpoints for a provider, applying preset templating."""
        ptype = provider.provider_type
        preset = PRESETS.get(ptype, {})

        if ptype == SSOProviderType.AZURE.value:
            tenant = provider.tenant_id or "common"
            base = f"https://login.microsoftonline.com/{tenant}"
            return {
                "authorization_url": f"{base}/oauth2/v2.0/authorize",
                "token_url": f"{base}/oauth2/v2.0/token",
                "userinfo_url": "https://graph.microsoft.com/oidc/userinfo",
                "jwks_url": f"{base}/discovery/v2.0/keys",
                "issuer": f"https://login.microsoftonline.com/{tenant}/v2.0",
            }

        if ptype == SSOProviderType.OKTA.value:
            issuer = (provider.issuer or "").rstrip("/")
            return {
                "authorization_url": f"{issuer}/v1/authorize",
                "token_url": f"{issuer}/v1/token",
                "userinfo_url": f"{issuer}/v1/userinfo",
                "jwks_url": f"{issuer}/v1/keys",
                "issuer": issuer,
            }

        # Google / GitHub / generic_oidc: use stored values, falling back to preset defaults
        return {
            "authorization_url": provider.authorization_url or preset.get("authorization_url"),
            "token_url": provider.token_url or preset.get("token_url"),
            "userinfo_url": provider.userinfo_url or preset.get("userinfo_url"),
            "jwks_url": provider.jwks_url or preset.get("jwks_url"),
            "issuer": provider.issuer or preset.get("issuer"),
        }

    def generate_state(self) -> str:
        return secrets.token_urlsafe(32)

    def build_authorize_url(self, provider: SSOProvider, redirect_uri: str, state: str) -> str:
        endpoints = self._resolve_endpoints(provider)
        if not endpoints["authorization_url"]:
            raise SSOError("missing_authorization_url", "Provider has no authorization_url configured.", status_code=500)
        params = {
            "client_id": provider.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": provider.scopes or "openid email profile",
            "state": state,
        }
        return f"{endpoints['authorization_url']}?{urlencode(params)}"

    def _exchange_code(self, provider: SSOProvider, code: str, redirect_uri: str) -> Dict[str, Any]:
        endpoints = self._resolve_endpoints(provider)
        if not endpoints["token_url"]:
            raise SSOError("missing_token_url", "Provider has no token_url configured.", status_code=500)
        client_secret = decrypt(provider.client_secret_encrypted)
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": provider.client_id,
        }
        if client_secret:
            data["client_secret"] = client_secret
        headers = {"Accept": "application/json"}
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.post(endpoints["token_url"], data=data, headers=headers)
        except httpx.HTTPError as exc:
            raise SSOError("token_exchange_failed", f"Token endpoint unreachable: {exc}", status_code=502) from exc
        if resp.status_code >= 400:
            logger.warning("SSO token exchange failed: %s %s", resp.status_code, resp.text)
            raise SSOError("token_exchange_failed", "Failed to exchange authorization code.", status_code=400)
        try:
            payload = resp.json()
        except ValueError as exc:
            raise SSOError("token_exchange_failed", "Invalid token response.", status_code=502) from exc
        return payload

    def _fetch_userinfo(self, provider: SSOProvider, access_token: str) -> Dict[str, Any]:
        endpoints = self._resolve_endpoints(provider)
        if not endpoints["userinfo_url"]:
            raise SSOError("missing_userinfo_url", "Provider has no userinfo_url configured.", status_code=500)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(timeout=10.0) as client:
                resp = client.get(endpoints["userinfo_url"], headers=headers)
                if resp.status_code >= 400:
                    raise SSOError("userinfo_failed", "Failed to fetch user info.", status_code=502)
                userinfo = resp.json()

                # GitHub doesn't return email in /user when it's private; fetch /user/emails.
                if provider.provider_type == SSOProviderType.GITHUB.value and not userinfo.get("email"):
                    emails_resp = client.get("https://api.github.com/user/emails", headers=headers)
                    if emails_resp.status_code < 400:
                        emails = emails_resp.json() or []
                        primary = next((e for e in emails if e.get("primary") and e.get("verified")), None) or next(
                            (e for e in emails if e.get("verified")), None
                        )
                        if primary:
                            userinfo["email"] = primary.get("email")
        except httpx.HTTPError as exc:
            raise SSOError("userinfo_failed", f"UserInfo endpoint unreachable: {exc}", status_code=502) from exc
        return userinfo

    def _extract_identity(self, provider: SSOProvider, token_payload: Dict[str, Any], userinfo: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
        """Return (subject, email, full_name) from token + userinfo."""
        sub: Optional[str] = None
        email: Optional[str] = None
        name: Optional[str] = None

        # Try to decode id_token (without strict signature verification — providers
        # already authenticated via the token endpoint over TLS using client_secret).
        id_token = token_payload.get("id_token")
        if id_token:
            try:
                claims = jwt.get_unverified_claims(id_token)
                sub = sub or claims.get("sub")
                email = email or claims.get("email")
                name = name or claims.get("name")
            except Exception:  # noqa: BLE001
                pass

        # Userinfo provides authoritative profile data
        sub = sub or str(userinfo.get("sub") or userinfo.get("id") or "")
        email = email or userinfo.get("email")
        name = name or userinfo.get("name") or userinfo.get("login")

        if not sub:
            raise SSOError("no_subject", "Provider response did not contain a stable user identifier.", status_code=400)

        return str(sub), email, name

    def handle_callback(
        self,
        db: Session,
        provider: SSOProvider,
        code: str,
        redirect_uri: str,
    ) -> User:
        token_payload = self._exchange_code(provider, code=code, redirect_uri=redirect_uri)
        access_token = token_payload.get("access_token")
        if not access_token:
            raise SSOError("no_access_token", "Token endpoint did not return an access token.", status_code=400)

        userinfo = self._fetch_userinfo(provider, access_token=access_token)
        subject, email, full_name = self._extract_identity(provider, token_payload, userinfo)

        # 1) Existing identity → return its user
        identity = sso_identity_repository.get(db, provider_id=provider.id, subject=subject)
        if identity:
            user = identity.user
            if not user.is_active or user.blocked:
                raise SSOError("user_disabled", "Your account is disabled.", status_code=403)
            return user

        # 2) Match by email → link
        user: Optional[User] = None
        if email:
            user = user_repository.get_by_email(db, email=email)

        if user is None:
            settings_obj = self.get_settings(db)
            if not settings_obj.sso_create_account_on_login:
                raise SSOError(
                    "not_provisioned",
                    "No account exists for this identity. Ask an administrator to create one.",
                    status_code=403,
                )
            user = self._provision_user(db, email=email, full_name=full_name, default_role_id=settings_obj.sso_default_role_id)

        if not user.is_active or user.blocked:
            raise SSOError("user_disabled", "Your account is disabled.", status_code=403)

        sso_identity_repository.create(
            db, user_id=user.id, provider_id=provider.id, subject=subject, email=email
        )
        return user

    def _provision_user(self, db: Session, *, email: Optional[str], full_name: Optional[str], default_role_id: Optional[int]) -> User:
        if not email:
            raise SSOError("no_email", "Identity provider did not return an email address; cannot create account.", status_code=400)
        # Lazy import to avoid circular dependency
        from app.repositories.roles_repository import role_repository

        user = User(
            email=email,
            hashed_password=None,
            full_name=full_name or email.split("@")[0],
            is_active=True,
            is_superuser=False,
            password_updated_at=datetime.now(timezone.utc),
            blocked=False,
            count_tentative=0,
        )
        if default_role_id:
            role = role_repository.get(db, id=default_role_id)
            if role:
                user.roles.append(role)
        db.add(user)
        db.commit()
        db.refresh(user)
        return user


sso_service = SSOService()
