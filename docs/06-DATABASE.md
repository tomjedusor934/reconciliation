# Database Schema & Architecture

## Overview

PostgreSQL 16 with 3 schemas:

| Schema | Purpose | Tables |
|--------|---------|--------|
| `public` | Orchestro (Orchestro template) | users, roles, settings, sso_provider, ... |
| `reco` | Reconciliation data | flow, ingestion_run, reconciliation_entry, match_group, exclusion, ... |
| `audit` | Audit logs | audit_log, ui_action_log |

## Schema: `public` (Orchestro)

**Pre-existing from Orchestro template:**

```sql
CREATE TABLE "user" (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    full_name VARCHAR(255),
    hashed_password VARCHAR(255),
    is_active BOOLEAN DEFAULT TRUE,
    is_superuser BOOLEAN DEFAULT FALSE,
    blocked BOOLEAN DEFAULT FALSE,
    count_tentative INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
);

CREATE TABLE role (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    level VARCHAR(20),  -- Orchestro: "ADMIN", "USER", etc.
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
);

CREATE TABLE user_role (
    user_id INTEGER FOREIGN KEY REFERENCES "user"(id) ON DELETE CASCADE,
    role_id INTEGER FOREIGN KEY REFERENCES role(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, role_id),
);

CREATE TABLE accessible_page (
    id SERIAL PRIMARY KEY,
    path VARCHAR(255) NOT NULL,
    access_level VARCHAR(10) NOT NULL,  -- 'ALL', 'EDIT', 'NONE'
    role_id INTEGER FOREIGN KEY REFERENCES role(id) ON DELETE CASCADE,
);

CREATE TABLE setting (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT,
    description TEXT,
);
```

**Key settings** (seeded by `init_db.py`):
- `sso_enabled=true`
- `sso_force=true` (password login disabled)
- `sso_create_account_on_login=true`
- `sso_default_role_id=<superadmin role id>`

---

## Schema: `reco` (Reconciliation)

### Flow Table

```sql
CREATE TABLE reco.flow (
    id SERIAL PRIMARY KEY,
    code VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    is_active BOOLEAN DEFAULT FALSE,
    source_type VARCHAR(20) NOT NULL,  -- 'file', 'finacle_db', 'hybrid'
    parser_type VARCHAR(20) NOT NULL,  -- 'cobol_mosel', 'csv', 'xml', 'mt940', 'finacle_db'
    match_key_strategy VARCHAR(20) NOT NULL,  -- 'reco_id_amount', 'file_ref_amount', 'ref_amount'
    default_currency VARCHAR(3) DEFAULT 'EUR',
    inbox_subfolder VARCHAR(100),
    parser_config JSONB,  -- { "encoding": "utf-8", "allowed_event_types": [...], ... }
    finacle_query TEXT,
    finacle_connection_id INTEGER FOREIGN KEY REFERENCES source_connection(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
);

CREATE INDEX idx_flow_code ON reco.flow(code);
```

**Example row**:
```sql
INSERT INTO reco.flow VALUES (
    1,
    'atm',
    'ATM',
    'ATM cash withdrawals & deposits',
    TRUE,
    'file',
    'cobol_mosel',
    'reco_id_amount',
    'EUR',
    'atm',
    '{"encoding": "utf-8", "allowed_event_types": ["ARACCMVT", "ARCLHSAN", ...]}'::JSONB,
    NULL,
    NULL,
    NOW(),
    NOW()
);
```

### FlowAccount Table

```sql
CREATE TABLE reco.flow_account (
    id SERIAL PRIMARY KEY,
    flow_id INTEGER NOT NULL FOREIGN KEY REFERENCES reco.flow(id) ON DELETE CASCADE,
    account_number VARCHAR(50) NOT NULL,
    label VARCHAR(255),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(flow_id, account_number),
);

CREATE INDEX idx_flow_account_flow_id ON reco.flow_account(flow_id);
```

**Example**:
```sql
INSERT INTO reco.flow_account VALUES
(1, 1, '0010110035001', 'ATM DEPOSIT'),
(2, 1, '0010110040001', 'ATM WITHDRAWAL');
```

### SourceConnection Table

```sql
CREATE TABLE reco.source_connection (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    source_type VARCHAR(20),  -- 'finacle_db', 'salesforce', etc.
    dsn_template VARCHAR(500),  -- "postgresql://{user}:{password}@{host}:{port}/{database}"
    connection_params JSONB,  -- { "user": "admin", "password": "...", "host": "...", ... }
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
);
```

**Used for Finacle / external DB connections.**

### ReconciliationEntry Table (Live)

```sql
CREATE TABLE reco.reconciliation_entry (
    id BIGSERIAL PRIMARY KEY,
    flow_id INTEGER NOT NULL FOREIGN KEY REFERENCES reco.flow(id),
    ingestion_run_id INTEGER FOREIGN KEY REFERENCES reco.ingestion_run(id),
    reco_id VARCHAR(100),  -- Match key: used to group entries
    account VARCHAR(100),  -- Account number
    currency VARCHAR(3) NOT NULL,  -- Match key: EUR, USD, etc.
    amount NUMERIC(15, 2) NOT NULL,  -- Match key: transaction amount
    direction VARCHAR(10),  -- 'debit' or 'credit' (optional)
    value_date DATE NOT NULL,
    operation_date DATE,
    event_type VARCHAR(50),  -- e.g., 'ARACCMVT', 'ARCLHSAN'
    external_ref VARCHAR(255),  -- Original reference
    file_name VARCHAR(255),  -- Source file
    payload_raw JSONB,  -- Full parsed record (for audit)
    source_hash VARCHAR(64) NOT NULL UNIQUE,  -- SHA256 for dedup
    status VARCHAR(20) DEFAULT 'pending',  -- 'pending', 'matched', 'forced', 'excluded'
    match_group_id INTEGER FOREIGN KEY REFERENCES reco.match_group(id),
    matched_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
);

-- Indexes
CREATE INDEX USING brin ON reco.reconciliation_entry (value_date);
CREATE INDEX ON reco.reconciliation_entry (flow_id, status);
CREATE INDEX ON reco.reconciliation_entry (reco_id);
CREATE INDEX ON reco.reconciliation_entry (match_group_id);
```

### ReconciliationEntryEmargement Table

```sql
CREATE TABLE reco.reconciliation_entry_emargement (
    -- Same columns as reconciliation_entry, plus:
    emarged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,  -- When entry was moved to émargement
);
```

Entries are moved here once reconciliation is validated (matched, forced, or excluded). This happens:
1. **Inline** — immediately after matching/forcing/excluding (normal flow)
2. **Via DAG** — `archive_matched` sweeps remaining non-pending entries as a safety net

**Characteristics**:
- **Two-table design**: live table only holds pending entries, émargement holds validated entries
- **Simple PK** — `id` (BigInteger, autoincrement)
- **Table-wide UNIQUE** on `source_hash` (prevents re-ingestion)
- **Immutable** once inserted (status updates, but not reco_id/amount)

### MatchGroup Table

```sql
CREATE TABLE reco.match_group (
    id SERIAL PRIMARY KEY,
    flow_id INTEGER NOT NULL FOREIGN KEY REFERENCES reco.flow(id),
    reco_id VARCHAR(100),
    currency VARCHAR(3) NOT NULL,
    total NUMERIC(15, 2) NOT NULL,  -- SUM(amount) of matched entries
    mode VARCHAR(10) NOT NULL,  -- 'auto' (sum=0) or 'forced' (user override)
    comment TEXT,  -- For forced matches (mandatory if unbalanced)
    created_by_user_id INTEGER FOREIGN KEY REFERENCES "user"(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reconciliation_run_id INTEGER FOREIGN KEY REFERENCES reco.reconciliation_run(id),
);

CREATE INDEX idx_match_group_flow ON reco.match_group(flow_id);
CREATE INDEX idx_match_group_reco ON reco.match_group(reco_id);
```

**Row example** (auto match):
```sql
INSERT INTO reco.match_group VALUES (
    NULL,  -- id (auto-generated)
    1,     -- flow_id
    'TXN-12345',  -- reco_id
    'EUR',
    0.00,  -- sum of 3 entries: 100 + (-100) + 0
    'auto',
    NULL,
    NULL,
    NOW(),
    5
);
```

### Exclusion Table

```sql
CREATE TABLE reco.exclusion (
    id SERIAL PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES reco.reconciliation_entry(id),
    reason TEXT NOT NULL,  -- Mandatory reason for exclusion
    excluded_by_user_id INTEGER FOREIGN KEY REFERENCES "user"(id),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
);

CREATE INDEX idx_exclusion_entry ON reco.exclusion(entry_id);
```

**Notes**:
- FK to `reconciliation_entry(id)` — simple integer FK

### IngestionRun Table

```sql
CREATE TABLE reco.ingestion_run (
    id SERIAL PRIMARY KEY,
    flow_id INTEGER NOT NULL FOREIGN KEY REFERENCES reco.flow(id),
    source_file VARCHAR(255),
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    status VARCHAR(20),  -- 'pending', 'running', 'success', 'partial', 'failed'
    rows_in INTEGER,  -- Total rows in file
    rows_ok INTEGER,  -- Successfully inserted
    rows_ko INTEGER,  -- Validation errors
    rows_duplicate INTEGER,  -- Skipped (already exist)
    error TEXT,  -- Error message if failed
);

CREATE INDEX idx_ingestion_run_flow ON reco.ingestion_run(flow_id);
CREATE INDEX idx_ingestion_run_status ON reco.ingestion_run(status);
```

### ReconciliationRun Table

```sql
CREATE TABLE reco.reconciliation_run (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at TIMESTAMP,
    entries_scanned INTEGER,  -- Total pending entries checked
    groups_created INTEGER,  -- New match_group rows created
    entries_matched INTEGER,  -- Total entries flipped to 'matched'
    duration_ms INTEGER,  -- Execution time
    triggered_by VARCHAR(50),  -- 'auto_engine', 'manual_api', etc.
);
```

---

## Schema: `audit` (Audit Logs)

### AuditLog Table

```sql
CREATE TABLE audit.audit_log (
    id SERIAL PRIMARY KEY,
    table_name VARCHAR(100) NOT NULL,
    row_pk TEXT,  -- Primary key of modified row
    op VARCHAR(10) NOT NULL,  -- 'INSERT', 'UPDATE', 'DELETE'
    old_data JSONB,  -- Previous values (UPDATE/DELETE)
    new_data JSONB,  -- New values (INSERT/UPDATE)
    user_id INTEGER FOREIGN KEY REFERENCES "user"(id),
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
);

CREATE INDEX idx_audit_log_table ON audit.audit_log(table_name);
CREATE INDEX idx_audit_log_user ON audit.audit_log(user_id);
```

**Populated by**: DB triggers on insert/update/delete of reco tables

**Example** (user forces a match):
```json
{
  "table_name": "match_group",
  "row_pk": "42",
  "op": "INSERT",
  "old_data": null,
  "new_data": {
    "id": 42,
    "flow_id": 1,
    "reco_id": "TXN-12345",
    "mode": "forced",
    "created_by_user_id": 5,
    "comment": "Temporarily unbalanced, will reconcile next month"
  },
  "user_id": 5,
  "ts": "2025-05-01T10:30:00Z"
}
```

### UIActionLog Table

```sql
CREATE TABLE audit.ui_action_log (
    id SERIAL PRIMARY KEY,
    user_id INTEGER FOREIGN KEY REFERENCES "user"(id),
    action VARCHAR(50) NOT NULL,  -- 'force_match', 'exclude', 'login', 'export', etc.
    target_type VARCHAR(50),  -- 'match_group', 'entry', etc.
    target_id TEXT,  -- ID of affected resource
    details JSONB,  -- Additional context
    ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
);

CREATE INDEX idx_ui_action_log_user ON audit.ui_action_log(user_id);
CREATE INDEX idx_ui_action_log_action ON audit.ui_action_log(action);
```

**Populated by**: Services (`reconciliation_service.force_match()`, etc.)

**Example**:
```json
{
  "user_id": 5,
  "action": "force_match",
  "target_type": "match_group",
  "target_id": "42",
  "details": {
    "entry_ids": [123, 456, 789],
    "entry_count": 3,
    "sum": 0.00,
    "comment": "Auto-matched entries"
  },
  "ts": "2025-05-01T10:30:00Z"
}
```

---

## Initialization Sequence

### `init_db.py` — Orchestro Setup
1. Create public schema
2. Seed superadmin role with ALL access pages
3. Apply SSO defaults (sso_enabled, sso_force, sso_create_account_on_login)

### `init_reco.py` — Reco Setup
1. Create reco schema
2. Create audit schema + trigger function
3. Create per-table audit triggers (except reconciliation_entry)

### `seed_flows.py` — Flow Configuration
1. Idempotently seed 5 flows
2. Create accounts for each flow
3. ATM is active; others inactive

---

## Table Design Details

### Two-Table Design (Live + Émargement)

**Volume**: ~1M+ transactions/year

The `reconciliation_entry` table uses a **two-table design**:
- `reconciliation_entry` = **live table** — ingestion inserts here. Only pending entries remain.
- `reconciliation_entry_emargement` = **émargement table** — entries move here once reconciliation is done (matched/forced/excluded), with an `emarged_at` timestamp.

**How entries move**:
- **Inline** — immediately after matching, forcing, or excluding (normal flow)
- **Via DAG** — `archive_matched` sweeps any remaining non-pending entries as a safety net (03:30 UTC daily)

This keeps the live table small and fast for querying pending entries.

---

## Performance Tuning

### Indexes

**BRIN (Block Range Index)**:
- Excellent for append-only columns (value_date)
- Small memory footprint
- Fast sequential scans

**B-tree (Standard)**:
- flow_id, status (common WHERE clause)
- reco_id, match_group_id (lookups)

### Constraints

**UNIQUE (source_hash)**:
- Table-wide constraint (prevents duplicate ingestion)

**Simple PK**:
- `id` (BigInteger, autoincrement) — no composite key needed

### Connection Pool

**SQLAlchemy settings** (in `session.py`):
```python
pool_size=20  # Connections in pool
max_overflow=10  # Extra connections if needed
pool_pre_ping=True  # Verify connection before use
```

---

## Backup & Disaster Recovery

### Regular Backups

```bash
# Full backup
pg_dump -h localhost -U reconciliation_user reconciliation_db > backup.sql

# Compressed
pg_dump -h localhost -U reconciliation_user -Fc reconciliation_db > backup.dump
```

### Restore

```bash
psql -h localhost -U reconciliation_user reconciliation_db < backup.sql
```

### Archive Old Data

```bash
# Dump old émargement entries to cold storage (yearly)
pg_dump -h localhost -U reconciliation_user -Fc -t reconciliation_entry_emargement \
  reconciliation_db > emargement_archive_2024.dump

# Optionally delete old émargement entries after verifying backup
DELETE FROM reco.reconciliation_entry_emargement WHERE emarged_at < '2024-01-01';
```

---

## Common Queries (for Debugging)

### Count entries by status
```sql
SELECT status, COUNT(*) FROM reco.reconciliation_entry
WHERE value_date >= '2025-05-01'
GROUP BY status;
```

### Find unmatched entries for a flow
```sql
SELECT reco_id, SUM(amount) as total_amount, COUNT(*) as entry_count
FROM reco.reconciliation_entry
WHERE flow_id = 1 AND status = 'pending' AND value_date >= '2025-05-01'
GROUP BY reco_id
HAVING COUNT(*) > 1
ORDER BY total_amount DESC;
```

### Check table sizes
```sql
SELECT schemaname, tablename, pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename))
FROM pg_tables
WHERE schemaname = 'reco'
ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC;
```

### Audit trail for an entry
```sql
SELECT * FROM audit.audit_log
WHERE table_name = 'reconciliation_entry' AND row_pk = '12345'
ORDER BY ts;
```
