from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.roles import AccessLevelEnum, AccessiblePage, Role
from app.models.settings import Settings
from app.repositories.settings_repository import setting_repository
from app.schemas.settings import SettingCreate
from app.schemas.user import UserCreate
from app.services.roles_services import role_service
from app.services.settings_services import TABLE_COLUMNS_KEY, settings_service
from app.services.sso_service import sso_service
from app.services.user_service import UserService

# Pages accessibles par défaut au rôle superadmin (auto-attribué via SSO).
# Toute nouvelle vue importante doit être ajoutée ici.
SUPERADMIN_PAGES = [
    "/",
    "/dashboard",
    "/profile",
    "/users",
    "/users/create",
    "/users/:id",
    "/roles",
    "/roles/create",
    "/roles/:id",
    "/admin/settings",
    # Reconciliation app pages
    "/flows",
    "/flows/create",
    "/flows/:id",
    "/reconciliation",
    "/reconciliation/operational",
    "/reconciliation/transversal",
    "/reconciliation/lots",
    "/reconciliation/runs",
    "/reconciliation/audit",
]


def _ensure_superadmin_role(db: Session) -> Role:
    """Crée (ou complète) le rôle 'superadmin' avec accès ALL à toutes les pages."""
    role = role_service.get_role_by_name(db, name="superadmin")
    if not role:
        role = Role(name="superadmin", description="Accès total — auto-assigné via SSO")
        db.add(role)
        db.commit()
        db.refresh(role)

    existing_paths = {p.path for p in role.accessible_pages}
    missing = [p for p in SUPERADMIN_PAGES if p not in existing_paths]
    for path in missing:
        db.add(AccessiblePage(path=path, access_level=AccessLevelEnum.ALL, role_id=role.id))
    if missing:
        db.commit()
        db.refresh(role)
    return role


def init_db(db: Session) -> None:
    user_service = UserService()

    # Ensure pre-existing schemas pick up the new nullable hashed_password column.
    # `Base.metadata.create_all()` does not ALTER existing columns, so we do it once here.
    try:
        db.execute(text('ALTER TABLE "user" ALTER COLUMN hashed_password DROP NOT NULL'))
        db.commit()
    except Exception:
        db.rollback()

    user = user_service.get_user_by_email(db, email=settings.FIRST_SUPERUSER)
    if not user:
        user_in = UserCreate(
            email=settings.FIRST_SUPERUSER,
            password=settings.FIRST_SUPERUSER_PASSWORD,
            is_superuser=True,
            full_name="Initial Admin",
            is_active=True,
        )
        user = user_service.create_user(db, user_in)
        print(f"Superuser {settings.FIRST_SUPERUSER} created.")
    else:
        print(f"Superuser {settings.FIRST_SUPERUSER} already exists.")

    settings_app = settings_service.get_setting_by_key(db, key="app_title")
    if not settings_app:
        setting_in = SettingCreate(
            key="app_title",
            value=settings.TITLE,
            description="Titre de l'application"
        )
        settings_service.create_setting(db, setting_in)
        print("Setting 'app_title' created.")
    else:
        print("Setting 'app_title' already exists.")

    # Initialiser l'icône SVG par défaut (vide au départ)
    settings_icon = settings_service.get_setting_by_key(db, key="app_icon")
    if not settings_icon:
        setting_in = SettingCreate(
            key="app_icon",
            value="",  # Vide au départ, l'utilisateur peut configurer son propre SVG
            description="Icône SVG de l'application"
        )
        settings_service.create_setting(db, setting_in)
        print("Setting 'app_icon' created.")
    else:
        print("Setting 'app_icon' already exists.")

    # Initialiser les settings de mots de passe
    password_settings = {
        "password_min_length": ("8", "Longueur minimale du mot de passe"),
        "password_require_uppercase": ("true", "Majuscules requises dans le mot de passe"),
        "password_require_lowercase": ("true", "Minuscules requises dans le mot de passe"),
        "password_require_numbers": ("true", "Chiffres requis dans le mot de passe"),
        "password_require_special": ("true", "Caractères spéciaux requis dans le mot de passe"),
        "session_timeout_minutes": ("60", "Durée de session en minutes avant déconnexion"),
        "password_expiry_days": ("90", "Nombre de jours avant expiration du mot de passe (0 = pas d'expiration)"),
        "max_login_attempts": ("5", "Nombre maximal de tentatives de connexion avant blocage"),
    }

    for key, (value, description) in password_settings.items():
        existing = settings_service.get_setting_by_key(db, key=key)
        if not existing:
            setting_in = SettingCreate(
                key=key,
                value=value,
                description=description
            )
            settings_service.create_setting(db, setting_in)
            print(f"Setting '{key}' created.")
        else:
            print(f"Setting '{key}' already exists.")

    # SSO global settings (sso_enabled, sso_force, sso_create_account_on_login, sso_default_role_id)
    sso_service.ensure_default_settings(db)
    print("SSO settings ensured.")

    # Operational table columns per flow. Seeded with exactly the set that used
    # to be hard-coded in OperationalView.vue (and the nostro_mirror variant that
    # carried PaymentTimestamp), so making the columns configurable changes
    # nothing on an existing install. Only created when absent — never
    # overwrites an administrator's configuration.
    if not settings_service.get_setting_by_key(db, key=TABLE_COLUMNS_KEY):
        default_columns = [
            "flow_id", "reco_id", "account", "amount", "payment_statuses",
            "value_date", "event_type", "transaction_id",
            "transaction_particulars", "ref_no", "remarks_1", "status",
        ]
        nostro_columns = default_columns.copy()
        nostro_columns.insert(
            default_columns.index("payment_statuses") + 1, "payment_timestamp"
        )
        settings_service.set_table_columns(
            db, {"__default__": default_columns, "nostro_mirror": nostro_columns}
        )
        print(f"Setting '{TABLE_COLUMNS_KEY}' created.")
    else:
        print(f"Setting '{TABLE_COLUMNS_KEY}' already exists.")

    # Seed du rôle 'superadmin' (auto-attribué via SSO) avec accès ALL.
    superadmin_role = _ensure_superadmin_role(db)
    print(f"Role 'superadmin' ensured (id={superadmin_role.id}).")

    # Active SSO-only par défaut (si pas déjà configuré explicitement) :
    # - sso_enabled = true
    # - sso_force = true (désactive le login email/password)
    # - sso_create_account_on_login = true
    # - sso_default_role_id = id du rôle superadmin
    sso_defaults_to_apply = {
        "sso_enabled": "true",
        "sso_force": "false",
        "sso_create_account_on_login": "true",
        "sso_default_role_id": str(superadmin_role.id),
    }
    for key, value in sso_defaults_to_apply.items():
        existing = setting_repository.get_by_key(db, key=key)
        # On ne pousse la valeur que si l'admin n'a pas déjà personnalisé
        # (heuristique : valeur vide / "false" sur une fresh install).
        if existing is None:
            db.add(Settings(key=key, value=value))
        elif existing.value in (None, "", "false") and key != "sso_default_role_id":
            existing.value = value
            db.add(existing)
        elif key == "sso_default_role_id" and (existing.value in (None, "")):
            existing.value = value
            db.add(existing)
    db.commit()
    print("SSO-only defaults applied (sso_enabled=true, sso_force=false, auto-create=true).")
