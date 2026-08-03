"""SSO endpoints: public discovery, OAuth2 redirect/callback, and admin CRUD."""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, List
from urllib.parse import urlencode

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.core import security
from app.core.config import settings as app_settings
from app.schemas.sso import (
    SSOPreset,
    SSOProviderCreate,
    SSOProviderResponse,
    SSOProviderUpdate,
    SSOPublicConfig,
    SSOPublicProvider,
    SSOSettings,
    SSOSettingsUpdate,
)
from app.services.settings_services import settings_service
from app.services.sso_service import SSOError, sso_service

router = APIRouter()

STATE_COOKIE_NAME = "sso_state"
PROVIDER_COOKIE_NAME = "sso_provider"
STATE_COOKIE_TTL_SECONDS = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _provider_to_response(provider) -> SSOProviderResponse:
    return SSOProviderResponse(
        id=provider.id,
        name=provider.name,
        display_name=provider.display_name,
        provider_type=provider.provider_type,
        client_id=provider.client_id,
        authorization_url=provider.authorization_url,
        token_url=provider.token_url,
        userinfo_url=provider.userinfo_url,
        jwks_url=provider.jwks_url,
        issuer=provider.issuer,
        scopes=provider.scopes,
        tenant_id=provider.tenant_id,
        icon=provider.icon,
        button_color=provider.button_color,
        enabled=provider.enabled,
        order=provider.order,
        has_secret=bool(provider.client_secret_encrypted),
    )


def _build_callback_uri(request: Request, provider_name: str) -> str:
    base = app_settings.BACKEND_URL.rstrip("/")
    return f"{base}{app_settings.API_V1_STR}/sso/callback/{provider_name}"


def _is_secure() -> bool:
    return app_settings.ENVIRONMENT != "dev"


def _set_session_cookies(response: Response, db: Session, user_id: int) -> None:
    session_timeout_minutes = settings_service.get_session_timeout_minutes(db)
    access_token = security.create_access_token(user_id, expires_delta=timedelta(minutes=session_timeout_minutes))
    csrf_token = secrets.token_hex(32)
    is_secure = _is_secure()

    response.set_cookie(
        key="access_token",
        value=f"Bearer {access_token}",
        httponly=True,
        max_age=session_timeout_minutes * 60,
        expires=session_timeout_minutes * 60,
        samesite="lax",
        secure=is_secure,
    )
    response.set_cookie(
        key="csrf_token",
        value=csrf_token,
        httponly=False,
        max_age=session_timeout_minutes * 60,
        expires=session_timeout_minutes * 60,
        samesite="lax",
        secure=is_secure,
    )


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------
@router.get("/providers", response_model=SSOPublicConfig)
def public_providers(db: Session = Depends(deps.get_db)) -> Any:
    """Public payload consumed by the LoginView to render SSO buttons."""
    s = sso_service.get_settings(db)
    providers = sso_service.list_public_enabled_providers(db) if s.sso_enabled else []
    return SSOPublicConfig(
        sso_enabled=s.sso_enabled,
        sso_force=s.sso_force,
        password_login_enabled=sso_service.is_password_login_enabled(db),
        providers=[SSOPublicProvider.model_validate(p) for p in providers],
    )


@router.get("/login/{provider_name}")
def sso_login(provider_name: str, request: Request, db: Session = Depends(deps.get_db)):
    """Redirect the browser to the IdP authorization URL."""
    s = sso_service.get_settings(db)
    if not s.sso_enabled:
        raise HTTPException(status_code=404, detail="SSO is disabled")

    provider = sso_service.get_provider_by_name(db, provider_name)
    if not provider or not provider.enabled:
        raise HTTPException(status_code=404, detail="Unknown SSO provider")

    state = sso_service.generate_state()
    redirect_uri = _build_callback_uri(request, provider_name)

    try:
        url = sso_service.build_authorize_url(provider, redirect_uri=redirect_uri, state=state)
    except SSOError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    response = RedirectResponse(url=url, status_code=302)
    is_secure = _is_secure()
    response.set_cookie(
        key=STATE_COOKIE_NAME,
        value=state,
        max_age=STATE_COOKIE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_secure,
    )
    response.set_cookie(
        key=PROVIDER_COOKIE_NAME,
        value=provider_name,
        max_age=STATE_COOKIE_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_secure,
    )
    return response


@router.get("/callback/{provider_name}")
def sso_callback(
    provider_name: str,
    request: Request,
    code: str = Query(default=None),
    state: str = Query(default=None),
    error: str = Query(default=None),
    db: Session = Depends(deps.get_db),
    state_cookie: str = Cookie(default=None, alias=STATE_COOKIE_NAME),
    provider_cookie: str = Cookie(default=None, alias=PROVIDER_COOKIE_NAME),
):
    frontend_base = app_settings.FRONTEND_URL.rstrip("/")

    def _redirect_with_error(code_: str) -> RedirectResponse:
        params = urlencode({"sso_error": code_})
        resp = RedirectResponse(url=f"{frontend_base}/login?{params}", status_code=302)
        resp.delete_cookie(STATE_COOKIE_NAME, samesite="lax", secure=_is_secure())
        resp.delete_cookie(PROVIDER_COOKIE_NAME, samesite="lax", secure=_is_secure())
        return resp

    if error:
        return _redirect_with_error(error)
    if not code or not state:
        return _redirect_with_error("missing_params")
    if not state_cookie or not secrets.compare_digest(state, state_cookie):
        return _redirect_with_error("state_mismatch")
    if provider_cookie and provider_cookie != provider_name:
        return _redirect_with_error("provider_mismatch")

    provider = sso_service.get_provider_by_name(db, provider_name)
    if not provider or not provider.enabled:
        return _redirect_with_error("unknown_provider")

    redirect_uri = _build_callback_uri(request, provider_name)
    try:
        user = sso_service.handle_callback(db, provider=provider, code=code, redirect_uri=redirect_uri)
    except SSOError as exc:
        return _redirect_with_error(exc.code)

    response = RedirectResponse(url=f"{frontend_base}/", status_code=302)
    response.delete_cookie(STATE_COOKIE_NAME, samesite="lax", secure=_is_secure())
    response.delete_cookie(PROVIDER_COOKIE_NAME, samesite="lax", secure=_is_secure())
    _set_session_cookies(response, db, user.id)
    return response


# ---------------------------------------------------------------------------
# Admin endpoints
# ---------------------------------------------------------------------------
@router.get("/admin/settings", response_model=SSOSettings)
def admin_get_settings(
    db: Session = Depends(deps.get_db),
    _user=Depends(deps.get_current_active_superuser),
) -> Any:
    return sso_service.get_settings(db)


@router.put("/admin/settings", response_model=SSOSettings)
def admin_update_settings(
    payload: SSOSettingsUpdate,
    db: Session = Depends(deps.get_db),
    _user=Depends(deps.get_current_active_superuser),
) -> Any:
    return sso_service.update_settings(db, payload)


@router.get("/admin/presets", response_model=List[SSOPreset])
def admin_get_presets(_user=Depends(deps.get_current_active_superuser)) -> Any:
    return sso_service.get_presets()


@router.get("/admin/providers", response_model=List[SSOProviderResponse])
def admin_list_providers(
    db: Session = Depends(deps.get_db),
    _user=Depends(deps.get_current_active_superuser),
) -> Any:
    providers = sso_service.list_providers_admin(db)
    return [_provider_to_response(p) for p in providers]


@router.post("/admin/providers", response_model=SSOProviderResponse)
def admin_create_provider(
    payload: SSOProviderCreate,
    db: Session = Depends(deps.get_db),
    _user=Depends(deps.get_current_active_superuser),
) -> Any:
    try:
        provider = sso_service.create_provider(db, payload)
    except SSOError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _provider_to_response(provider)


@router.put("/admin/providers/{provider_id}", response_model=SSOProviderResponse)
def admin_update_provider(
    provider_id: int,
    payload: SSOProviderUpdate,
    db: Session = Depends(deps.get_db),
    _user=Depends(deps.get_current_active_superuser),
) -> Any:
    provider = sso_service.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    provider = sso_service.update_provider(db, provider, payload)
    return _provider_to_response(provider)


@router.delete("/admin/providers/{provider_id}")
def admin_delete_provider(
    provider_id: int,
    db: Session = Depends(deps.get_db),
    _user=Depends(deps.get_current_active_superuser),
) -> Any:
    provider = sso_service.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    sso_service.delete_provider(db, provider)
    return {"status": "deleted"}
