from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.orm import Session

from app.core.security import verify_password
from app.models.user import User
from app.services.settings_services import settings_service
from app.services.user_service import user_service


class AuthService:
    def _get_max_login_attempts(self, db: Session) -> int:
        """Récupère le nombre max de tentatives depuis les settings."""
        password_settings = settings_service.get_password_settings(db)
        return password_settings.get("max_login_attempts", 5)

    def authenticate(self, db: Session, email: str, password: str) -> Tuple[Optional[User], Dict[str, Any]]:
        """
        Authentifie un utilisateur avec gestion du blocage par tentatives.
        Les superusers sont exemptés du blocage et du comptage des tentatives.
        Retourne (user, info) où info contient is_blocked et remaining_attempts.
        """
        max_attempts = self._get_max_login_attempts(db)
        user = user_service.get_user_by_email(db, email=email)

        if not user:
            # Uniform response to prevent user enumeration
            return None, {"is_blocked": False, "remaining_attempts": None}

        is_superuser = user.is_superuser

        if user.blocked and not is_superuser:
            return None, {"is_blocked": True, "remaining_attempts": 0}

        # SSO-only accounts have no password set; refuse password login cleanly.
        if not user.hashed_password:
            return None, {"is_blocked": False, "remaining_attempts": None}

        if not verify_password(password, user.hashed_password):
            if not is_superuser:
                user.count_tentative = (user.count_tentative or 0) + 1
                remaining = max(max_attempts - user.count_tentative, 0)

                if user.count_tentative >= max_attempts:
                    user.blocked = True
                    remaining = 0

                db.add(user)
                db.commit()
                db.refresh(user)

                return None, {
                    "is_blocked": user.blocked,
                    "remaining_attempts": remaining,
                }
            else:
                return None, {
                    "is_blocked": False,
                    "remaining_attempts": None,
                }

        # Succès : réinitialiser le compteur (même pour les superusers)
        user.count_tentative = 0
        db.add(user)
        db.commit()
        db.refresh(user)

        remaining = max_attempts
        return user, {"is_blocked": False, "remaining_attempts": remaining}

    def check_password_expiration(self, db: Session, user: User) -> Dict[str, Any]:
        """
        Vérifie l'expiration du mot de passe de l'utilisateur.
        Retourne le nombre de jours restants et si le changement est obligatoire.
        """
        password_settings = settings_service.get_password_settings(db)
        expiry_days = password_settings.get("password_expiry_days", 90)

        if not expiry_days or expiry_days <= 0:
            return {"password_days_remaining": None, "must_change_password": False}

        password_updated_at = user.password_updated_at
        if not password_updated_at:
            return {"password_days_remaining": 0, "must_change_password": True}

        # S'assurer que password_updated_at est en UTC aware
        if password_updated_at.tzinfo is None:
            password_updated_at = password_updated_at.replace(tzinfo=timezone.utc)

        now = datetime.now(timezone.utc)
        elapsed_days = (now - password_updated_at).days
        days_remaining = expiry_days - elapsed_days

        return {
            "password_days_remaining": max(days_remaining, 0),
            "must_change_password": days_remaining <= 0,
        }

auth_service = AuthService()
