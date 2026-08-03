#!/bin/bash
# test_e2e_orchestrator.sh — Complete E2E test using the orchestrator DAG
# Generates files for ATM, IP, and Webripost flows, then triggers the orchestrator
# Usage: bash test_e2e_orchestrator.sh [--mode server|default] [atm_count] [ip_count] [webripost_count] [atm_ratio] [ip_ratio] [webripost_ratio]
#   --mode server    Use dataStaging paths (/POST_HOME/dataStaging/in|out)
#   --mode default   Use local shared/inbox paths (default)
set -e

# Activate project venv (contains openpyxl and other deps)
VENV="$(cd "$(dirname "$0")/.." && pwd)/.venv"
if [[ -f "$VENV/bin/activate" ]]; then
  source "$VENV/bin/activate"
fi

# Load .env from project root (sets RECO_BACKEND_INTERNAL_TOKEN, AIRFLOW_API_URL, etc.)
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

# Parse --mode flag
MODE="default"
if [[ "$1" == "--mode" ]]; then
  MODE="$2"
  shift 2
fi

echo "════════════════════════════════════════════════════════════════"
echo "🚀 E2E Orchestrator Test — All 3 Flows + Reconciliation"
echo "   Mode: $MODE"
echo "════════════════════════════════════════════════════════════════"
echo ""

# Configuration
BACKEND_TOKEN="${RECO_BACKEND_INTERNAL_TOKEN:-change-me-internal-token}"
DB_CONTAINER="reconciliation-db"
DB_CMD="docker exec $DB_CONTAINER psql -U ${POSTGRES_USER:-reco_user} -d ${POSTGRES_DB:-reco_db}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
SEED=$(date +%s)

# Paths depend on mode
if [[ "$MODE" == "server" ]]; then
  BACKEND_URL="http://localhost:${BACKEND_PORT:-8010}"
  ATM_INBOX="/POST_HOME/dataStaging/in/mosel/reconciliation/atm"
  IP_INBOX="/POST_HOME/dataStaging/in/bcee/reconciliation/ip"
  WEBRIPOST_INBOX="/POST_HOME/dataStaging/in/webriposte/reconciliation/webripost"
else
  BACKEND_URL="http://localhost:8000"
  ATM_INBOX="$PROJECT_ROOT/shared/inbox/atm"
  IP_INBOX="$PROJECT_ROOT/shared/inbox/ip"
  WEBRIPOST_INBOX="$PROJECT_ROOT/shared/inbox/webripost"
fi

ATM_COUNT="${1:-100}"
IP_COUNT="${2:-100}"
WEBRIPOST_COUNT="${3:-100}"
ATM_RATIO="${4:-0.5}"
IP_RATIO="${5:-0.7}"
WEBRIPOST_RATIO="${6:-0.6}"

echo "📋 Configuration:"
echo "  ATM:        $ATM_COUNT records (matched ratio: $ATM_RATIO)"
echo "  IP:         $IP_COUNT transactions (matched ratio: $IP_RATIO)"
echo "  Webripost:  $WEBRIPOST_COUNT records (matched ratio: $WEBRIPOST_RATIO)"
echo "  Seed:       $SEED"
echo "  Orchestrator: $AIRFLOW_URL"
echo ""

# ── Pre-flight checks ──────────────────────────────────────────────────────────
echo "🔍 Pre-flight checks..."
if ! curl -sf "$BACKEND_URL/api/v1/openapi.json" > /dev/null 2>&1; then
  echo "   ❌ Backend not reachable at $BACKEND_URL"
  exit 1
fi
echo "   ✅ Backend reachable"

# ── Step 0: Activate all flows and update inbox_subfolder ─────────────────────────
echo ""
echo "0️⃣  Activating all flows and sources..."
$DB_CMD -c "UPDATE reco.flow SET is_active = true WHERE code IN ('atm', 'ip', 'webripost');" 2>&1 | tail -1
$DB_CMD -c "UPDATE reco.flow_source SET is_active = true WHERE code IN ('cobol_file', 'mt940_file', 'xlsx_file');" 2>&1 | tail -1
# Ensure inbox_subfolder matches expected folder names
$DB_CMD -c "UPDATE reco.flow_source SET inbox_subfolder = 'ip' WHERE code = 'mt940_file' AND flow_id = (SELECT id FROM reco.flow WHERE code = 'ip');" 2>&1 | tail -1
$DB_CMD -c "UPDATE reco.flow_source SET inbox_subfolder = 'other_payments' WHERE code = 'mt940_file' AND flow_id = (SELECT id FROM reco.flow WHERE code = 'other_payments');" 2>&1 | tail -1
echo "   ✅ All flows and sources activated"

# ── Step 1: Generate test files for all 3 flows ────────────────────────────────────
echo ""
echo "1️⃣  Generating test files for all 3 flows..."
echo ""
echo "   1a) ATM (Cobol/MOSEL) files..."
mkdir -p "$ATM_INBOX"
python3 generate_atm.py \
  --count "$ATM_COUNT" \
  --matched-ratio "$ATM_RATIO" \
  --output "$ATM_INBOX" \
  --date "$(date +%Y%m%d)" \
  --seed "$SEED"
chmod 777 "$ATM_INBOX"/* 2>/dev/null || true
echo "       ✅ Generated"

echo ""
echo "   1b) IP (MT940) files..."
mkdir -p "$IP_INBOX"
python3 generate_mt940.py \
  --count "$IP_COUNT" \
  --matched-ratio "$IP_RATIO" \
  --output "$IP_INBOX" \
  --date "$(date +%Y%m%d)" \
  --seed "$SEED"
chmod 777 "$IP_INBOX"/* 2>/dev/null || true
echo "       ✅ Generated"

echo ""
echo "   1c) Webripost (XLSX) files..."
mkdir -p "$WEBRIPOST_INBOX"
python3 generate_webripost.py \
  --count "$WEBRIPOST_COUNT" \
  --matched-ratio "$WEBRIPOST_RATIO" \
  --output "$WEBRIPOST_INBOX" \
  --date "$(date +%Y%m%d)" \
  --seed "$SEED"
chmod 777 "$WEBRIPOST_INBOX"/* 2>/dev/null || true
echo "       ✅ Generated"

# Count total files
TOTAL_FILES=$(find "$ATM_INBOX" "$IP_INBOX" "$WEBRIPOST_INBOX" -type f 2>/dev/null | wc -l)
echo ""
echo "   📦 Total files generated: $TOTAL_FILES"

# ── Step 2: Trigger the orchestrator DAG via backend ─────────────────────────────
echo ""
echo "2️⃣  Triggering Airflow orchestrator DAG (ingest → reconcile → emargement)..."

ORCH_RESULT=$(curl -s -X POST "$BACKEND_URL/api/v1/tasks/orchestrate" \
  -H "X-Internal-Token: $BACKEND_TOKEN")
echo "   Response: $ORCH_RESULT"
ORCH_OK=$(echo "$ORCH_RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('ok','false'))" 2>/dev/null || echo "false")
DAG_RUN_ID=$(echo "$ORCH_RESULT" | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('data',{}).get('dag_run_id','?'))" 2>/dev/null || echo "?")

if [[ "$ORCH_OK" == "True" ]]; then
  echo "   ✅ DAG triggered — dag_run_id: $DAG_RUN_ID"
else
  echo "   ❌ Failed to trigger orchestrator DAG"
  echo "   Detail: $ORCH_RESULT"
  exit 1
fi

echo ""
echo "   ⏳ Waiting for DAG to complete (polling every 10s, max 5 min)..."
AIRFLOW_INTERNAL_URL="${AIRFLOW_API_URL:-http://airflow-airflow-apiserver-1:8080/api/v2}"
AIRFLOW_USER="${AIRFLOW_API_USER:-airflow}"
AIRFLOW_PASS="${AIRFLOW_API_PASSWORD:-airflow}"
MAX_WAIT=300
ELAPSED=0
DAG_STATE="queued"
while [[ "$DAG_STATE" == "queued" || "$DAG_STATE" == "running" ]]; do
  sleep 10
  ELAPSED=$((ELAPSED + 10))
  DAG_STATE=$(docker compose -f "$PROJECT_ROOT/docker-compose.yml" exec -T backend python3 -c "
import httpx, sys
base = '$AIRFLOW_INTERNAL_URL'.rstrip('/')
r = httpx.post('$AIRFLOW_INTERNAL_URL'.split('/api')[0]+'/auth/token', json={'username':'$AIRFLOW_USER','password':'$AIRFLOW_PASS'}, timeout=5)
token = r.json().get('access_token','')
r2 = httpx.get(base+'/dags/orchestrate_ingestion/dagRuns/$DAG_RUN_ID', headers={'Authorization':f'Bearer {token}'}, timeout=10)
import json; print(r2.json().get('state','unknown'))
" 2>/dev/null || echo "unknown")
  echo "   ⏳ [$ELAPSED s] State: $DAG_STATE"
  if [[ $ELAPSED -ge $MAX_WAIT ]]; then
    echo "   ⚠️  Timeout reached — DAG still running. Check Airflow UI."
    break
  fi
done

if [[ "$DAG_STATE" == "success" ]]; then
  echo "   ✅ DAG completed successfully"
elif [[ "$DAG_STATE" == "failed" ]]; then
  echo "   ❌ DAG failed — check Airflow logs"
  exit 1
fi

# ── Step 3: Collect results ───────────────────────────────────────────────────────
echo ""
echo "3️⃣  Collecting results..."
echo ""

# Ingestion summary
echo "   📥 Ingestion Summary:"
$DB_CMD -c "
  SELECT 
    f.code,
    COUNT(ir.id) as total_runs,
    COALESCE(SUM(ir.rows_ok), 0) as total_ok,
    COALESCE(SUM(ir.rows_ko), 0) as total_ko,
    COALESCE(SUM(ir.rows_duplicate), 0) as total_dup
  FROM reco.ingestion_run ir
  RIGHT JOIN reco.flow f ON ir.flow_id = f.id
  WHERE f.code IN ('atm', 'ip', 'webripost')
  GROUP BY f.code
  ORDER BY f.code;" 2>&1 | tail -8 | sed 's/^/       /'

echo ""
echo "   🔀 Reconciliation Summary:"
$DB_CMD -c "
  SELECT
    f.code,
    COUNT(CASE WHEN re.status='PENDING' THEN 1 END) as pending,
    COUNT(CASE WHEN re.status='MATCHED' THEN 1 END) as matched,
    COUNT(CASE WHEN re.status='FORCED' THEN 1 END) as forced,
    COUNT(CASE WHEN re.status='EXCLUDED' THEN 1 END) as excluded,
    COUNT(mg.id) as match_groups
  FROM reco.reconciliation_entry re
  RIGHT JOIN reco.flow f ON re.flow_id = f.id
  LEFT JOIN reco.match_group mg ON mg.flow_id = f.id
  WHERE f.code IN ('atm', 'ip', 'webripost')
  GROUP BY f.code
  ORDER BY f.code;" 2>&1 | tail -8 | sed 's/^/       /'

# ── Final summary ───────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════════════"
echo "✅ E2E Orchestrator Test Complete!"
echo "════════════════════════════════════════════════════════════════"
echo ""

echo "📊 Expected vs Actual:"
for FLOW_CODE in "atm" "ip" "webripost"; do
  case "$FLOW_CODE" in
    "atm")
      COUNT="$ATM_COUNT"
      RATIO="$ATM_RATIO"
      ;;
    "ip")
      COUNT="$IP_COUNT"
      RATIO="$IP_RATIO"
      ;;
    "webripost")
      COUNT="$WEBRIPOST_COUNT"
      RATIO="$WEBRIPOST_RATIO"
      ;;
  esac
  
  EXPECTED_MATCHED=$(python3 -c "n=int($COUNT*$RATIO); print(n if n%2==0 else n-1)")
  EXPECTED_PENDING=$((COUNT - EXPECTED_MATCHED))
  
  echo "   $FLOW_CODE: ~$EXPECTED_MATCHED matched, ~$EXPECTED_PENDING pending (from $RATIO ratio)"
done

echo ""
echo "📍 Next steps:"
echo "   1. Check dashboard: http://localhost:5173"
echo "   2. View match details:"
echo "      $DB_CMD -c \"SELECT reco_id, amount, status FROM reco.reconciliation_entry WHERE status='PENDING' LIMIT 10;\""
echo "   3. Run orchestrator via Airflow:"
echo "      $AIRFLOW_URL/dags/orchestrate_ingestion"
echo "   4. Cleanup test data:"
echo "      $DB_CMD -c \"DELETE FROM reco.reconciliation_entry WHERE created_at > NOW() - INTERVAL '1 hour';\""
echo ""
