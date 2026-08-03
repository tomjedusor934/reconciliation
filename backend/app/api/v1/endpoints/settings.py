import re as regex_module
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.v1.deps import get_current_active_superuser, get_db
from app.models.user import User
from app.schemas.settings import (
    PasswordSettingsInput,
    PasswordValidationRequest,
    PasswordValidationResponse,
    SettingResponse,
    SettingUpdate,
)
from app.services.settings_services import settings_service

router = APIRouter()


def sanitize_svg(svg_content: str) -> str:
    """Remove potentially dangerous elements and attributes from SVG content."""
    if not svg_content or not svg_content.strip():
        return svg_content

    dangerous_tags = [
        r"<script[^>]*>.*?</script>",
        r"<iframe[^>]*>.*?</iframe>",
        r"<object[^>]*>.*?</object>",
        r"<embed[^>]*>.*?</embed>",
        r"<foreignObject[^>]*>.*?</foreignObject>",
    ]
    for pattern in dangerous_tags:
        svg_content = regex_module.sub(pattern, "", svg_content, flags=regex_module.IGNORECASE | regex_module.DOTALL)

    event_attrs = regex_module.compile(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', regex_module.IGNORECASE)
    svg_content = event_attrs.sub("", svg_content)

    href_js = regex_module.compile(r'(href|xlink:href)\s*=\s*["\']javascript:[^"\']*["\']', regex_module.IGNORECASE)
    svg_content = href_js.sub("", svg_content)

    return svg_content

@router.get("/app-name", response_model=SettingResponse)
def get_app_name(db: Session = Depends(get_db)):
    """Récupère le nom de l'application"""
    setting = settings_service.get_setting_by_key(db, key="app_title")
    if not setting:
        raise HTTPException(status_code=404, detail="Paramètre app_title non trouvé")
    return setting

@router.put("/app-name", response_model=SettingResponse)
def update_app_name(
    setting_in: SettingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Met à jour le nom de l'application"""
    setting = settings_service.get_setting_by_key(db, key="app_title")
    if not setting:
        raise HTTPException(status_code=404, detail="Paramètre app_title non trouvé")

    return settings_service.update_setting(db, db_setting=setting, setting_in=setting_in)

@router.get("/app-icon", response_model=SettingResponse)
def get_app_icon(db: Session = Depends(get_db)):
    """Récupère l'icône de l'application"""
    setting = settings_service.get_setting_by_key(db, key="app_icon")
    if not setting:
        raise HTTPException(status_code=404, detail="Paramètre app_icon non trouvé")
    return setting

@router.put("/app-icon", response_model=SettingResponse)
def update_app_icon(
    setting_in: SettingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Met à jour l'icône de l'application (SVG)"""
    setting = settings_service.get_setting_by_key(db, key="app_icon")
    if not setting:
        raise HTTPException(status_code=404, detail="Paramètre app_icon non trouvé")

    if setting_in.value:
        setting_in.value = sanitize_svg(setting_in.value)

    return settings_service.update_setting(db, db_setting=setting, setting_in=setting_in)

@router.get("/password-settings", response_model=Dict[str, Any])
def get_password_settings(db: Session = Depends(get_db)):
    """Récupère tous les settings de mots de passe"""
    return settings_service.get_password_settings(db)

@router.put("/password-settings", response_model=Dict[str, Any])
def update_password_settings(
    settings_in: PasswordSettingsInput,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_superuser),
):
    """Met à jour les settings de mots de passe"""
    # Filtrer les champs None
    settings_data = settings_in.model_dump(exclude_unset=True)

    try:
        settings_service.set_password_settings(db, settings_data)
        # Retourner les settings mis à jour
        return settings_service.get_password_settings(db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/validate-password", response_model=PasswordValidationResponse)
def validate_password(
    request: PasswordValidationRequest,
    db: Session = Depends(get_db),
):
    """Valide un mot de passe selon les critères configurés"""
    validation_result = settings_service.validate_password(db, request.password)
    return PasswordValidationResponse(**validation_result)
