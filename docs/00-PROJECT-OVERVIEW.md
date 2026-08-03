# Payment Reconciliation App — Complete Technical Overview

## Purpose
Generic payment reconciliation platform for Post Luxembourg, supporting 5 distinct payment flows with automatic matching, manual force matching, exclusion management, and full audit trails.

## High-level Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  DATA SOURCES                                                       │
├────────────────┬────────────────┬────────────────┬──────────────────┤
│  ATM Files     │  MT940 Files   │  Webripost CSV │  Finacle DB      │
│  (Cobol MOSEL) │  (BCEE)        │  (Generic)     │  (External DB)   │
└────────────┬───┴────────────┬───┴────────────┬───┴──────────────┬───┘
             │                │                │                  │
             │  shared/inbox/{flow}/           │                  │
             └────────────────┬────────────────┴──────────────-───┘
                              │
                    ┌─────────▼─────────┐
                    │  Airflow DAGs     │
                    │  (Polling 15min)  │
                    └─────────┬─────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
        ┌─────▼────────┐ ┌──-─▼─────────┐ ┌─--▼────────────┐
        │  Parse       │ │  Bulk insert │ │ Daily engine   │
        │  (parser_*)  │ │  ON CONFLICT │ │ (group by...   │
        │              │ │  source_hash │ │  sum=0)        │
        │  Validate    │ │              │ │                │
        └──────────────┘ └──────┬───────┘ └─────-─┬────────┘
                                │                 │
                    ┌───────────▼─────────────────▼────────┐
                    │  reconciliation_entry (live)         │
                    │   ├─ id (BigInteger PK)              │
                    │   ├─ flow_id                         │
                    │   ├─ reco_id (match key)             │
                    │   ├─ amount (match key)              │
                    │   ├─ currency (match key)            │
                    │   ├─ value_date                      │
                    │   ├─ status (pending/matched/...)    │
                    │   └─ match_group_id (FK to match)    │
                    └───────────┬──────────────────────────┘
                                │
                    ┌───────────▼──────────────┐
                    │  match_group             │
                    │   ├─ mode (auto/forced)  │
                    │   ├─ sum (for validation)│
                    │   └─ reconciliation_run  │
                    └──────────────────────────┘
                                │
                    ┌───────────▼──────────────────────┐
                    │  Émargement (after validation)   │
                    │  reconciliation_entry_            │
                    │  emargement (+ emarged_at)        │
                    └──────────────────────────────────┘
```

## Technology Stack

| Layer | Technology | Version | Purpose |
|-------|-----------|---------|---------|
| **DB** | PostgreSQL 16 | 16+ | Primary transactional DB |
| **Cache/Queue** | Redis 7 | 7+ | Session cache, Airflow Celery broker |
| **Backend** | FastAPI | 0.104+ | REST API, business logic, auth |
| **ORM** | SQLAlchemy | 1.4+ | DB models, repositories |
| **Frontend** | Vue 3 | 3.3+ | UI, Composition API |
| **Bundler** | Vite | Latest | Frontend build |
| **Orchestration** | Apache Airflow | 2.8+ | Scheduling DAGs (ingest, reconcile, émargement) |
| **Task Queue** | Celery | 5.3+ | Airflow worker executor |
| **Auth** | OAuth2 + JWT | — | SSO-only, Cookie-based JWT + CSRF |

## Project Structure

```
reconciliation/
├── docker-compose.yml              # Main stack (backend, frontend, db, redis)
├── docker-compose.test.yml         # Test DB (if needed)
├── .env.example                    # Environment template
├── README.md                        # Quick start
├── docs/                            # This documentation
│
├── backend/                         # FastAPI application
│   ├── app/
│   │   ├── main.py                 # Entry point, startup hooks
│   │   ├── core/
│   │   │   ├── config.py           # Settings from env vars
│   │   │   ├── security.py         # JWT, bcrypt, password
│   │   │   ├── team_filter.py      # RBAC filtering
│   │   │   └── middleware.py       # CSRF, logging, auth
│   │   ├── db/
│   │   │   ├── base.py             # SQLAlchemy declarative base
│   │   │   ├── session.py          # Engine, SessionLocal
│   │   │   ├── init_db.py          # Superuser seed, SSO config
│   │   │   ├── init_reco.py        # Reco schemas, triggers
│   │   │   └── seed_flows.py       # Flow + account seed (5 flows)
│   │   ├── models/                 # SQLAlchemy models
│   │   │   ├── __init__.py         # Re-exports all models
│   │   │   ├── flow.py             # Flow, FlowAccount, enums
│   │   │   ├── source_connection.py
│   │   │   ├── reconciliation_entry.py  # Main table (live + émargement)
│   │   │   ├── match_group.py
│   │   │   ├── exclusion.py
│   │   │   ├── ingestion_run.py
│   │   │   ├── reconciliation_run.py
│   │   │   └── audit_log.py        # Data + UI action logs
│   │   ├── schemas/                # Pydantic v2 response models
│   │   │   ├── flow.py
│   │   │   ├── reconciliation.py   # All reco schemas
│   │   │   └── ...
│   │   ├── repositories/           # CRUD + complex queries
│   │   │   ├── flow_repository.py
│   │   │   ├── reconciliation_entry_repository.py
│   │   │   ├── match_group_repository.py
│   │   │   └── ...
│   │   ├── services/               # Business logic
│   │   │   ├── flow_service.py
│   │   │   ├── reconciliation_service.py  # Auto engine, force, exclude
│   │   │   ├── emargement_service.py
│   │   │   ├── dashboard_service.py
│   │   │   ├── airflow_client_service.py
│   │   │   └── parsers/            # File parsers
│   │   │       ├── base_parser.py          # Abstract
│   │   │       ├── cobol_mosel_parser.py   # ATM
│   │   │       ├── csv_parser.py
│   │   │       ├── xml_parser.py
│   │   │       ├── mt940_parser.py         # BCEE (stub)
│   │   │       └── finacle_db_extractor.py # DB extractor
│   │   ├── api/v1/
│   │   │   ├── api.py              # Router registration
│   │   │   ├── deps.py             # Dependencies (DB, Auth, etc.)
│   │   │   └── endpoints/
│   │   │       ├── flows.py        # CRUD /flows
│   │   │       ├── reconciliation_entries.py
│   │   │       ├── match_groups.py
│   │   │       ├── ingestion_runs.py
│   │   │       ├── reconciliation_runs.py
│   │   │       ├── dashboards.py   # KPIs
│   │   │       ├── audit.py        # Audit logs
│   │   │       └── tasks.py        # Internal Airflow endpoints
│   │   └── tasks/                  # Async task definitions (if used)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── alembic/                    # DB migrations (if used)
│
├── frontend/                       # Vue 3 application
│   ├── src/
│   │   ├── main.ts                 # Bootstrap
│   │   ├── App.vue                 # Root component
│   │   ├── style.css               # Tailwind directives
│   │   ├── api/
│   │   │   └── axios.ts            # HTTP client (withCredentials, CSRF)
│   │   ├── types/
│   │   │   └── index.ts            # All TypeScript interfaces
│   │   ├── stores/
│   │   │   ├── auth.ts             # Pinia auth store
│   │   │   └── sidebar.ts          # Sidebar state
│   │   ├── services/
│   │   │   ├── flowService.ts
│   │   │   ├── reconciliationService.ts
│   │   │   ├── matchGroupService.ts
│   │   │   ├── dashboardService.ts
│   │   │   ├── runService.ts
│   │   │   ├── auditService.ts
│   │   │   └── ...existing (user, role, sso)
│   │   ├── utils/
│   │   │   ├── cn.ts               # clsx + twMerge
│   │   │   ├── toaster.ts          # Toast notifications
│   │   │   └── routeUtils.ts       # RBAC route helpers
│   │   ├── router/
│   │   │   └── index.ts            # Route definitions + guards
│   │   ├── components/
│   │   │   ├── layout/             # AppLayout, Navbar, Sidebar, etc.
│   │   │   └── ui/                 # Reusable components (Table, Modal, etc.)
│   │   ├── config/
│   │   │   └── sidebarLinks.ts     # Sidebar structure + permissions
│   │   └── views/                  # Page components
│   │       ├── flows/              # FlowList, FlowForm
│   │       ├── reconciliation/      # Dashboard, Operational, Transversal, Runs, Audit
│   │       └── ...existing (users, roles, settings)
│   ├── package.json
│   ├── vite.config.ts
│   ├── tsconfig.json
│   └── Dockerfile
│
├── shared/                         # Shared volumes / DAGs
│   ├── inbox/                      # Input folder (mounted from docker)
│   │   ├── atm/                    # ATM files (Cobol/MOSEL)
│   │   ├── mt940_ip/               # IP MT940 files
│   │   ├── mt940_other/            # Other MT940 files
│   │   ├── webripost/              # Webripost CSV
│   │   └── finacle/                # Finacle extracts (if file-based)
│   ├── inbox_processed/            # Successfully processed files
│   │   └── atm/
│   ├── inbox_error/                # Failed files (with error log)
│   │   └── atm/
│   ├── dags/                       # Airflow DAGs (generated + static)
│   │   ├── common.py               # Backend client utilities
│   │   ├── dag_factory.py          # Generic ingest DAG builder
│   │   ├── ingest_atm.py           # ATM every 15 min (active)
│   │   ├── ingest_ip.py            # IP MT940 (paused)
│   │   ├── ingest_other_payments.py
│   │   ├── ingest_webripost.py
│   │   ├── ingest_float_ip.py
│   │   ├── reconcile_daily.py      # Auto engine (02:00 UTC)
│   │   └── archive_matched.py      # Émargement sweep (03:30 UTC)
│   ├── airflow_logs/               # Airflow task logs
│   └── airflow_plugins/            # Custom Airflow plugins (empty)
│
└── schema_fichier_reco.txt         # Input file format specs
```

## Core Concepts

### Flows
5 payment flows, each with a unique:
- **Source** (file, DB, or hybrid)
- **Parser** (Cobol, CSV, XML, MT940, Finacle DB)
- **Match key strategy** (reco_id+amount, file+ref+amount, ref+amount)
- **Accounts** to track
- **Parser config** (delimiter, date format, allowed event types, etc.)

**Active at launch:** ATM only. Others seeded inactive, ready to activate once samples arrive.

### Reconciliation Entry
High-volume fact table (millions of rows). Two-table design:
- `reconciliation_entry` = **live table** — ingestion inserts here, only pending entries remain
- `reconciliation_entry_emargement` = **émargement table** — entries move here once reconciliation is done (matched/forced/excluded), with `emarged_at` timestamp
- Each row = 1 transaction from a flow
- Uniqueness = `source_hash` (table-wide UNIQUE, prevents duplicates from re-ingestion)
- PK = `id` (BigInteger, autoincrement)
- Status: `pending` → `matched` (auto) / `forced` (manual) / `excluded` (manual)
- Link to `match_group` when matched

### Match Group
- **Auto match**: sum(amount) = 0 within a group (flow+reco_id+currency)
- **Forced match**: user selects ≥2 entries, can force even if unbalanced (with comment)
- Entries flip to status='matched' + match_group_id

### Exclusion
- Single entry marked as `status='excluded'` with mandatory reason
- Prevents that entry from ever being auto-matched

### Audit
- **Data audit**: DB triggers log all changes to reconciliation tables (except high-volume entry table)
- **UI audit**: services log user actions (force, exclude, login, etc.)
- `audit_log.user_id` requires middleware to set `app.current_user_id` setting

### Émargement (Two-Table Design)
- `reconciliation_entry` (live) — only pending entries remain here
- `reconciliation_entry_emargement` — entries are moved here immediately after reconciliation is validated (matched, forced, or excluded), with an `emarged_at` timestamp
- The `archive_matched` DAG acts as a safety net to sweep any remaining non-pending entries from live to émargement

---

## Workflows (Happy Paths)

### 1. Ingest ATM File
- File dropped in `shared/inbox/atm/`
- `ingest_atm` DAG (every 15 min) calls `POST /tasks/ingest/atm`
- Backend: reads file → parses (Cobol) → validates → computes `source_hash` → bulk inserts
- File moved to `shared/inbox_processed/atm/` on success, `shared/inbox_error/atm/` on failure
- `ingestion_run` row created with row counts and any errors

### 2. Auto Reconciliation
- Daily DAG `reconcile_daily` (02:00 UTC) calls `POST /tasks/reconcile`
- Backend: groups pending entries by (flow_id, reco_id, currency)
- For each group with sum=0 → create `match_group(mode='auto')` + flip entries to `matched`
- `reconciliation_run` row records counts

### 3. Émargement Sweep
- Daily DAG `archive_matched` (03:30 UTC) calls `POST /tasks/emargement`
- Backend: finds any non-pending entries still in live table → moves to `reconciliation_entry_emargement`
- Safety net — normally entries are moved inline right after matching/forcing/excluding

### 4. User Forces a Match
- UI: select ≥2 entries (same flow, same currency)
- If sum ≠ 0, comment is mandatory
- `POST /match-groups/force` with `entry_ids`, optional `comment`
- Backend: creates `match_group(mode='forced')` + flips entries to `matched` + logs UI action

### 5. User Excludes an Entry
- UI: single pending entry, provide reason (mandatory)
- `POST /reconciliation-entries/exclude` with `entry_id`, `reason`
- Backend: flips entry to `status='excluded'` + inserts `exclusion` row + logs UI action

---

## Environment Variables
See `.env.example`, grouped by component:

- **Backend**: `BACKEND_PORT`, `BACKEND_CORS_ORIGINS`, `BACKEND_DEBUG`
- **Database**: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `DATABASE_URL`
- **Redis**: `REDIS_URL`
- **Inbox/Ingestion**: `INBOX_BASE_PATH`
- **Airflow**: `AIRFLOW_USE_EXTERNAL`, `AIRFLOW_API_URL`, `AIRFLOW_USER`, `AIRFLOW_PASSWORD`, `AIRFLOW_CORE_DAGS_FOLDER`
- **Auth**: `SECRET_KEY`, `ALGORITHM`, `SSO_DEFAULT_ROLE_NAME`

---

## Key Implementation Rules (Enforced by copilot-instructions)

1. **Endpoint → Service → Repository → Model** (strict layering)
2. **Services never raise HTTPException** (endpoints translate errors)
3. **Singletons at module bottom** (avoid circular imports)
4. **Plural kebab-case for routes** (`/flows`, `/match-groups`, `/reconciliation-entries`)
5. **Simple PKs** — `reconciliation_entry` PK is just `id` (BigInteger, autoincrement)
6. **Bulk inserts use ON CONFLICT DO NOTHING** (dedup by source_hash)
7. **Frontend: < script setup> + TypeScript always** (no Options API, no JS) <!-- leave space betwen < script  otherwise md security disable the line-->
8. **Reuse UI lib components** (Table, Modal, Input, Button, etc.) — don't reinvent

---

## Testing / Validation Notes

- **No pytest/vitest yet** — awaiting real ATM file sample
- **syntax check**: `python -m py_compile backend/app/**/*.py`
- **Docker**: `docker-compose up -d` then manual API / UI tests

---

## Next Steps (When You Have Samples)

1. **ATM sample file** → validate Cobol parser encoding, sign format
2. **MT940 BCEE sample** → implement `mt940_parser.py`
3. **Finacle ODS schema** → set up connection in `source_connection` UI
4. **Run E2E tests** with real data
5. **Wire audit `app.current_user_id`** in middleware

---

See individual docs/ files for deep dives into each component.
