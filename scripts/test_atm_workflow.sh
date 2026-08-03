#!/bin/bash
# test_atm_workflow.sh — Full end-to-end test for the ATM (Kobol/MOSEL) flow.
# Usage: bash test_atm_workflow.sh [--mode server|default] [count] [matched_ratio]
#   --mode server    Use dataStaging paths (/POST_HOME/dataStaging/in|out)
#   --mode default   Use local shared/inbox paths (default)
set -e

# Parse --mode flag
MODE="default"
if [[ "$1" == "--mode" ]]; then
  MODE="$2"
  shift 2
fi

echo "================================"
echo "🚀 ATM Reconciliation Workflow Test (mode: $MODE)"
echo "================================"
echo ""

# Configuration
BACKEND_TOKEN="${RECO_BACKEND_INTERNAL_TOKEN:-change-me-internal-token}"
DB_CONTAINER="reconciliation-db"
DB_CMD="docker exec $DB_CONTAINER psql -U ${POSTGRES_USER:-reco_user} -d ${POSTGRES_DB:-reco_db}"
COUNT="${1:-100}"
MATCHED_RATIO="${2:-0.5}"
SEED="${3:-$(date +%s)}"

if [[ "$MODE" == "server" ]]; then
  BACKEND_URL="http://localhost:${BACKEND_PORT:-8010}"
  INBOX_DIR="/POST_HOME/dataStaging/in/mosel/reconciliation/atm"
else
  BACKEND_URL="http://localhost:8000"
  INBOX_DIR="./shared/inbox/atm"
fi

echo "📋 Configuration:"
echo "  Count:         $COUNT records"
echo "  Matched ratio: $MATCHED_RATIO"
echo "  Seed:          $SEED"
echo "  Inbox:         $INBOX_DIR"
echo ""

# ── Step 0: Verify ATM flow is active (it should be by default) ───────────────
echo "0️⃣  Checking ATM flow status..."
ATM_ACTIVE=$($DB_CMD -t -c "SELECT is_active FROM reco.flow WHERE code='atm'" | tr -d ' \n')
if [ "$ATM_ACTIVE" != "t" ]; then
  echo "   ⚠️  ATM flow is inactive — activating..."
  $DB_CMD -c "UPDATE reco.flow SET is_active = true WHERE code = 'atm';" 2>&1 | tail -1
fi
SOURCE_ACTIVE=$($DB_CMD -t -c "SELECT is_active FROM reco.flow_source WHERE code='cobol_file' AND flow_id=(SELECT id FROM reco.flow WHERE code='atm')" | tr -d ' \n')
if [ "$SOURCE_ACTIVE" != "t" ]; then
  echo "   ⚠️  cobol_file source is inactive — activating..."
  $DB_CMD -c "UPDATE reco.flow_source SET is_active = true WHERE code='cobol_file' AND flow_id=(SELECT id FROM reco.flow WHERE code='atm');" 2>&1 | tail -1
fi
echo "   ✅ ATM flow active, cobol_file source active"

# Step 1: Generate test files
echo ""
echo "1️⃣  Generating test ATM files..."
mkdir -p "$INBOX_DIR"
python3 generate_atm.py \
  --count "$COUNT" \
  --matched-ratio "$MATCHED_RATIO" \
  --output "$INBOX_DIR" \
  --date "$(date +%Y%m%d)" \
  --seed "$SEED"
echo "   ✅ Files generated"

# Step 2: Ingest
echo ""
echo "2️⃣  Triggering ingestion..."
INGEST_TMP=$(mktemp)
curl -s -X POST "$BACKEND_URL/api/v1/tasks/ingest/atm" \
  -H "X-Internal-Token: $BACKEND_TOKEN" -o "$INGEST_TMP"
cat "$INGEST_TMP" | python3 -c "import json,sys; d=json.load(sys.stdin); print(f\"   Response: ok={d.get('ok')}, runs={d.get('data',{}).get('count','?')}\")" 2>/dev/null || cat "$INGEST_TMP"
rm -f "$INGEST_TMP"
sleep 2

echo ""
echo "   Ingestion run details:"
$DB_CMD -c "
  SELECT ir.id, ir.rows_ok, ir.rows_ko, ir.rows_duplicate, ir.status,
         fs.code AS source_code
  FROM reco.ingestion_run ir
  LEFT JOIN reco.flow_source fs ON fs.id = ir.flow_source_id
  WHERE ir.flow_id = (SELECT id FROM reco.flow WHERE code='atm')
  ORDER BY ir.id DESC LIMIT 3;" 2>&1 | tail -8

# Step 3: Check pre-reconciliation state
echo ""
echo "3️⃣  Pre-reconciliation state:"
$DB_CMD -c "
  SELECT status, COUNT(*) as count 
  FROM reco.reconciliation_entry 
  WHERE flow_id = (SELECT id FROM reco.flow WHERE code='atm')
  GROUP BY status 
  ORDER BY status;" 2>&1 | tail -10

# Step 4: Reconcile
echo ""
echo "4️⃣  Triggering reconciliation engine..."
RECO_TMP=$(mktemp)
curl -s -X POST "$BACKEND_URL/api/v1/tasks/reconcile" \
  -H "X-Internal-Token: $BACKEND_TOKEN" -o "$RECO_TMP"
cat "$RECO_TMP" | python3 -c "import json,sys; d=json.load(sys.stdin); dd=d.get('data',{}); print(f\"   groups_created={dd.get('groups_created','?')}, entries_matched={dd.get('entries_matched','?')}\")" 2>/dev/null || cat "$RECO_TMP"
rm -f "$RECO_TMP"
sleep 2

# Step 5: Final results
echo ""
echo "5️⃣  Final reconciliation state:"
$DB_CMD -c "
  SELECT 
    'Total' as type, COUNT(*) as count 
  FROM reco.reconciliation_entry
  WHERE flow_id = (SELECT id FROM reco.flow WHERE code='atm')
  UNION ALL 
  SELECT 'Pending', COUNT(*) FROM reco.reconciliation_entry 
    WHERE flow_id = (SELECT id FROM reco.flow WHERE code='atm') AND status = 'PENDING'
  UNION ALL 
  SELECT 'Matched', COUNT(*) FROM reco.reconciliation_entry 
    WHERE flow_id = (SELECT id FROM reco.flow WHERE code='atm') AND status = 'MATCHED'
  UNION ALL
  SELECT 'Match Groups', COUNT(*) FROM reco.match_group
    WHERE flow_id = (SELECT id FROM reco.flow WHERE code='atm')
  ORDER BY type;" 2>&1 | tail -10

echo ""
EXPECTED_MATCHED=$(python3 -c "
count=$COUNT; ratio=$MATCHED_RATIO
n=int(count*ratio); n=n if n%2==0 else n-1
print(n)")
EXPECTED_PENDING=$((COUNT - EXPECTED_MATCHED))
echo "📊 Expected: ~$EXPECTED_MATCHED matched, ~$EXPECTED_PENDING pending (based on $MATCHED_RATIO matched ratio)"

echo ""
echo "================================"
echo "✅ ATM Workflow test complete!"
echo "================================"
echo ""
echo "📊 Quick commands to explore:"
echo "   # View match groups"
echo "   docker exec $DB_CONTAINER psql -U postgres -d app -c \\"
echo "     \"SELECT * FROM reco.match_group WHERE flow_id=(SELECT id FROM reco.flow WHERE code='atm') LIMIT 5;\""
echo ""
echo "   # View unmatched entries"
echo "   docker exec $DB_CONTAINER psql -U postgres -d app -c \\"
echo "     \"SELECT reco_id, amount, status FROM reco.reconciliation_entry WHERE status='PENDING' AND flow_id=(SELECT id FROM reco.flow WHERE code='atm') LIMIT 10;\""
echo ""
echo "   # Cleanup all ATM test data"
echo "   docker exec $DB_CONTAINER psql -U postgres -d app -c \\"
echo "     \"DELETE FROM reco.reconciliation_entry WHERE flow_id=(SELECT id FROM reco.flow WHERE code='atm');\""
echo ""
echo "   # Open frontend"
echo "   Open http://localhost:5173"

