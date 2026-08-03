import re
from typing import Any, Dict

from sqlalchemy.orm import Session

from app.models.settings import Settings
from app.repositories.settings_repository import setting_repository
from app.schemas.settings import SettingCreate, SettingUpdate


class SettingsService:
    def create_setting(self, db: Session, setting_in: SettingCreate) -> Settings:
        setting_data = setting_in.model_dump()
        db_obj = Settings(**setting_data)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def get_setting_by_key(self, db: Session, key: str) -> Settings:
        return setting_repository.get_by_key(db, key=key)

    def get_setting(self, db: Session, setting_id: int) -> Settings:
        return setting_repository.get(db, id=setting_id)

    def get_settings(self, db: Session, skip: int = 0, limit: int = 100) -> list[Settings]:
        return setting_repository.get_multi(db, skip=skip, limit=limit)

    def update_setting(self, db: Session, db_setting: Settings, setting_in: SettingUpdate) -> Settings:
        setting_data = setting_in.model_dump(exclude_unset=True)
        for field, value in setting_data.items():
            setattr(db_setting, field, value)
        db.add(db_setting)
        db.commit()
        db.refresh(db_setting)
        return db_setting

    def get_password_settings(self, db: Session) -> Dict[str, Any]:
        """Récupère tous les settings liés aux mots de passe"""
        keys = [
            "password_min_length",
            "password_require_uppercase",
            "password_require_lowercase",
            "password_require_numbers",
            "password_require_special",
            "session_timeout_minutes",
            "password_expiry_days",
            "max_login_attempts"
        ]

        settings_dict = {}
        for key in keys:
            setting = setting_repository.get_by_key(db, key=key)
            if setting:
                # Convertir les strings "true"/"false" en booléens
                value = setting.value
                if value in ("true", "false"):
                    settings_dict[key] = value.lower() == "true"
                elif key in ("password_min_length", "session_timeout_minutes", "password_expiry_days", "max_login_attempts"):
                    try:
                        settings_dict[key] = int(value)
                    except (ValueError, TypeError):
                        settings_dict[key] = value
                else:
                    settings_dict[key] = value

        return settings_dict

    def set_password_settings(self, db: Session, settings_data: Dict[str, Any]) -> Dict[str, Settings]:
        """Met à jour tous les settings liés aux mots de passe"""
        updated = {}

        for key, value in settings_data.items():
            setting = setting_repository.get_by_key(db, key=key)

            # Convertir les booléens en strings
            str_value = str(value).lower() if isinstance(value, bool) else str(value)

            if setting:
                setting.value = str_value
                db.add(setting)
            else:
                # Créer le setting s'il n'existe pas
                new_setting = Settings(key=key, value=str_value)
                db.add(new_setting)
                setting = new_setting

            updated[key] = setting

        db.commit()
        # Rafraîchir les objets après commit
        for setting in updated.values():
            db.refresh(setting)

        return updated

    def get_session_timeout_minutes(self, db: Session) -> int:
        """Récupère la durée de session en minutes depuis la BD"""
        setting = setting_repository.get_by_key(db, key="session_timeout_minutes")
        if setting:
            try:
                return int(setting.value)
            except (ValueError, TypeError):
                return 30  # Fallback si conversion échoue

    def validate_password(self, db: Session, password: str) -> Dict[str, Any]:
        """
        Valide un mot de passe selon les settings configurés.
        Retourne un dictionnaire avec:
        - is_valid: booléen indiquant si le MDP est valide
        - errors: liste des critères non respectés
        - criteria: dict avec les détails de chaque critère
        """
        settings = self.get_password_settings(db)

        criteria = {}
        errors = []

        # Critère 1: Longueur minimale
        min_length = settings.get("password_min_length", 8)
        criteria["min_length"] = {
            "required": min_length,
            "met": len(password) >= min_length
        }
        if not criteria["min_length"]["met"]:
            errors.append(f"Au minimum {min_length} caractères")

        # Critère 2: Lettres majuscules
        require_uppercase = settings.get("password_require_uppercase", False)
        criteria["uppercase"] = {
            "required": require_uppercase,
            "met": bool(re.search(r'[A-Z]', password))
        }
        if require_uppercase and not criteria["uppercase"]["met"]:
            errors.append("Au moins une lettre majuscule (A-Z)")

        # Critère 3: Lettres minuscules
        require_lowercase = settings.get("password_require_lowercase", False)
        criteria["lowercase"] = {
            "required": require_lowercase,
            "met": bool(re.search(r'[a-z]', password))
        }
        if require_lowercase and not criteria["lowercase"]["met"]:
            errors.append("Au moins une lettre minuscule (a-z)")

        # Critère 4: Chiffres
        require_numbers = settings.get("password_require_numbers", False)
        criteria["numbers"] = {
            "required": require_numbers,
            "met": bool(re.search(r'\d', password))
        }
        if require_numbers and not criteria["numbers"]["met"]:
            errors.append("Au moins un chiffre (0-9)")

        # Critère 5: Caractères spéciaux
        require_special = settings.get("password_require_special", False)
        criteria["special"] = {
            "required": require_special,
            "met": bool(re.search(r'[!@#$%^&*()_+\-=\[\]{};:\'",.<>?/\\|`~]', password))
        }
        if require_special and not criteria["special"]["met"]:
            errors.append("Au moins un caractère spécial (!@#$%^&*...)")

        return {
            "is_valid": len(errors) == 0,
            "errors": errors,
            "criteria": criteria
        }

settings_service = SettingsService()
