# Payment Reconciliation App

Generic payment reconciliation platform for **Post Luxembourg**, supporting 5 flows:
**ATM (priority)**, IP, Other Payments, Webripost, Float IP.

Built on the Orchestro template (FastAPI backend + Vue 3 frontend + PostgreSQL +
Redis), extended with **Apache Airflow** for the ingestion / reconciliation /
émargement workflows.

## Architecture

```
Files (CSV / XML / Cobol-MOSEL) → shared/inbox/{flow}/   ┐
Finacle DB                                                 ├─→  Airflow DAGs (every X min)
                                                           │       └─ POST /tasks/ingest/{flow}
                                                           ▼
                                              reconciliation_entry  (live table)
                                                           │
                                                           ▼
                                               Daily DAG  →  /tasks/reconcile
                                                           │   group by flow+reco_id+currency
                                                           │   sum=0 → match_group (auto)
                                                           ▼
                                              Daily DAG  →  /tasks/archive
                                                           │   matched > 90d → archive table
```

### Schemas

- `public` — Orchestro tables (users, roles, settings, sso_provider, …)
- `reco`   — reconciliation tables (`flow`, `flow_account`, `source_connection`,
            `ingestion_run`, `reconciliation_entry`,
            `reconciliation_entry_emargement`, `match_group`, `exclusion`,
            `reconciliation_run`)
- `audit`  — `audit_log` (DB triggers) + `ui_action_log` (app actions)

### Two-table design & volumetry

- `reconciliation_entry` (**live table**) — ingestion inserts parsed entries
  here. Only pending entries remain; matched/forced/excluded entries are moved
  to the émargement table immediately after reconciliation.
- `reconciliation_entry_emargement` — final destination for validated entries.
  Once an entry is matched, forced, or excluded, it is moved here with an
  `emarged_at` timestamp.
- UNIQUE constraint on `source_hash` in both tables prevents duplicate ingestion.
- Indexes: B-tree on `(flow_id, status, value_date)`, `reco_id`,
  `match_group_id`, `value_date`.
- Bulk inserts use app-level dedup on `source_hash` before INSERT.

## Stack

| Service | Port | Purpose |
|---|---|---|
| db | 5432 | App PostgreSQL |
| redis | 6379 | Cache + Celery broker (Airflow uses db 1) |
| backend | 8000 | FastAPI |
| frontend | 5173 | Vue 3 / Vite |
| airflow-webserver | 8080 | Airflow UI (profile `airflow`) |
| airflow-scheduler | — | (profile `airflow`) |
| airflow-worker | — | Celery worker (profile `airflow`) |

Start the stack:

```bash
cp .env.example .env
docker-compose up -d                      # backend + frontend + db + redis
docker-compose --profile airflow up -d    # add Airflow services
```

Or, if you already have a corporate Airflow, leave the `airflow` profile down
and set:

```env
AIRFLOW_USE_EXTERNAL=true
AIRFLOW_API_URL=https://airflow.corp/api/v1
AIRFLOW_USER=...
AIRFLOW_PASSWORD=...
```

…then deploy `shared/dags/` to the external Airflow.

## Authentication

- **SSO only.** Local password login is disabled by `sso_force=true` (seeded).
- First SSO login auto-creates the user and assigns the `superadmin` role
  (which has `ALL` access on every page).
- The mechanism for roles / RBAC is preserved but not used functionally.

## Adding a new flow

1. **Backend** — add a row in `seed_flows.py` (or via the `/flows` UI) with the
   right `parser_type`, `match_key_strategy` and `parser_config`. Add the
   target accounts.
2. **Parser** — if a new format is needed, drop a parser in
   `backend/app/services/parsers/{name}_parser.py` extending `BaseParser` and
   register it in `parsers/__init__.py`.
3. **DAG** — copy `shared/dags/ingest_atm.py`, change the `flow_code` and
   `schedule`. Set `is_paused=True` until validated.
4. **Frontend** — nothing to change: the operational view is generic and
   filters on `flow_id`.

## Reconciliation engine

Daily DAG `reconcile_daily` calls `POST /tasks/reconcile` which:

1. selects pending entries grouped by `(flow_id, reco_id, currency)`;
2. for each group whose `SUM(amount) = 0`, creates a `match_group(mode='auto')`
   and updates the entries to `status='matched'` with the group id;
3. records a `reconciliation_run` with counters.

**Manual force match** (UI):
- Must select ≥ 2 entries from the **same flow + currency**.
- If the sum is 0 → comment optional, otherwise mandatory.
- Creates a `match_group(mode='forced')` with the user's comment.

**Exclusion** (UI):
- Single entry, **mandatory reason**.
- Sets `status='excluded'` and inserts an `exclusion` row.

## Audit

- DB triggers attached to all reco tables (except the high-volume
  `reconciliation_entry`) write to `audit.audit_log`.
- UI actions (force, exclude, login, export…) are logged to
  `audit.ui_action_log` from the services.
- `audit_log.user_id` reads `current_setting('app.current_user_id')` which is
  set automatically by `AuditUserMiddleware` + `get_db()` via
  `SET LOCAL app.current_user_id = <id>` on every authenticated request.

## Known TODOs

- MT940 BCEE parser (`mt940_parser.py`) — stub; awaiting sample.
- Cobol amount sign / encoding (UTF-8 vs EBCDIC, leading sign vs overpunch) —
  configurable in `parser_config.encoding`; sample file required to confirm.
- ~~Wire `app.current_user_id` in the request middleware for full audit.~~ ✅ Done.
- Pytest + Vitest E2E coverage.
