from pathlib import Path
from typing import List, Union

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

# Chemin vers le fichier .env global (à la racine du projet)
# En local: /Users/.../OrchestroTemplate/.env
# En Docker (volume): /app (backend) → ../../.env (racine)
ENV_FILE = Path(__file__).resolve().parent.parent.parent.parent / ".env"

# Project settings imported from .env file
class Settings(BaseSettings):
    PROJECT_NAME: str = Field(default="Orchestro Template", env="PROJECT_NAME")
    API_V1_STR: str = Field(default="/api/v1", env="API_V1_STR")
    ENVIRONMENT: str = Field(default="dev", env="ENVIRONMENT")
    SECRET_KEY: str = Field(default="changethis_secret_key_for_production_use_only", env="SECRET_KEY")
    ALGORITHM: str = Field(default="HS256", env="ALGORITHM")
    ACCESS_TOKEN_EXPIRE_MINUTES: int = Field(default=30, env="ACCESS_TOKEN_EXPIRE_MINUTES")
    REFRESH_TOKEN_EXPIRE_DAYS: int = Field(default=7, env="REFRESH_TOKEN_EXPIRE_DAYS")

    FIRST_SUPERUSER: str = Field(default="admin@reveals.lu", env="FIRST_SUPERUSER")
    FIRST_SUPERUSER_PASSWORD: str = Field(default="admin", env="FIRST_SUPERUSER_PASSWORD")

    DATABASE_URL: str = Field(default="postgresql://reco_user:changeme@db:5432/reco_db", env="DATABASE_URL")

    REDIS_URL: str = Field(default="redis://redis:6379/0", env="REDIS_URL")

    BACKEND_CORS_ORIGINS: str = Field(default="http://localhost:5173,http://localhost:3000", env="BACKEND_CORS_ORIGINS")

    TITLE: str = Field(default="Réconciliation", env="TITLE")

    # SSO
    SSO_ENCRYPTION_KEY: str = Field(
        default="zoKD58Ed1jRJEHzmTO98A-9vAlFteSbcad9UG58QEaU=",  # Dev key for testing
        env="SSO_ENCRYPTION_KEY",
        description="Fernet key for SSO credential encryption. Generate with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )
    BACKEND_URL: str = Field(default="http://localhost:8000", env="BACKEND_URL")
    FRONTEND_URL: str = Field(default="http://localhost:5173", env="FRONTEND_URL")

    # Reconciliation: inbox folders (file ingestion)
    INBOX_BASE_PATH: str = Field(default="/shared/inbox", env="INBOX_BASE_PATH")
    INBOX_PROCESSED_PATH: str = Field(default="/shared/inbox_processed", env="INBOX_PROCESSED_PATH")
    INBOX_ERROR_PATH: str = Field(default="/shared/inbox_error", env="INBOX_ERROR_PATH")
    INBOX_STAGING_MODE: bool = Field(
        default=True, env="INBOX_STAGING_MODE",
        description="When true, use dataStaging layout: {base}/{provider}/reconciliation/{flow}/",
    )

    # Reconciliation: archive policy
    RECO_ARCHIVE_AFTER_DAYS: int = Field(default=90, env="RECO_ARCHIVE_AFTER_DAYS")

    # Finacle: lower bound (ISO date) for the first-ever extraction of a source
    # (no watermark yet). Served to the ingest_finacle DAG via /tasks/finacle/sources
    # so the date lives in one place — the app .env — regardless of the Airflow env.
    RECO_FINACLE_BACKFILL_SINCE: str = Field(default="", env="RECO_FINACLE_BACKFILL_SINCE")

    # Airflow integration
    AIRFLOW_API_URL: str = Field(default="http://airflow-webserver:8080/api/v1", env="AIRFLOW_API_URL")
    AIRFLOW_API_USER: str = Field(default="airflow", env="AIRFLOW_API_USER")
    AIRFLOW_API_PASSWORD: str = Field(default="airflow", env="AIRFLOW_API_PASSWORD")
    AIRFLOW_USE_EXTERNAL: bool = Field(default=False, env="AIRFLOW_USE_EXTERNAL")

    # Internal token shared between DAGs (tasks endpoints) and backend
    RECO_BACKEND_INTERNAL_TOKEN: str = Field(
        default="change-me-internal-token", env="RECO_BACKEND_INTERNAL_TOKEN"
    )

    # Default role auto-assigned to users created via SSO (must match name in init_db)
    SSO_DEFAULT_ROLE_NAME: str = Field(default="superadmin", env="SSO_DEFAULT_ROLE_NAME")

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> str:
        # Garder en string, le parsing se fera à l'utilisation si nécessaire
        if isinstance(v, list):
            return ",".join(v)
        return v

    def get_cors_origins(self) -> List[str]:
        """Retourne les origines CORS parsées en liste"""
        origins = self.BACKEND_CORS_ORIGINS
        if isinstance(origins, str):
            return [i.strip() for i in origins.split(",")]
        return origins

    class Config:
        case_sensitive = True
        env_file = str(ENV_FILE)
        # The root .env also feeds docker-compose (POSTGRES_*, COMPOSE_*, ...):
        # ignore the keys that are not app settings instead of crashing.
        extra = "ignore"

settings = Settings()
