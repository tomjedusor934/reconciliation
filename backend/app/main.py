from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.orm import configure_mappers

from app.api.v1.api import api_router
from app.core.config import settings
from app.core.middleware import AuditUserMiddleware, CSRFMiddleware, RateLimitMiddleware, SecurityHeadersMiddleware
from app.db.base import Base
from app.db.init_db import init_db
from app.db.init_reco import init_reco_db
from app.db.seed_flows import seed_flows
from app.db.session import SessionLocal, engine
from app.services.airflow_sync_service import airflow_sync_service

# Initialize versioning
configure_mappers()

# Ensure custom schemas exist BEFORE create_all (for tables with schema=reco / audit)
with engine.begin() as conn:
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS reco"))
    conn.execute(text("CREATE SCHEMA IF NOT EXISTS audit"))

# Create tables (for simplicity in this example, usually done via Alembic)
Base.metadata.create_all(bind=engine)

# Initialize DB with superuser + reconciliation infra (audit triggers, ...)
db = SessionLocal()
try:
    init_db(db)
    init_reco_db(db)
    seed_flows(db)
finally:
    db.close()

# Sync DAGs into external Airflow (non-blocking: logs warnings on failure)
import threading
threading.Thread(target=airflow_sync_service.sync, daemon=True).start()

app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url=f"{settings.API_V1_STR}/openapi.json" if settings.ENVIRONMENT == "dev" else None,
    docs_url="/docs" if settings.ENVIRONMENT == "dev" else None,
    redoc_url="/redoc" if settings.ENVIRONMENT == "dev" else None,

)

app.add_middleware(SecurityHeadersMiddleware)

# Audit user ID extraction (stores user_id in request.state for DB audit triggers)
app.add_middleware(AuditUserMiddleware)

# Rate Limiting Middleware
app.add_middleware(RateLimitMiddleware)

# CSRF Middleware
app.add_middleware(CSRFMiddleware)

# Set all CORS enabled origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
)

app.include_router(api_router, prefix=settings.API_V1_STR)

@app.get("/")
def root():
    return {"message": f"Welcome to {settings.PROJECT_NAME} API!"}
