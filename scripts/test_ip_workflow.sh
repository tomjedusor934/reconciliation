#!/bin/bash
# test_ip_workflow.sh — Full end-to-end test for the IP (MT940) flow.
# Usage: bash test_ip_workflow.sh [--mode server|default] [count] [matched_ratio]
#   --mode server    Use dataStaging paths (/POST_HOME/dataStaging/in|out)
#   --mode default   Use local shared/inbox paths (default)
set -e

# Parse --mode flag
MODE="default"
if [[ "$1" == "--mode" ]]; then
  MODE="$2"
  shift 2
fi

echo "========================================"
echo "🚀 IP (MT940) Reconciliation Workflow Test (mode: $MODE)"
echo "========================================"
echo ""

BACKEND_TOKEN="${RECO_BACKEND_INTERNAL_TOKEN:-change-me-internal-token}"
DB_CONTAINER="reconciliation-db"
DB_CMD="docker exec $DB_CONTAINER psql -U ${POSTGRES_USER:-reco_user} -d ${POSTGRES_DB:-reco_db}"
COUNT="${1:-100}"
MATCHED_RATIO="${2:-0.7}"
SEED="${3:-$(date +%s)}"

if [[ "$MODE" == "server" ]]; then
  BACKEND_URL="http://localhost:${BACKEND_PORT:-8010}"
  INBOX_DIR="/POST_HOME/dataStaging/in/bcee/reconciliation/ip"
else
  BACKEND_URL="http://localhost:8000"
  INBOX_DIR="./shared/inbox/ip"
fi

echo "📋 Configuration:"
echo "  Count:         $COUNT transactions"
echo "  Matched ratio: $MATCHED_RATIO"
echo "  Seed:          $SEED"
echo "  Inbox:         $INBOX_DIR"
echo ""

# ── Pre-flight checks ──────────────────────────────────────────────────────────
echo "🔍 Pre-flight checks..."
if ! curl -sf "$BACKEND_URL/health" > /dev/null 2>&1 && ! curl -sf "$BACKEND_URL/api/v1/status" > /dev/null 2>&1; then
  # Just try to reach the backend at all
  HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "$BACKEND_URL/api/v1/tasks/ingest/ip" \
    -H "X-Internal-Token: $BACKEND_TOKEN" -X POST 2>/dev/null || echo "000")
  if [ "$HTTP_CODE" = "000" ]; then
    echo "   ❌ Backend not reachable at $BACKEND_URL"
    exit 1
  fi
fi
echo "   ✅ Backend reachable"

# ── Step 0: Activate IP flow & source ─────────────────────────────────────────
echo ""
echo "0️⃣  Activating IP flow and mt940_file source..."
$DB_CMD -c "UPDATE reco.flow SET is_active = true WHERE code = 'ip';" 2>&1 | tail -1
$DB_CMD -c "UPDATE reco.flow_source SET is_active = true WHERE code = 'mt940_file' AND flow_id = (SELECT id FROM reco.flow WHERE code = 'ip');" 2>&1 | tail -1
echo "   ✅ IP flow and source activated"

# ── Step 1: Generate test MT940 files ─────────────────────────────────────────
echo ""
echo "1️⃣  Generating MT940 BCEE test files..."
python3 generate_mt940.py \
  --count "$COUNT" \
  --matched-ratio "$MATCHED_RATIO" \
  --output "$INBOX_DIR" \
  --date "$(date +%Y%m%d)" \
  --seed "$SEED"
echo "   ✅ MT940 file generated in $INBOX_DIR"

# ── Step 2: Ingest ────────────────────────────────────────────────────────────
echo ""
echo "2️⃣  Triggering ingestion for IP flow..."
INGEST_TMP=$(mktemp)
curl -s -X POST "$BACKEND_URL/api/v1/tasks/ingest/ip" \
  -H "X-Internal-Token: $BACKEND_TOKEN" -o "$INGEST_TMP"
cat "$INGEST_TMP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"   Response: ok={d.get('ok')}, runs={d.get('data',{}).get('count','?')}\")" 2>/dev/null || cat "$INGEST_TMP"
rm -f "$INGEST_TMP"
sleep 2

# Verify via DB
LAST_RUN=$($DB_CMD -t -c \
  "SELECT id, rows_ok, rows_ko, rows_duplicate, status FROM reco.ingestion_run ORDER BY id DESC LIMIT 1" \
  | tr -d ' ' | head -1)
echo "   ✅ Last run: $LAST_RUN"

# ── Step 3: Pre-reconciliation state ──────────────────────────────────────────
echo ""
echo "3️⃣  Pre-reconciliation state (IP flow):"
$DB_CMD -c "
  SELECT status, COUNT(*) AS count
  FROM reco.reconciliation_entry
  WHERE flow_id = (SELECT id FROM reco.flow WHERE code='ip')
  GROUP BY status ORDER BY status;" 2>&1 | tail -8

echo ""
echo "   Ingestion run details:"
$DB_CMD -c "
  SELECT ir.id, ir.rows_ok, ir.rows_ko, ir.rows_duplicate, ir.status,
         fs.code AS source_code
  FROM reco.ingestion_run ir
  LEFT JOIN reco.flow_source fs ON fs.id = ir.flow_source_id
  WHERE ir.flow_id = (SELECT id FROM reco.flow WHERE code='ip')
  ORDER BY ir.id DESC LIMIT 5;" 2>&1 | tail -10

# ── Step 4: Reconcile ─────────────────────────────────────────────────────────
echo ""
echo "4️⃣  Triggering reconciliation engine..."
RECO_TMP=$(mktemp)
curl -s -X POST "$BACKEND_URL/api/v1/tasks/reconcile" \
  -H "X-Internal-Token: $BACKEND_TOKEN" -o "$RECO_TMP"
cat "$RECO_TMP" | python3 -c "import json,sys; d=json.load(sys.stdin); dd=d.get('data',{}); print(f\"   groups_created={dd.get('groups_created','?')}, entries_matched={dd.get('entries_matched','?')}\")" 2>/dev/null || cat "$RECO_TMP"
rm -f "$RECO_TMP"
sleep 2

# ── Step 5: Final results ──────────────────────────────────────────────────────
echo ""
echo "5️⃣  Final reconciliation state (IP flow):"
$DB_CMD -c "
  SELECT 'Total'        AS type, COUNT(*) AS count FROM reco.reconciliation_entry WHERE flow_id = (SELECT id FROM reco.flow WHERE code='ip')
  UNION ALL
  SELECT 'Pending',     COUNT(*) FROM reco.reconciliation_entry WHERE flow_id = (SELECT id FROM reco.flow WHERE code='ip') AND status = 'PENDING'
  UNION ALL
  SELECT 'Matched',     COUNT(*) FROM reco.reconciliation_entry WHERE flow_id = (SELECT id FROM reco.flow WHERE code='ip') AND status = 'MATCHED'
  UNION ALL
  SELECT 'Match Groups',COUNT(*) FROM reco.match_group           WHERE flow_id = (SELECT id FROM reco.flow WHERE code='ip')
  ORDER BY type;" 2>&1 | tail -10

echo ""
EXPECTED_MATCHED=$(python3 -c "
count=$COUNT; ratio=$MATCHED_RATIO
n=int(count*ratio); n=n if n%2==0 else n-1
print(n)")
EXPECTED_PENDING=$((COUNT - EXPECTED_MATCHED))
echo "📊 Expected: ~$EXPECTED_MATCHED matched, ~$EXPECTED_PENDING pending (based on $MATCHED_RATIO matched ratio)"

echo ""
echo "========================================"
echo "✅ IP Workflow test complete!"
echo "========================================"
echo ""
echo "💡 Tip: Re-activate Webripost/float_ip flows only when needed."
echo "   To clean up: docker exec $DB_CONTAINER psql -U postgres -d app -c \\"
echo "     \"DELETE FROM reco.reconciliation_entry WHERE flow_id=(SELECT id FROM reco.flow WHERE code='ip');\""
echo ""
echo "   Open http://localhost:5173 to view results in the UI"
