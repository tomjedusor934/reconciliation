from typing import Optional

from pydantic import BaseModel, ConfigDict


class SettingBase(BaseModel):
    key: str
    value: Optional[str] = None
    description: Optional[str] = None

class SettingCreate(SettingBase):
    value: str  # La valeur est requise pour la création
    description: Optional[str] = None

class SettingUpdate(BaseModel):
    value: Optional[str] = None
    description: Optional[str] = None

class SettingResponse(SettingBase):
    id: int

    model_config = ConfigDict(from_attributes=True)

class PasswordSettingsInput(BaseModel):
    """Schéma pour les settings de mots de passe"""
    password_min_length: Optional[int] = None
    password_require_uppercase: Optional[bool] = None
    password_require_lowercase: Optional[bool] = None
    password_require_numbers: Optional[bool] = None
    password_require_special: Optional[bool] = None
    session_timeout_minutes: Optional[int] = None
    password_expiry_days: Optional[int] = None
    max_login_attempts: Optional[int] = None

class PasswordValidationRequest(BaseModel):
    """Requête de validation de mot de passe"""
    password: str

class PasswordValidationResponse(BaseModel):
    """Réponse de validation de mot de passe"""
    is_valid: bool
    errors: list[str]
    criteria: dict[str, dict[str, bool | int]]
