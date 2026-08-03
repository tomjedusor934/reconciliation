# Backend Architecture Deep Dive

## Overview
FastAPI application following strict layering: **Endpoint → Service → Repository → Model**.

## Directory Structure

```
backend/
├── app/
│   ├── main.py                      # Entry point, startup hooks
│   ├── core/
│   │   ├── config.py                # Settings from env vars (Pydantic)
│   │   ├── security.py              # JWT encoding, password hashing
│   │   ├── team_filter.py           # RBAC filtering (from Orchestro)
│   │   └── middleware.py            # CSRF, auth, logging
│   ├── db/
│   │   ├── base.py                  # SQLAlchemy declarative_base()
│   │   ├── session.py               # create_engine, SessionLocal
│   │   ├── init_db.py               # Seed superadmin role, SSO config
│   │   ├── init_reco.py             # Create schemas, triggers
│   │   └── seed_flows.py            # Seed 5 flows + accounts
│   ├── models/                      # SQLAlchemy ORM models
│   │   ├── __init__.py              # ⚠️ MUST re-export all models
│   │   ├── flow.py                  # Flow, FlowAccount, enums
│   │   ├── source_connection.py
│   │   ├── reconciliation_entry.py  # Main table (live + émargement)
│   │   ├── match_group.py
│   │   ├── exclusion.py
│   │   ├── ingestion_run.py
│   │   ├── reconciliation_run.py
│   │   └── audit_log.py
│   ├── schemas/                     # Pydantic response models
│   │   ├── flow.py
│   │   ├── reconciliation.py        # All reco schemas
│   │   └── ...
│   ├── repositories/                # CRUD + complex queries
│   │   ├── flow_repository.py
│   │   ├── reconciliation_entry_repository.py
│   │   ├── match_group_repository.py
│   │   └── ...
│   ├── services/                    # Business logic
│   │   ├── flow_service.py
│   │   ├── reconciliation_service.py
│   │   ├── emargement_service.py
│   │   ├── dashboard_service.py
│   │   ├── airflow_client_service.py
│   │   └── parsers/
│   │       ├── base_parser.py
│   │       ├── cobol_mosel_parser.py
│   │       ├── csv_parser.py
│   │       ├── xml_parser.py
│   │       ├── mt940_parser.py
│   │       └── finacle_db_extractor.py
│   ├── api/v1/
│   │   ├── api.py                   # Router registration
│   │   ├── deps.py                  # Dependencies (auth, DB, etc.)
│   │   └── endpoints/
│   │       ├── flows.py
│   │       ├── reconciliation_entries.py
│   │       ├── match_groups.py
│   │       ├── ingestion_runs.py
│   │       ├── reconciliation_runs.py
│   │       ├── dashboards.py
│   │       ├── audit.py
│   │       └── tasks.py
│   ├── tasks/                       # Async task definitions (if Celery used)
│   └── utils/                       # Helpers
├── requirements.txt
├── Dockerfile
└── alembic/                         # DB migrations (if Alembic used)
```

## Key Files Explained

### 1. `main.py` — Entry Point

```python
# Startup sequence:
1. Create engine + SessionLocal
2. Create tables (Base.metadata.create_all)
3. Pre-create reco/audit schemas (before create_all, else table init fails)
4. Run init_db() — seed superadmin role, apply SSO defaults
5. Run init_reco_db() — create audit triggers
6. Run seed_flows() — create 5 flows + accounts
7. Initialize FastAPI app + register routers
```

**Important**: `init_reco_db()` must run **after** `create_all()` because it creates triggers and schemas via raw SQL.

### 2. `core/config.py` — Settings

Uses Pydantic Settings pattern. Reads from `.env`:

```python
class Settings(BaseSettings):
    # Database
    DATABASE_URL: str = "postgresql://..."
    
    # Inbox & paths
    INBOX_BASE_PATH: str = "/shared/inbox"
    
    # Airflow
    AIRFLOW_USE_EXTERNAL: bool = False
    AIRFLOW_API_URL: str = "http://airflow-webserver:8080/api/v1"
    AIRFLOW_USER: str = "airflow"
    AIRFLOW_PASSWORD: str = "airflow"
    
    # Auth
    SECRET_KEY: str = "change-in-production"
    ALGORITHM: str = "HS256"
    
    # SSO
    SSO_DEFAULT_ROLE_NAME: str = "superadmin"
    
    class Config:
        env_file = ".env"
        case_sensitive = True
```

**Accessed via**: `from app.core.config import settings`

### 3. `db/session.py` — Database Connection

```python
engine = create_engine(
    settings.DATABASE_URL,
    connect_args={"check_same_thread": False},  # SQLite only
    pool_pre_ping=True,  # Verify connection before query
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

Used in dependencies: `get_db: Session = Depends(get_db)`

### 4. `db/init_reco.py` — Schemas & Triggers

Creates:
1. `reco` schema
2. `audit` schema + trigger function `audit.fn_audit_row()`
3. Per-table audit triggers (except `reconciliation_entry` — too high volume)

**Tables** (created by SQLAlchemy `create_all()`):
```sql
-- reconciliation_entry: live table for pending entries
CREATE TABLE reco.reconciliation_entry (
    id BIGSERIAL PRIMARY KEY,
    value_date DATE,
    ...
);

-- reconciliation_entry_emargement: entries move here after validation
CREATE TABLE reco.reconciliation_entry_emargement (
    ...  -- same columns as reconciliation_entry
    emarged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 5. `db/seed_flows.py` — Idempotent Flow Seeding

```python
SEED_FLOWS = [
    {
        "code": "atm",
        "name": "ATM",
        "is_active": True,
        "parser_type": ParserType.COBOL_MOSEL,
        "parser_config": {
            "encoding": "utf-8",
            "allowed_event_types": ["ARACCMVT", "ARCLHSAN", ...],
        },
        "accounts": [
            ("0010110035001", "ATM DEPOSIT"),
            ("0010110040001", "ATM WITHDRAWAL"),
        ],
    },
    # ... 4 more flows (inactive)
]

def seed_flows(db: Session):
    for spec in SEED_FLOWS:
        existing = flow_repository.get_by_code(db, code=spec["code"])
        if existing:
            # Add missing accounts only
            continue
        # Create new flow
```

**Idempotent**: Can run multiple times; only creates missing flows.

### 6. `models/reconciliation_entry.py` — High-Volume Table

```python
class ReconciliationEntry(Base):
    __tablename__ = "reconciliation_entry"
    __table_args__ = (
        # Simple PK on id
        # BRIN index on value_date (append-only)
        Index("ix_reco_entry_value_date", "value_date", postgresql_using="brin"),
        # B-tree on common filters
        Index("ix_reco_entry_flow_status", "flow_id", "status"),
        Index("ix_reco_entry_reco_id", "reco_id"),
        # Unique source_hash (table-wide)
        UniqueConstraint("source_hash", name="uq_source_hash"),
    )
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    flow_id = Column(Integer, ForeignKey("flow.id"), nullable=False)
    reco_id = Column(String, nullable=True)
    account = Column(String, nullable=True)
    amount = Column(Numeric(15, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    value_date = Column(Date, nullable=False)
    status = Column(String, default="pending")
    match_group_id = Column(Integer, nullable=True)
    # ... more fields
```

**Two-table design**: `ReconciliationEntry` is the live table; `ReconciliationEntryEmargement` has the same schema plus an `emarged_at` column. Entries move to émargement once reconciliation is done.

### 7. `repositories/reconciliation_entry_repository.py` — Repository Pattern

```python
class ReconciliationEntryRepository:
    @staticmethod
    def bulk_insert(db: Session, entries: list[ParsedEntry]) -> tuple[int, int, int, int]:
        """Insert entries, skip duplicates by source_hash. Returns (in, ok, ko, dup)."""
        stmt = insert(ReconciliationEntry).values([...])
        stmt = stmt.on_conflict_do_nothing(index_elements=["source_hash"])
        # ...

    @staticmethod
    def find_balanced_groups(db: Session) -> list[dict]:
        """Group pending entries by (flow_id, reco_id, currency), return groups with sum=0."""
        stmt = select(...).where(status='pending').group_by(...).having(sum(amount) == 0)
        # ...

    @staticmethod
    def mark_matched(db: Session, entry_ids: list[int], match_group_id: int):
        """Update entries to matched."""
        # ...

    @staticmethod
    def get_one(db: Session, entry_id: int):
        """Fetch entry by id."""
        # ...
```

**Key methods used by services**.

### 8. `services/reconciliation_service.py` — Auto Engine & Manual Actions

```python
class ReconciliationService:
    @staticmethod
    def run_auto(db: Session) -> dict:
        """Auto-match groups with sum=0."""
        # 1. Find balanced groups
        # 2. Create match_group(mode='auto')
        # 3. Update entries to 'matched'
        # 4. Log reconciliation_run

    @staticmethod
    def force_match(db: Session, entry_ids: list[int], ...) -> dict:
        """User forces a match."""
        # 1. Validate: same flow, same currency
        # 2. Validate: sum=0 OR comment provided
        # 3. Create match_group(mode='forced')
        # 4. Update entries to 'matched'
        # 5. Log UI action

    @staticmethod
    def exclude(db: Session, entry_id: int, reason: str):
        """User excludes an entry."""
        # 1. Validate: entry exists, status='pending'
        # 2. Update entry to 'excluded'
        # 3. Insert exclusion row
        # 4. Log UI action
```

**Services are the **business logic** — never raise HTTPException.**

### 9. `services/parsers/` — Pluggable Parsers

**Base class**:
```python
class ParsedEntry:
    reco_id: str | None
    amount: Decimal
    currency: str
    value_date: date
    # ... more fields

    def compute_source_hash(self) -> str:
        """SHA256(flow_id|external_ref|reco_id|amount|value_date|account)."""

class BaseParser(ABC):
    @abstractmethod
    def parse(self, file_path: str) -> list[ParsedEntry]:
        pass
```

**Implementations**:
- `CobolMoselParser` — ATM files (fixed-width Cobol/MOSEL format)
- `CSVParser` — Generic CSV (configurable column mapping)
- `XMLParser` — Generic XML (XPath-based)
- `MT940Parser` — BCEE format (stub, awaiting sample)
- `FinacleDBExtractor` — Direct DB extraction (SQLAlchemy-based)

**Used by**:
```python
def get_parser(parser_type: ParserType, parser_config: dict) -> BaseParser:
    return {
        ParserType.COBOL_MOSEL: CobolMoselParser(parser_config),
        ParserType.CSV: CSVParser(parser_config),
        # ...
    }[parser_type]
```

### 10. `api/v1/endpoints/` — REST Endpoints

All endpoints follow the pattern:

```python
@router.get("/")
async def list_items(
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
):
    try:
        items = item_service.list(db, skip, limit)
        return {"data": items}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal error")
```

**Key rule**: Endpoints **translate exceptions** from services. Services don't raise HTTPException.

### 11. `api/v1/endpoints/tasks.py` — Internal Airflow Endpoints

```python
@router.post("/tasks/ingest/{flow_code}")
async def task_ingest(
    flow_code: str,
    db: Session = Depends(get_db),
    token: str = Depends(verify_internal_token),  # X-Internal-Token header
):
    """Called by Airflow DAG. Polls inbox, parses, inserts."""

@router.post("/tasks/reconcile")
async def task_reconcile(
    db: Session = Depends(get_db),
    token: str = Depends(verify_internal_token),
):
    """Called by reconcile_daily DAG. Runs auto engine."""

@router.post("/tasks/emargement")
async def task_emargement(
    db: Session = Depends(get_db),
    token: str = Depends(verify_internal_token),
):
    """Called by archive_matched DAG. Sweeps non-pending entries to émargement table."""
```

**Auth**: X-Internal-Token header must match `RECO_BACKEND_INTERNAL_TOKEN` env var. CSRF middleware exempts `/tasks/`.

---

## Architecture Rules (Enforced)

1. **Endpoint → Service → Repository → Model**
   - Endpoints: validate input, call service, translate errors
   - Services: business logic, no HTTPException, call repositories
   - Repositories: CRUD + queries, call models
   - Models: ORM only, no logic

2. **Services never raise HTTPException**
   - Raise ValueError or custom exceptions
   - Endpoints catch and translate to HTTP status

3. **Singletons at module bottom**
   ```python
   # At end of service.py:
   flow_service = FlowService()
   
   # Usage:
   from app.services.flow_service import flow_service
   ```

4. **Plural kebab-case routes**
   ```python
   router = APIRouter(prefix="/flows", tags=["flows"])
   router = APIRouter(prefix="/reconciliation-entries", tags=["reconciliation"])
   router = APIRouter(prefix="/match-groups", tags=["reconciliation"])
   ```

5. **Re-export models in `__init__.py`**
   ```python
   # models/__init__.py
   from app.models.flow import Flow, FlowAccount
   from app.models.reconciliation_entry import ReconciliationEntry
   # ...
   ```
   Used by `from app.models import Flow`, etc.

---

## Common Workflows in Backend

### Parsing & Bulk Ingestion
```python
# 1. Get parser from config
parser = get_parser(flow.parser_type, flow.parser_config)

# 2. Parse file
parsed_entries = parser.parse(file_path)

# 3. Compute hashes
for entry in parsed_entries:
    entry.source_hash = entry.compute_source_hash()

# 4. Bulk insert with dedup
in_, ok, ko, dup = reconciliation_entry_repository.bulk_insert(db, parsed_entries)
```

### Auto Reconciliation
```python
# 1. Find balanced groups
groups = reconciliation_entry_repository.find_balanced_groups(db)

# 2. For each group, create match_group
for group in groups:
    mg = reconciliation_service.create_match_group(db, group["entry_ids"], mode="auto")

# 3. Update entries
reconciliation_entry_repository.mark_matched(db, group["entry_ids"], mg.id)
```

### Force Match (User Action)
```python
# 1. Validate entries (same flow, same currency, ≥2 entries)
total = sum(e.amount for e in entries)
if total != 0 and not comment:
    raise ValueError("Unbalanced match requires comment")

# 2. Create match_group
mg = reconciliation_service.create_match_group(db, entry_ids, mode="forced", comment=comment)

# 3. Update entries
reconciliation_entry_repository.mark_matched(db, entry_ids, mg.id)

# 4. Log UI action
ui_action_log_repository.create(db, user_id, "force_match", ...)
```

---

## Performance Considerations

1. **Bulk inserts** — use `ON CONFLICT DO NOTHING` to skip duplicates
2. **Indexes** — BRIN on `value_date`, B-tree on `(flow_id, status)`, `reco_id`
3. **Audit** — disabled for `reconciliation_entry` (would explode on millions of rows)
4. **Connections** — pool size set in engine; connection pooling handled by SQLAlchemy

---

## Testing Notes

- **No pytest yet** — awaiting real data samples
- **Syntax validation**: `python -m py_compile backend/app/**/*.py`
- **Manual tests**: Use Swagger UI (`/docs`) or curl
