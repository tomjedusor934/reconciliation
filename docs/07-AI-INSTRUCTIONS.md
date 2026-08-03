# Instructions for AI/LLM Development

Use this guide to help an AI assistant understand the project without needing the full context every time.

## Quick Context Summary

**Project**: Payment Reconciliation App for Post Luxembourg
- 5 flows (ATM priority, IP, Other Payments, Webripost, Float IP)
- Ingest → Auto-reconcile → Émargement workflow
- High-volume DB (two-table design: live + émargement)
- Full audit trail

**Tech**: FastAPI + Vue 3 + PostgreSQL 16 + Airflow + Redis

**Key Repo Files**:
- `README.md` — Quick start (2 min read)
- `docs/00-PROJECT-OVERVIEW.md` — Architecture (5 min read)
- `docs/01-DOCKER-COMPOSE.md` — Docker setup (10 min read)
- `docs/02-BACKEND.md` — FastAPI structure (15 min read)
- `docs/03-FRONTEND.md` — Vue 3 structure (15 min read)
- `docs/04-AIRFLOW.md` — DAGs & scheduling (10 min read)
- `docs/05-PARSERS.md` — File parsers (10 min read)
- `docs/06-DATABASE.md` — Schema & SQL (15 min read)

---

## Understanding the Code

### When Modifying Backend
**Always read**: `docs/02-BACKEND.md` (Architecture Rules section)

**Key rules**:
1. ✅ Endpoint → Service → Repository → Model (strict layers)
2. ✅ Services never raise HTTPException (endpoints translate)
3. ✅ Singletons at module bottom (`service = FlowService()`)
4. ✅ Plural kebab-case routes (`/flows`, `/reconciliation-entries`)
5. ✅ Re-export models in `models/__init__.py`

**File naming**:
- Model: `backend/app/models/{entity}.py` (singular)
- Schema: `backend/app/schemas/{entity}.py` (singular)
- Repository: `backend/app/repositories/{entity}_repository.py` (singular)
- Service: `backend/app/services/{entity}_service.py` (singular)
- Endpoint: `backend/app/api/v1/endpoints/{entities}.py` (PLURAL)

**Example flow**:
```
POST /flows → flows.py (endpoint) 
  → flow_service.create() (service)
    → flow_repository.create() (repository)
      → Flow model (SQLAlchemy ORM)
        → INSERT into reco.flow (SQL)
```

### When Modifying Frontend
**Always read**: `docs/03-FRONTEND.md` (Frontend Rules section)

**Key rules**:
1. ✅ `<script setup lang="ts">` ALWAYS (no Options API)
2. ✅ Type all props: `defineProps<Props>()`
3. ✅ Use `ref()` not `reactive()` for primitives
4. ✅ Reuse UI components (Table, Modal, Input, Button, etc.)
5. ✅ Use services, not direct fetch

**File structure**:
```
frontend/src/
├── services/{entity}Service.ts (literal object, one per entity)
├── types/index.ts (ALL interfaces, centralized)
├── views/{entities}/{Entity}List.vue (plural folder, PascalCase file)
└── components/ui/{Component}.vue (reusable UI lib)
```

**Example flow**:
```
Component mounted → flowService.getAll()
  → API call via axios
    → Backend endpoint
      → Service → Repository → Model
        → Database
    → Response JSON
  → Component state updated
  → Template re-renders
```

### When Modifying Database
**Always read**: `docs/06-DATABASE.md`

**Key concepts**:
1. `reco` schema — reconciliation tables
2. `audit` schema — audit logs (triggers + UI actions)
3. `reconciliation_entry` has a simple PK (`id`, BigInteger autoincrement)
4. Source hash (`sha256`) prevents duplicate ingestion
5. Status: pending → matched/forced/excluded
6. Two-table design: `reconciliation_entry` (live, pending only) + `reconciliation_entry_emargement` (validated entries)

**No special query rules** — `reconciliation_entry` uses a simple PK (`id`), no partition key needed in queries.

### When Modifying Airflow
**Always read**: `docs/04-AIRFLOW.md`

**Key concepts**:
1. DAGs in `shared/dags/` (5 ingest + 2 system)
2. All call backend `/tasks/*` endpoints
3. Authentication: X-Internal-Token header
4. Schedules: ATM every 15 min, reconcile at 02:00 UTC, émargement sweep at 03:30 UTC

**File naming**:
- `ingest_{flow_code}.py` — ingest DAG
- `reconcile_daily.py` — auto engine
- `archive_matched.py` — émargement sweep (safety net)

### When Adding Parsers
**Always read**: `docs/05-PARSERS.md`

**Step-by-step**:
1. Extend `BaseParser` abstract class
2. Implement `parse(file_path: str) -> list[ParsedEntry]`
3. Return normalized `ParsedEntry` objects
4. Register in `get_parser()` factory function

**Example**:
```python
class NewParser(BaseParser):
    def parse(self, file_path: str) -> list[ParsedEntry]:
        # 1. Open file
        # 2. Parse rows
        # 3. Validate
        # 4. Return [ParsedEntry(...), ...]
```

---

## Common Tasks

### Add a New Flow Type

1. **Backend**:
   - Seed in `backend/app/db/seed_flows.py`
   - Configure parser_type, parser_config, accounts
   - Set is_active=False initially

2. **Frontend**:
   - FlowList/FlowForm already support all fields
   - No changes needed (generic)

3. **Airflow**:
   - Create `shared/dags/ingest_{flow_code}.py`
   - Set is_paused=True initially
   - Activate when ready

4. **Parser**:
   - Implement if custom format (e.g., MT940, Cobol)
   - Register in `get_parser()` factory

### Add a New Reconciliation Action

1. **Backend endpoint** (`endpoints/reconciliation_entries.py` or new):
   ```python
   @router.post("/reconciliation-entries/custom-action")
   async def custom_action(...):
       service.custom_action(...)  # Call service
   ```

2. **Service method** (`services/reconciliation_service.py`):
   ```python
   def custom_action(db, ...):
       # Business logic
       # No HTTPException!
   ```

3. **Frontend** (e.g., new button/modal):
   ```vue
   <Button @click="submit">Custom action</Button>
   
   const submit = async () => {
       try {
           await reconciliationService.customAction(...)
       } catch (e) { toaster.error(...) }
   }
   ```

### Debugging Data Issues

**Q: "Why aren't my entries matching?"**
- Check: `SELECT * FROM reco.reconciliation_entry WHERE reco_id = '...' AND status = 'pending'`
- Check: `SUM(amount)` — must be exactly 0
- Check: `flow_id`, `currency` — must be same

**Q: "Why are entries duplicated?"**
- Check: `source_hash` — uniqueness constraint
- Check: `IngestionRun` — was file ingested before?

**Q: "Audit not logging user?"**
- Check: `AuditUserMiddleware` is wired — it extracts `user_id` from JWT cookie
- `get_db()` executes `SET LOCAL app.current_user_id` on each DB session
- `audit_log.user_id` reads from `current_setting('app.current_user_id')`

---

## Running Tests & Validation

**No pytest/vitest yet** (awaiting real data samples)

**Manual validation**:
```bash
# 1. Start stack
docker-compose up -d

# 2. Check backend health
curl http://localhost:8000/docs

# 3. Check DB
docker exec -it reconciliation-db psql -U reconciliation_user -d reconciliation_db -c \
  "SELECT * FROM reco.flow;"

# 4. Check Airflow (optional)
docker-compose --profile airflow up -d
# Then http://localhost:8080

# 5. Test ingest (manual)
# Drop file in shared/inbox/atm/
# Wait 15 min or trigger manually
# Check: reco.ingestion_run, reco.reconciliation_entry

# 6. Test reconcile (manual)
# Wait for 02:00 UTC or trigger manually
# Check: reco.match_group, reconciliation_entry.status
```

---

## Environment & Configuration

**Key .env variables**:
```bash
# Database
POSTGRES_PASSWORD=...  # Change in production!

# Airflow (optional)
AIRFLOW_USE_EXTERNAL=false  # true if using corp Airflow

# Auth
SECRET_KEY=...  # Change in production!
ALGORITHM=HS256

# Reconciliation
RECO_ARCHIVE_AFTER_DAYS=90  # Legacy — no longer used (émargement is immediate)
```

**Startup checklist**:
- [ ] `.env` created from `.env.example`
- [ ] `shared/inbox/` directories created
- [ ] `docker-compose up -d` runs without errors
- [ ] Backend logs show "Application startup complete"
- [ ] Frontend loads at http://localhost:5173
- [ ] Can login with auto-created superadmin

---

## Code Patterns

### Backend Service Pattern

```python
class FlowService:
    @staticmethod
    def create(db: Session, flow_create: FlowCreate) -> Flow:
        # Business logic here (validation, transforms, etc.)
        if flow_create.code in [f.code for f in flow_repository.all(db)]:
            raise ValueError("Code already exists")
        
        flow = Flow(**flow_create.dict())
        db.add(flow)
        db.commit()
        db.refresh(flow)
        return flow
    
    # ... other methods

flow_service = FlowService()  # SINGLETON at bottom
```

### Backend Endpoint Pattern

```python
@router.post("/")
async def create_flow(
    flow_create: FlowCreate,
    db: Session = Depends(get_db),
):
    try:
        flow = flow_service.create(db, flow_create)
        return FlowResponse.from_orm(flow)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal error")
```

### Frontend Component Pattern

```vue
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import flowService from '@/services/flowService'
import toaster from '@/utils/toaster'
import type { Flow } from '@/types'

interface Props { /* ... */ }
const props = defineProps<Props>()

const emit = defineEmits<{ (e: 'saved'): void }>()

const loading = ref(true)
const items = ref<Flow[]>([])

const fetchData = async () => {
  loading.value = true
  try {
    const { data } = await flowService.getAll()
    items.value = data
  } catch (e) {
    toaster.error('Failed to load')
  } finally {
    loading.value = false
  }
}

onMounted(fetchData)
</script>

<template>
  <div v-if="loading"><Loader /></div>
  <Table v-else :items="items" />
</template>
```

---

## Contributing Tips

1. **Before coding**: Read relevant docs (see "When Modifying X" sections above)
2. **Ask questions**: If you're unsure about pattern/architecture, ask
3. **One change per PR**: Keep PRs focused
4. **Test manually**: No automated tests yet, use Docker stack
5. **Follow conventions**: Naming, file structure, code patterns (see above)
6. **Document changes**: Update relevant docs if architecture changes

---

## Troubleshooting Quick Guide

| Problem | Checklist |
|---------|-----------|
| Backend won't start | Check DATABASE_URL, db is running, migrations applied |
| Frontend API 404 | Check VITE_API_URL, backend is running on :8000 |
| Entries not matching | Check reco_id, currency, flow_id, amount sum=0 |
| Duplicate entries | Check source_hash uniqueness, IngestionRun status |
| Audit not logging user | AuditUserMiddleware must be wired (extracts user_id from JWT) |
| Airflow DAG not triggering | Check schedule, is_paused, airflow scheduler running |
| File not ingesting | Check inbox folder permissions, file format, parser config |

---

## Links to Key Files (Relative to Repo Root)

**Configuration**:
- `.env.example`
- `docker-compose.yml`

**Backend**:
- `backend/app/main.py` — startup
- `backend/app/core/config.py` — settings
- `backend/app/db/init_*.py` — initialization
- `backend/app/models/` — ORM models
- `backend/app/services/` — business logic
- `backend/app/api/v1/endpoints/` — REST endpoints

**Frontend**:
- `frontend/src/main.ts` — bootstrap
- `frontend/src/router/index.ts` — routes
- `frontend/src/stores/auth.ts` — authentication
- `frontend/src/services/` — API client
- `frontend/src/types/index.ts` — TypeScript interfaces
- `frontend/src/views/` — page components
- `frontend/src/components/ui/` — UI component library

**Airflow**:
- `shared/dags/common.py` — backend client utilities
- `shared/dags/dag_factory.py` — generic ingest DAG builder
- `shared/dags/ingest_*.py` — specific ingest DAGs
- `shared/dags/reconcile_daily.py`, `archive_matched.py`

**Database**:
- `backend/app/db/base.py` — SQLAlchemy declarative base
- `backend/app/db/session.py` — connection & SessionLocal
- `backend/app/db/init_reco.py` — schemas, triggers
- `backend/app/db/seed_flows.py` — flow seeding
- `backend/app/repositories/` — CRUD & queries

**Parsers**:
- `backend/app/services/parsers/base_parser.py` — abstract interface
- `backend/app/services/parsers/*_parser.py` — implementations

---

## When You Get Stuck

1. **Check the docs** — start with relevant section in docs/
2. **Read the code** — look at similar implementations in the codebase
3. **Grep for patterns** — `grep -r "def.*match" backend/app/` to see matching logic
4. **Check git history** — `git log --oneline -- path/to/file.py` to understand changes
5. **Write a small test** — create minimal example to understand behavior
6. **Ask for help** — include relevant code snippets and error messages

---

## Final Notes

- **This is production-ready scaffolding**, not a finished app
- **Tests are TODO** — awaiting real data samples from Post Finance
- **MT940 parser is stub** — BCEE format needs sample file
- **Finacle integration is template** — needs real ODS schema + credentials
- **Audit user_id is wired** — `AuditUserMiddleware` extracts user_id from JWT cookie and `get_db()` executes `SET LOCAL app.current_user_id` on each DB session

The architecture is **solid and extensible**. You should be able to add new flows, parsers, endpoints, and views following the patterns described without much extra work.

Good luck! 🚀
