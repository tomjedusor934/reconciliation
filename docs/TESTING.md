# E2E Testing Guide

## Quick Start

### Run Full E2E Test with Orchestrator

```bash
bash test_e2e_orchestrator.sh [atm_count] [ip_count] [webripost_count] [atm_ratio] [ip_ratio] [webripost_ratio]
```

**Examples:**

```bash
# Default: 100 records per flow, various match ratios
bash test_e2e_orchestrator.sh

# Custom counts: 500 ATM, 300 IP, 200 Webripost
bash test_e2e_orchestrator.sh 500 300 200

# Custom counts + match ratios
bash test_e2e_orchestrator.sh 200 200 200 0.8 0.9 0.7
```

### Individual Flow Tests

Each flow has its own standalone test script:

```bash
# ATM (Cobol/MOSEL)
bash test_atm_workflow.sh [count] [matched_ratio]
bash test_atm_workflow.sh 150 0.6

# IP (MT940)
bash test_ip_workflow.sh [count] [matched_ratio]
bash test_ip_workflow.sh 200 0.75

# Webripost (XLSX)
bash test_webripost_workflow.sh [count] [matched_ratio]
bash test_webripost_workflow.sh 100 0.5
```

## What the E2E Test Does

1. **Pre-flight Checks**
   - Verifies backend is reachable
   - Checks Airflow orchestrator DAG exists

2. **Activation**
   - Activates all 3 flows (ATM, IP, Webripost)
   - Activates all source types (kobol_file, mt940_file, xlsx_file)

3. **File Generation**
   - Generates synthetic test files for each flow
   - Each file is parameterized for match ratio
   - Files placed in `shared/inbox/{atm,mt940_ip,webripost}/`

4. **Ingestion Orchestration**
   - Attempts to trigger `orchestrate_ingestion` DAG via Airflow API
   - Falls back to direct backend API calls if Airflow unavailable
   - Ingests all active flows automatically

5. **Results Collection**
   - Displays ingestion summary (rows_ok, rows_ko, duplicates)
   - Shows reconciliation summary (pending, matched, forced, excluded)
   - Compares expected vs actual based on match ratio

## Expected Results

For a test with N records and R matched ratio:
- **Matched pairs**: N × R (rounded to even number)
- **Pending/Unmatched**: N - (Matched pairs)

Example:
```
ATM: 100 records, 0.5 ratio → ~50 matched pairs, ~50 pending
IP:  200 records, 0.7 ratio → ~140 matched pairs, ~60 pending
Webripost: 80 records, 0.6 ratio → ~48 matched pairs, ~32 pending
```

## Database Queries

### View ingestion runs
```bash
docker exec reconciliation-db-1 psql -U postgres -d app -c \
  "SELECT flow_id, COUNT(*) as runs, SUM(rows_ok) as total_ok FROM reco.ingestion_run GROUP BY flow_id;"
```

### View pending entries
```bash
docker exec reconciliation-db-1 psql -U postgres -d app -c \
  "SELECT reco_id, account, amount, status FROM reco.reconciliation_entry WHERE status='PENDING' LIMIT 20;"
```

### View match groups
```bash
docker exec reconciliation-db-1 psql -U postgres -d app -c \
  "SELECT mg.id, mg.reco_id, mg.currency, mg.total, COUNT(re.id) as entries FROM reco.match_group mg LEFT JOIN reco.reconciliation_entry re ON re.match_group_id = mg.id GROUP BY mg.id LIMIT 10;"
```

### Cleanup test data
```bash
docker exec reconciliation-db-1 psql -U postgres -d app -c \
  "DELETE FROM reco.reconciliation_entry WHERE created_at > NOW() - INTERVAL '1 hour';"
```

## Orchestrator DAG

The orchestrator DAG (`orchestrate_ingestion`) can be triggered:

1. **Via Airflow UI**: http://localhost:8080/dags/orchestrate_ingestion
2. **Via Airflow API**:
   ```bash
   curl -u airflow:airflow -X POST \
     http://localhost:8080/api/v1/dags/orchestrate_ingestion/dagRuns \
     -H "Content-Type: application/json" \
     -d '{"conf": {}}'
   ```
3. **Via test script**: Automatically handled by `test_e2e_orchestrator.sh`

## Troubleshooting

### "Backend not reachable"
- Ensure `docker-compose up -d` is running
- Check backend logs: `docker-compose logs backend`

### "DAG trigger failed"
- This is normal if Airflow is not running or scheduler is stopped
- The test will fall back to direct backend API calls

### Files not ingested
- Check file permissions: `ls -la shared/inbox/*/`
- Verify parser types are correct for each file
- Check ingestion logs: `docker-compose logs backend | grep -i "ingest\|parse\|error"`

### Reconciliation not running
- Ensure `auto_reconcile` is enabled in the backend
- Check reconciliation logs: `docker-compose logs backend | grep -i "reconcile"`

## Docker Health

After running tests, verify Docker is still healthy:

```bash
docker-compose ps
docker-compose logs --tail=20 backend
```

All containers should show "Up" status. The backend should show no error stack traces in recent logs.
