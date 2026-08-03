import secrets
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from app.api.v1 import deps
from app.core import security
from app.core.config import settings
from app.schemas.user import UserCreate, UserResponse
from app.services.auth_service import auth_service
from app.services.settings_services import settings_service
from app.services.sso_service import sso_service
from app.services.user_service import user_service

router = APIRouter()

@router.post("/login")
def login_access_token(
    response: Response,
    db: Session = Depends(deps.get_db),
    form_data: OAuth2PasswordRequestForm = Depends()
) -> Any:
    if not sso_service.is_password_login_enabled(db):
        raise HTTPException(
            status_code=403,
            detail="Password login is disabled. Please use Single Sign-On.",
        )

    user, login_info = auth_service.authenticate(
        db, email=form_data.username, password=form_data.password
    )

    if login_info.get("is_blocked"):
        return {
            "message": "Account blocked",
            "user": None,
            "password_days_remaining": None,
            "must_change_password": False,
            "is_blocked": True,
            "remaining_attempts": 0,
        }

    if not user:
        remaining = login_info.get("remaining_attempts")
        return {
            "message": "Incorrect email or password",
            "user": None,
            "password_days_remaining": None,
            "must_change_password": False,
            "is_blocked": False,
            "remaining_attempts": remaining,
        }

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Inactive user")

    session_timeout_minutes = settings_service.get_session_timeout_minutes(db)
    access_token_expires = timedelta(minutes=session_timeout_minutes)
    access_token = security.create_access_token(
        user.id, expires_delta=access_token_expires
    )

    csrf_token = secrets.token_hex(32)

    is_secure = settings.ENVIRONMENT != "dev"

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

    expiration_info = auth_service.check_password_expiration(db, user)

    return {
        "message": "Login successful",
        "user": {"email": user.email, "id": user.id},
        "password_days_remaining": expiration_info["password_days_remaining"],
        "must_change_password": expiration_info["must_change_password"],
        "is_blocked": False,
        "remaining_attempts": login_info.get("remaining_attempts"),
    }

@router.post("/logout")
def logout(response: Response):
    is_secure = settings.ENVIRONMENT != "dev"
    response.delete_cookie("access_token", secure=is_secure, samesite="lax")
    response.delete_cookie("csrf_token", secure=is_secure, samesite="lax")
    return {"message": "Logout successful"}


@router.post("/refresh")
def refresh_token(
    response: Response,
    db: Session = Depends(deps.get_db),
    current_user=Depends(deps.get_current_active_user),
) -> Any:
    """Renew the access token to extend an active session (sliding window)."""
    session_timeout_minutes = settings_service.get_session_timeout_minutes(db)
    access_token_expires = timedelta(minutes=session_timeout_minutes)
    access_token = security.create_access_token(
        current_user.id, expires_delta=access_token_expires
    )
    csrf_token = secrets.token_hex(32)

    is_secure = settings.ENVIRONMENT != "dev"

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
    return {"message": "Token refreshed"}

@router.get("/password-status")
def check_password_status(
    db: Session = Depends(deps.get_db),
    current_user = Depends(deps.get_current_active_user),
) -> Any:
    """Vérifie l'état d'expiration du mot de passe de l'utilisateur connecté."""
    expiration_info = auth_service.check_password_expiration(db, current_user)
    return expiration_info

@router.post("/signup", response_model=UserResponse)
def create_user_signup(
    *,
    db: Session = Depends(deps.get_db),
    user_in: UserCreate,
    current_user=Depends(deps.get_current_active_superuser),
) -> Any:
    user = user_service.get_user_by_email(db, email=user_in.email)
    if user:
        raise HTTPException(
            status_code=400,
            detail="The user with this email already exists in the system",
        )

    validation_result = settings_service.validate_password(db, user_in.password)
    if not validation_result["is_valid"]:
        raise HTTPException(
            status_code=400,
            detail=f"Password does not meet requirements: {', '.join(validation_result['errors'])}",
        )

    user = user_service.create_user(db, user_in=user_in)
    return user
