# Airflow & Reconciliation Workflows

## Architecture Overview

Airflow orchestrates the **5 ingestion flows** and the **daily reconciliation engine**:

```
┌─────────────────────────────────────────────────────────────────┐
│  Airflow DAGs (in shared/dags/)                                 │
├──────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ├─ ingest_atm.py ───────────► (every 15 min) ◄── ATM files     │
│  ├─ ingest_ip.py ────────────► (paused, every 30 min)           │
│  ├─ ingest_other_payments.py ► (paused, every 30 min)           │
│  ├─ ingest_webripost.py ─────► (paused, every 30 min)           │
│  ├─ ingest_float_ip.py ──────► (paused, @daily)                 │
│  │                                                                │
│  ├─ reconcile_daily.py ───────► (02:00 UTC) ◄── Auto engine     │
│  └─ archive_matched.py ──────► (03:30 UTC) ◄── Émargement sweep│
│                                                                   │
└──────────────┬──────────────────────────────────────────────────┘
               │
               │ All call backend `/tasks/*` endpoints
               │ with X-Internal-Token header
               │
       ┌───────▼─────────┐
       │  Backend API    │
       │  (FastAPI)      │
       │                 │
       │  ├─ /tasks/ingest/{flow}
       │  ├─ /tasks/reconcile
       │  └─ /tasks/emargement
       │
       └───────┬─────────┘
               │
        ┌──────▼──────────┐
        │  PostgreSQL     │
        │                 │
        │  reco.reconciliation_entry
        │  reco.match_group
        │  reco.exclusion
        │  reco.ingestion_run
        │  reco.reconciliation_run
        │
        └─────────────────┘
```

## Airflow DAG Structure

### 1. Generic Ingest DAG Factory — `shared/dags/dag_factory.py`

```python
def build_ingestion_dag(
    flow_code: str,
    schedule: str,
    finacle: bool = False,
    is_paused: bool = False,
    description: str = "",
) -> DAG:
    """
    Build a DAG that:
    1. Polls backend for ingest task (via common.py)
    2. If finacle=True, also extracts from Finacle DB
    3. Returns on success or failure
    """
    
    with DAG(
        dag_id=f"ingest_{flow_code}",
        schedule_interval=schedule,
        start_date=datetime(2025, 1, 1),
        catchup=False,
        max_active_runs=1,
        is_paused_upon_creation=is_paused,
        tags=["reconciliation", "ingest"],
    ) as dag:
        # Task: Call backend /tasks/ingest/{flow_code}
        ingest = PythonOperator(
            task_id="ingest",
            python_callable=call_backend,
            op_kwargs={
                "method": "post",
                "endpoint": f"/tasks/ingest/{flow_code}",
            },
        )
        
        # Task: If finacle, extract from DB
        if finacle:
            finacle_extract = PythonOperator(
                task_id="ingest_finacle",
                python_callable=call_backend,
                op_kwargs={
                    "method": "post",
                    "endpoint": f"/tasks/ingest-finacle/{flow_code}",
                },
            )
            ingest >> finacle_extract
    
    return dag
```

**Used by**:
```python
# ingest_atm.py
dag = build_ingestion_dag(
    flow_code="atm",
    schedule="*/15 * * * *",  # Every 15 minutes
    finacle=False,
    is_paused=False,
)

# ingest_ip.py
dag = build_ingestion_dag(
    flow_code="ip",
    schedule="*/30 * * * *",
    finacle=True,
    is_paused=True,  # Will be activated later
)
```

### 2. Backend Client — `shared/dags/common.py`

Utilities to call backend endpoints from Airflow:

```python
def call_backend(method: str, endpoint: str, data: dict | None = None, **kwargs):
    """
    Call backend /tasks/* endpoint with X-Internal-Token.
    Returns response JSON on success, raises on failure.
    """
    import httpx
    
    url = f"{AIRFLOW_BACKEND_URL}{endpoint}"
    headers = {
        "X-Internal-Token": RECO_BACKEND_INTERNAL_TOKEN,
        "Content-Type": "application/json",
    }
    
    with httpx.Client() as client:
        if method == "post":
            resp = client.post(url, json=data or {}, headers=headers)
        elif method == "get":
            resp = client.get(url, headers=headers)
        
        resp.raise_for_status()
        return resp.json()

def task_ingest(flow_code: str, **kwargs):
    """Wrapper for PythonOperator."""
    return call_backend("post", f"/tasks/ingest/{flow_code}")

def task_reconcile(**kwargs):
    """Called by reconcile_daily DAG."""
    return call_backend("post", "/tasks/reconcile")

def task_emargement(**kwargs):
    """Called by archive_matched DAG."""
    return call_backend("post", "/tasks/emargement")
```

### 3. ATM Ingest DAG — `shared/dags/ingest_atm.py`

```python
from dag_factory import build_ingestion_dag

dag = build_ingestion_dag(
    flow_code="atm",
    schedule="*/15 * * * *",          # Every 15 minutes (priority flow)
    finacle=False,
    is_paused=False,
    description="ATM cash withdrawals & deposits (Cobol/MOSEL files)",
)
```

**Triggers every 15 minutes**:
- Backend's `GET /tasks/ingest/atm` checks `shared/inbox/atm/` for new files
- Parses, validates, bulk inserts
- Moves files to `shared/inbox_processed/atm/` or `shared/inbox_error/atm/`
- Returns row counts via `IngestionRun` model

### 4. Daily Reconciliation DAG — `shared/dags/reconcile_daily.py`

```python
from common import task_reconcile

with DAG(
    dag_id="reconcile_daily",
    description="Run automatic reconciliation engine (group by flow+reco_id+currency, sum=0)",
    schedule_interval="0 2 * * *",    # 02:00 UTC daily
    start_date=datetime(2025, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["reconciliation", "engine"],
) as dag:
    PythonOperator(
        task_id="run_auto_reconciliation",
        python_callable=task_reconcile,
    )
```

**At 02:00 UTC each day**:
1. Backend groups pending entries by `(flow_id, reco_id, currency)`
2. For each group where `SUM(amount) = 0`:
   - Create `match_group(mode='auto')`
   - Flip entries to `status='matched'`
3. Record `reconciliation_run` with counters

### 5. Émargement Sweep DAG — `shared/dags/archive_matched.py`

```python
with DAG(
    dag_id="archive_matched",
    description="Sweep non-pending entries from live table to émargement (safety net)",
    schedule_interval="30 3 * * *",   # 03:30 UTC daily (after reconcile)
    ...
) as dag:
    PythonOperator(task_id="sweep_to_emargement", python_callable=task_emargement)
```

**At 03:30 UTC each day** (after reconciliation):
1. Find any non-pending entries still in `reconciliation_entry` (live table)
2. Move to `reconciliation_entry_emargement` with `emarged_at` timestamp
3. Delete from live table

This is a **safety net** — normally entries are moved to émargement inline right after matching, forcing, or excluding.

---

## Backend `/tasks/*` Endpoints

All endpoints in `backend/app/api/v1/endpoints/tasks.py`:

### `POST /tasks/ingest/{flow_code}`

**Called by**: `ingest_*` DAGs (every 15 min for ATM, 30 min for others)

**Process**:
1. Get flow config by code
2. List all files in `inbox/{flow.inbox_subfolder}/`
3. For each file:
   - Parse (using configured parser)
   - Validate (required fields, type conversions)
   - Compute `source_hash`
   - Bulk insert with `ON CONFLICT DO NOTHING` (skip duplicates)
   - Move to `inbox_processed/` on success
   - Move to `inbox_error/` on failure
4. Return `IngestionRun` with counters:
   ```json
   {
     "rows_in": 1000,
     "rows_ok": 998,
     "rows_ko": 2,
     "rows_duplicate": 0,
     "status": "success"
   }
   ```

**Error handling**:
- Parse errors → file moved to `inbox_error/`, IngestionRun status='failed'
- Partial success → status='partial', some rows in DB, some errors

### `POST /tasks/ingest-finacle/{flow_code}`

**Called by**: `ingest_*` DAGs with `finacle=True` (after file-based ingest)

**Process**:
1. Get flow's Finacle connection config
2. Extract from Finacle ODS (via SQL query from `source_connection`)
3. Transform to `ParsedEntry` format
4. Bulk insert (same dedup logic)
5. Return IngestionRun

**Used for**: IP, Other Payments, Float IP (hybrid or DB-only sources)

### `POST /tasks/reconcile`

**Called by**: `reconcile_daily` DAG at 02:00 UTC

**Process**:
1. Find all pending entries
2. Group by `(flow_id, reco_id, currency)`
3. For each group:
   - Calculate `SUM(amount)`
   - If sum = 0:
     - Create `match_group(mode='auto', sum=0)`
     - Update all entries: `status='matched', match_group_id=<id>`
4. Create `reconciliation_run` record with counters
5. Return:
   ```json
   {
     "entries_scanned": 50000,
     "groups_created": 2450,
     "entries_matched": 48900,
     "duration_ms": 5230
   }
   ```

**Key constraint**: Only groups with sum exactly = 0 are auto-matched.

### `POST /tasks/emargement`

**Called by**: `archive_matched` DAG at 03:30 UTC (after reconciliation)

**Process**:
1. Find non-pending entries still in `reconciliation_entry` (live table)
2. Insert into `reconciliation_entry_emargement` with `emarged_at = NOW()`
3. Delete from `reconciliation_entry`
4. Return count moved
5. Return:
   ```json
   {
     "emarged_count": 5230,
     "deleted_count": 5230
   }
   ```

**Atomic**: Uses CTE + INSERT SELECT, single transaction.

**Note**: This is a safety net. Normally entries are moved to émargement inline during matching/forcing/excluding.

---

## Environment Variables for Airflow

In `.env` (loaded by docker-compose for Airflow services):

```bash
# Airflow metadata
AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=postgresql://airflow:...@airflow-db/airflow
AIRFLOW__CORE__EXECUTOR=CeleryExecutor
AIRFLOW__CELERY__BROKER_URL=redis://redis:6379/1
AIRFLOW__CELERY__RESULT_BACKEND=db+postgresql://...

# DAG configuration
AIRFLOW__CORE__DAGS_FOLDER=/opt/airflow/dags
AIRFLOW__CORE__LOAD_EXAMPLES=False
AIRFLOW__CORE__LOAD_DEFAULT_CONNECTIONS=False

# Backend communication
AIRFLOW_BACKEND_URL=http://backend:8000/api/v1
RECO_BACKEND_INTERNAL_TOKEN=your-secret-token

# Web UI
AIRFLOW__WEBSERVER__BASE_LOG_FOLDER=/opt/airflow/logs
AIRFLOW__WEBSERVER__EXPOSE_CONFIG=False
```

---

## Scheduling Rules

| DAG | Schedule | Timezone | Purpose | Active |
|---|---|---|---|---|
| `ingest_atm` | */15 * * * * | UTC | ATM polling (priority) | ✅ |
| `ingest_ip` | */30 * * * * | UTC | IP MT940 + Finacle | ⏸️ |
| `ingest_other_payments` | */30 * * * * | UTC | Other MT940 + Finacle | ⏸️ |
| `ingest_webripost` | */30 * * * * | UTC | Webripost CSV + Finacle | ⏸️ |
| `ingest_float_ip` | @daily | UTC | Float IP (Finacle only) | ⏸️ |
| `reconcile_daily` | 0 2 * * * | UTC | Auto engine (after all ingests) | ✅ |
| `archive_matched` | 30 3 * * * | UTC | Émargement sweep (safety net) | ✅ |

**Paused DAGs** can be activated via Airflow UI when samples arrive and parsers are validated.

---

## Monitoring & Debugging

### Airflow UI
- **http://localhost:8080** (if running with `--profile airflow`)
- Dashboard: DAGs, recent runs, task status
- Admin → Connections: define Finacle, file sources
- Admin → Variables: store shared configuration

### Viewing DAG Code
DAGs are read from `shared/dags/` — Airflow auto-reloads. Edit and refresh UI.

### Checking Task Logs
```bash
docker-compose logs airflow-webserver    # UI logs
docker-compose logs airflow-scheduler    # Scheduler logs
docker-compose logs airflow-worker       # Task execution logs
```

### Testing a DAG Locally
```bash
cd shared/dags
python -m pytest test_dag_factory.py  # (if tests exist)
```

### Triggering a DAG Manually
```bash
docker exec airflow-webserver \
  airflow dags test reconcile_daily 2025-01-15
```

---

## Best Practices

1. **Idempotent DAGs** — can run multiple times with same data
2. **Retry logic** — DAGs set retries + retry delay
3. **Max active runs = 1** — prevent concurrent executions of same DAG
4. **Catchup = False** — don't backfill, only run from now
5. **Dependencies** — task A >> task B creates dependency graph
6. **Monitoring** — Airflow emails on failure (if configured)

---

## Extending Airflow (When New Flows Arrive)

### To add a new flow (e.g., "new_source"):

1. **Add to backend** (`backend/app/db/seed_flows.py`):
   ```python
   {
       "code": "new_source",
       "name": "New Source",
       "is_active": False,
       "parser_type": ParserType.CSV,  # or MT940, etc.
       "parser_config": { ... },
       "accounts": [ ... ],
   }
   ```

2. **Create DAG** (`shared/dags/ingest_new_source.py`):
   ```python
   from dag_factory import build_ingestion_dag
   
   dag = build_ingestion_dag(
       flow_code="new_source",
       schedule="*/30 * * * *",
       finacle=False,
       is_paused=True,  # Start paused
       description="New source flow",
   )
   ```

3. **Implement parser** (if needed):
   ```python
   # backend/app/services/parsers/new_source_parser.py
   from .base_parser import BaseParser, ParsedEntry
   
   class NewSourceParser(BaseParser):
       def parse(self, file_path: str) -> list[ParsedEntry]:
           # Implementation
           pass
   ```

4. **Register parser** (`backend/app/services/parsers/__init__.py`):
   ```python
   def get_parser(parser_type: ParserType, config: dict) -> BaseParser:
       return {
           # ... existing
           ParserType.NEW_SOURCE: NewSourceParser(config),
       }[parser_type]
   ```

5. **Test** — drop sample file in `shared/inbox/new_source/`, trigger manually
6. **Activate** — when satisfied, set `is_active=True` in seed, restart backend, activate DAG in Airflow UI

---

## External Airflow

If using corporate Airflow (not Docker):

1. Keep `AIRFLOW_USE_EXTERNAL=true` in backend `.env`
2. Set `AIRFLOW_API_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD`
3. Deploy `shared/dags/*` to corporate Airflow `dags_folder`
4. Ensure corporate Airflow can reach backend API
5. Create Finacle/Source connections via corporate Airflow UI
6. Everything else is same (schedules, task structure, etc.)

---

## Orchestrator DAG

### Purpose

The `orchestrate_ingestion` DAG is designed to be **triggered externally** by an external
application. It discovers all active flows, ingests all their sources, then runs
auto-reconciliation and émargement — all in one execution.

### Triggering

```bash
curl -X POST http://airflow:8080/api/v1/dags/orchestrate_ingestion/dagRuns \
  -H "Content-Type: application/json" \
  -u "airflow:airflow" \
  -d '{"conf": {}}'
```

### Task Graph

```
ingest_all_active_flows → auto_reconcile → sweep_emargement
```

1. **`ingest_all_active_flows`**: Calls `POST /tasks/active-flows` to discover active flows,
   then for each flow calls `POST /tasks/ingest/{flow_code}` (file sources) and/or
   `POST /tasks/ingest/{flow_code}/finacle` (Finacle sources). Errors per flow are logged
   but don't stop other flows from being processed.

2. **`auto_reconcile`**: Calls `POST /tasks/reconcile` to run the auto-matching engine.

3. **`sweep_emargement`**: Calls `POST /tasks/emargement` to archive matched/excluded entries.

### Coexistence with Individual DAGs

| Feature | Individual DAGs | Orchestrator |
|---------|----------------|--------------|
| Trigger | Schedule (cron) | External API call |
| Scope | Single flow | All active flows |
| Use case | Automatic polling | On-demand batch |

Both can safely coexist. The deduplication (`source_hash` UNIQUE + ON CONFLICT DO NOTHING)
ensures no duplicates even if both run on the same data.

---

## End-to-End Technical Flow

### File Ingestion — Step by Step

```
1. File dropped in shared/inbox/{subfolder}/
       │
2. Airflow DAG polls (schedule or external trigger)
       │
3. PythonOperator calls task_ingest(flow_code)
       │
4. POST /tasks/ingest/{flow_code} → backend
       │
5. ingestion_service.ingest_inbox_for_flow(flow)
       │
6. For each active FILE source:
   │
   6a. Resolve inbox_dir = shared/inbox/{source.inbox_subfolder or flow.code}
   │
   6b. List files in inbox_dir
   │
   6c. Filter by source.file_pattern (fnmatch glob) if set
   │     → e.g. "*.dat" matches only .dat files
   │     → null/empty = all files
   │
   6d. For each matching file:
       │
       6d-i.   get_parser(source.parser_type, source.parser_config)
       │         → CobolMoselParser, CsvParser, ExcelParser, XmlParser, MT940Parser
       │
       6d-ii.  parser.parse(file_path) → ParseResult(entries=[], errors=[])
       │
       6d-iii. _persist(entries, flow_id, source_id)
       │         → Compute source_hash for each entry
       │         → bulk INSERT into reco.reconciliation_entry
       │         → ON CONFLICT (source_hash) DO NOTHING (dedup)
       │
       6d-iv.  Move file → inbox_processed/ (success) or inbox_error/ (failure)
       │
       6d-v.   Create IngestionRun record with stats
              (rows_in, rows_ok, rows_ko, rows_duplicate)

7. For Finacle DB sources (separate endpoint):
   POST /tasks/ingest/{flow_code}/finacle
       │
   7a. FinacleDBExtractor connects to Finacle DB via connection_id
   7b. Executes finacle_query (with optional ?since parameter)
   7c. Parses results → entries
   7d. _persist() same as above
   7e. IngestionRun created

8. Auto-reconciliation:
   POST /tasks/reconcile
       │
   8a. reconciliation_service.run_auto()
   8b. Groups PENDING entries by (flow_id, reco_id, currency)
   8c. For each group: if SUM(amount) == 0 → create MatchGroup(mode=AUTO)
   8d. Mark entries as MATCHED, set match_group_id
   8e. Create ReconciliationRun with stats

9. Émargement (archive):
   POST /tasks/emargement
       │
   9a. Sweep MATCHED/FORCED/EXCLUDED entries from reco.reconciliation_entry
   9b. Copy to reco.reconciliation_entry_emargement
   9c. Delete from live table
```

### Multi-Source File Pattern Resolution

When two sources share the same `inbox_subfolder` but need different parsers:

```
shared/inbox/atm/
├── ATM_20250115.dat    ← Source 1 (cobol_mosel, file_pattern: "*.dat")
├── ATM_20250115.csv    ← Source 2 (csv, file_pattern: "*.csv")
└── ATM_summary.xlsx    ← Ignored by both (no matching pattern)
```

Each source only processes files matching its `file_pattern` glob. If `file_pattern`
is null, the source processes all files (backward compatible).
