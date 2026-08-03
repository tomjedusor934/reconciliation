# Documentation Index

Complete technical documentation for the Payment Reconciliation App.

## Quick Start
- **Start here**: [`README.md`](../README.md) — 2-minute overview

## Documentation Files

### 1. **Project Overview** [`00-PROJECT-OVERVIEW.md`](00-PROJECT-OVERVIEW.md)
**Read this first to understand the big picture.**

- Purpose & goals
- High-level architecture diagram
- Technology stack
- Project structure & file organization
- Core concepts (Flows, Entries, Matching, Exclusion, Audit, Émargement)
- Happy path workflows
- Environment variables
- Key implementation rules
- Next steps when you have real data

**Time to read**: 15 minutes

---

### 2. **Docker Compose Setup** [`01-DOCKER-COMPOSE.md`](01-DOCKER-COMPOSE.md)
**Read this to understand how services are containerized and networked.**

- Stack overview (db, redis, backend, frontend, airflow)
- Detailed explanation of each service
- Volume mappings
- Network configuration (internal vs external access)
- Environment variables explained
- Startup flows (basic stack, full stack, external Airflow)
- Health checks & debugging
- Performance tuning notes
- Backup & disaster recovery

**Time to read**: 20 minutes

---

### 3. **Backend Architecture** [`02-BACKEND.md`](02-BACKEND.md)
**Read this to understand FastAPI structure, business logic, and data flow.**

- Directory structure
- Key files explained (main.py, config, DB session, models, repositories, services, endpoints)
- Architecture rules (Endpoint → Service → Repository → Model)
- Each service's responsibility
- Parser architecture & factory pattern
- API endpoint patterns
- Internal Airflow endpoints (`/tasks/*`)
- Performance considerations
- Common workflows in code

**Key rules**:
- Services never raise HTTPException
- Plural kebab-case routes
- Models re-exported in `__init__.py`
- Singletons at module bottom

**Time to read**: 25 minutes

---

### 4. **Frontend Architecture** [`03-FRONTEND.md`](03-FRONTEND.md)
**Read this to understand Vue 3 component structure, state management, and API integration.**

- Directory structure
- Key files explained (axios, types, stores, services, router, components, views)
- Composition API patterns (no Options API, no `this`)
- Pinia store (auth, sidebar)
- Service layer pattern (literal objects, not classes)
- Router & navigation guards
- RBAC-integrated components (Button, Input, etc.)
- Table component with search/sort/pagination
- Example views (list, form, dashboard)
- Component patterns & best practices
- Performance tips

**Key rules**:
- `<script setup lang="ts">` ALWAYS
- Type all props & emits
- Reuse UI library components
- No native HTML elements (use Table, Modal, Input, etc.)

**Time to read**: 25 minutes

---

### 5. **Airflow & DAGs** [`04-AIRFLOW.md`](04-AIRFLOW.md)
**Read this to understand scheduling, orchestration, and async task execution.**

- Architecture overview
- DAG structure & workflow
- Generic ingest DAG factory
- Backend client utilities (`common.py`)
- Specific DAGs (ATM, IP, reconciliation, émargement)
- Backend `/tasks/*` endpoints
- Environment variables for Airflow
- Scheduling rules (times, frequencies)
- Monitoring & debugging
- Best practices
- Extending Airflow (adding new flows)
- External Airflow setup

**Time to read**: 20 minutes

---

### 6. **File Parsers & Ingestion** [`05-PARSERS.md`](05-PARSERS.md)
**Read this to understand how different file formats are parsed and ingested.**

- Parser architecture (BaseParser abstract class, ParsedEntry dataclass)
- Factory function pattern
- Cobol/MOSEL parser (ATM — fixed-width format)
- CSV parser (generic, configurable column mapping)
- XML parser (XPath-based extraction)
- MT940 parser (BCEE format — STUB, needs sample)
- Finacle DB extractor (SQL-based)
- Ingestion workflow (polling, parsing, validation, bulk insert)
- Adding a new parser (step-by-step guide)
- Parser config examples for each flow

**Time to read**: 20 minutes

---

### 7. **Database Schema & SQL** [`06-DATABASE.md`](06-DATABASE.md)
**Read this to understand data models, two-table design, and audit trails.**

- 3 schemas: public (Orchestro), reco (reconciliation), audit (logs)
- Flow & FlowAccount tables
- SourceConnection table
- **ReconciliationEntry table** (high-volume, two-table design)
- MatchGroup table
- Exclusion table
- IngestionRun & ReconciliationRun tables
- AuditLog & UIActionLog tables
- Initialization sequence
- Two-table design: live (pending) + émargement (validated)
- Performance tuning (indexes, constraints)
- Backup & disaster recovery
- Common debugging queries

**Key concepts**:
- Simple PK on `id` (BigInteger, autoincrement)
- Table-wide UNIQUE constraint on source_hash
- Two-table design: `reconciliation_entry` (live) + `reconciliation_entry_emargement`
- BRIN index on append-only columns

**Time to read**: 25 minutes

---

### 8. **Instructions for AI/LLM Development** [`07-AI-INSTRUCTIONS.md`](07-AI-INSTRUCTIONS.md)
**Read this if you're an AI assistant working on this project.**

- Quick context summary
- Understanding the code (rules by component)
- File naming conventions
- Common tasks (add flow, add action, debug data)
- Running tests & validation
- Code patterns (service, endpoint, component)
- Troubleshooting quick guide
- Links to key files
- When you get stuck

**Time to read**: 10 minutes

---

## How to Use This Documentation

### "I want to understand the project"
Read in this order:
1. [`README.md`](../README.md) (2 min)
2. [`00-PROJECT-OVERVIEW.md`](00-PROJECT-OVERVIEW.md) (15 min)
3. [`01-DOCKER-COMPOSE.md`](01-DOCKER-COMPOSE.md) (20 min)

### "I need to modify the backend"
1. [`02-BACKEND.md`](02-BACKEND.md) — Architecture & patterns
2. [`06-DATABASE.md`](06-DATABASE.md) — Data model
3. Relevant code files (see links at bottom of docs)

### "I need to add a new file format parser"
1. [`05-PARSERS.md`](05-PARSERS.md) — Parser architecture
2. Look at existing parser implementations
3. Follow the "Adding a New Parser" section

### "I need to add a new flow type"
1. [`00-PROJECT-OVERVIEW.md`](00-PROJECT-OVERVIEW.md) — Understand flows
2. [`05-PARSERS.md`](05-PARSERS.md) — Parser for the format
3. [`04-AIRFLOW.md`](04-AIRFLOW.md) — Create the DAG
4. [`02-BACKEND.md`](02-BACKEND.md) — Seed in `seed_flows.py`

### "I need to deploy this"
1. [`01-DOCKER-COMPOSE.md`](01-DOCKER-COMPOSE.md) — Docker setup
2. `.env.example` — Environment variables
3. Health checks & debugging section

### "I'm an AI assistant and need context"
Read: [`07-AI-INSTRUCTIONS.md`](07-AI-INSTRUCTIONS.md)

---

## Key Concepts (Quick Reference)

| Concept | Definition | Location |
|---------|-----------|----------|
| **Flow** | Payment channel (ATM, IP, etc.) with config (parser, match strategy) | `reco.flow` table, `backend/app/models/flow.py` |
| **Entry** | Single transaction record (reco_id, amount, date, etc.) | `reco.reconciliation_entry` table (live) |
| **Match Group** | Set of entries that sum to 0 (auto or forced) | `reco.match_group` table |
| **Exclusion** | Entry marked as excluded with reason | `reco.exclusion` table |
| **Parser** | Converts file format to normalized ParsedEntry | `backend/app/services/parsers/` |
| **Source Hash** | SHA256 of (reco_id, ref, amount, date) for dedup | `ParsedEntry.compute_source_hash()` |
| **Émargement** | Table holding validated entries (matched/forced/excluded) | `reco.reconciliation_entry_emargement` |
| **Audit** | DB triggers + UI action logs | `audit.audit_log`, `audit.ui_action_log` |
| **DAG** | Airflow workflow (ingest, reconcile, émargement, etc.) | `shared/dags/` |

---

## Architecture Decisions

### Why Two-Table Design?
- High volume (~1M+ transactions/year)
- Live table stays small (only pending entries) → fast queries
- Émargement table holds validated entries with `emarged_at` timestamp
- Entries move inline after matching/forcing/excluding (DAG sweeps as safety net)

### Why Source Hash?
- Prevents duplicate ingestion if same file processed twice
- SHA256 of key fields (reco_id, amount, date, etc.)
- Table-wide UNIQUE constraint

### Why Audit Schema?
- Separate schema for compliance & debugging
- DB triggers auto-log all changes
- UI action log for user-initiated actions
- Full chain of custody for reconciliation

### Why Airflow?
- Reliable, battle-tested task orchestration
- Web UI for monitoring & debugging
- Retry logic, error handling
- Flexible: can run in Docker or corporate Airflow

### Why Vue 3 Composition API?
- Modern, type-safe (TypeScript)
- Reactive state management (simpler than Options API)
- Composable functions for code reuse

---

## File Structure Cheat Sheet

```
reconciliation/
├── README.md                          ← Start here
├── docs/                              ← You are here
│   ├── INDEX.md                       ← This file
│   ├── 00-PROJECT-OVERVIEW.md         ← Big picture
│   ├── 01-DOCKER-COMPOSE.md           ← Docker setup
│   ├── 02-BACKEND.md                  ← FastAPI
│   ├── 03-FRONTEND.md                 ← Vue 3
│   ├── 04-AIRFLOW.md                  ← Scheduling
│   ├── 05-PARSERS.md                  ← Ingest logic
│   ├── 06-DATABASE.md                 ← SQL schema
│   └── 07-AI-INSTRUCTIONS.md          ← For AI assistants
│
├── backend/app/                       ← FastAPI code
│   ├── main.py                        ← Startup
│   ├── core/config.py                 ← Settings
│   ├── db/                            ← DB setup
│   ├── models/                        ← ORM
│   ├── services/                      ← Business logic
│   ├── repositories/                  ← CRUD
│   └── api/v1/endpoints/              ← REST endpoints
│
├── frontend/src/                      ← Vue 3 code
│   ├── main.ts                        ← Bootstrap
│   ├── stores/                        ← Pinia state
│   ├── services/                      ← API client
│   ├── types/                         ← TypeScript
│   ├── views/                         ← Pages
│   └── components/                    ← Components
│
├── shared/dags/                       ← Airflow DAGs
│   ├── common.py                      ← Utilities
│   ├── ingest_*.py                    ← Ingest DAGs
│   ├── reconcile_daily.py             ← Auto engine
│   └── archive_matched.py             ← Émargement sweep
│
├── shared/inbox/                      ← Input files
│   ├── atm/
│   ├── mt940_ip/
│   └── ...
│
├── .env.example                       ← Config template
└── docker-compose.yml                 ← Docker stack
```

---

## Common Questions Answered

**Q: How do I add a new flow type?**
A: See "Common Tasks → Add a New Flow Type" in `07-AI-INSTRUCTIONS.md`

**Q: How do I debug why entries aren't matching?**
A: See "Troubleshooting Quick Guide" in `07-AI-INSTRUCTIONS.md` or "Debugging Data Issues"

**Q: Can I run Airflow externally?**
A: Yes, set `AIRFLOW_USE_EXTERNAL=true` in `.env`. See `04-AIRFLOW.md`

**Q: What's the PK for reconciliation_entry?**
A: `id` (BigInteger, autoincrement) — simple PK, no composite key

**Q: How do I prevent duplicate ingestion?**
A: Source hash `source_hash` is UNIQUE (table-wide); `ON CONFLICT DO NOTHING` skips duplicates

**Q: Where's the audit trail?**
A: `audit.audit_log` (DB triggers) + `audit.ui_action_log` (user actions)

**Q: When should I add a DB migration?**
A: Schema changes go in `init_reco.py` (idempotent); no Alembic versioning (yet)

---

## Contributing to Documentation

If you update code, update relevant docs:
- New models? Update `06-DATABASE.md`
- New endpoints? Update `02-BACKEND.md`
- New views? Update `03-FRONTEND.md`
- New DAGs? Update `04-AIRFLOW.md`
- New parser? Update `05-PARSERS.md`

Keep docs in sync with code!

---

## Last Updated
**Date**: May 4, 2025
**Status**: Complete scaffolding, docs/07-AI-INSTRUCTIONS.mawaiting real data samples for tests

---


For questions, refer to the relevant doc or check the code directly.
