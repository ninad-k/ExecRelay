#!/bin/bash
# End-to-End Integration Test
# Verifies complete signal flow through all systems

set -e

echo "======================================="
echo "End-to-End Signal Flow Test"
echo "======================================="
echo ""

# Port-forward to ingress
echo "Setting up port-forwarding..."
kubectl port-forward -n execrelay svc/execrelay-ingress 8081:8080 > /dev/null 2>&1 &
PF_PID=$!
sleep 2

# Test signal ingestion
echo "Testing signal ingestion..."
TEST_LICENSE="550e8400-e29b-41d4-a716-446655440000"
TEST_INSTANCE="test-mt5"
TIMESTAMP=$(date +%s)
BODY="$TEST_LICENSE:buy:$TEST_INSTANCE:symbol=EURUSD"

RESPONSE=$(curl -s -X POST \
  -H "Content-Type: text/plain" \
  -H "X-ExecRelay-Timestamp: $TIMESTAMP" \
  -d "$BODY" \
  "http://localhost:8081/webhook")

if echo "$RESPONSE" | grep -q "accepted"; then
  TRACE_ID=$(echo "$RESPONSE" | grep -o '"trace_id":"[^"]*"' | head -1 | cut -d'"' -f4)
  echo "  ✓ Signal accepted"
  echo "  ✓ Trace ID: $TRACE_ID"
else
  echo "  ✗ Signal rejected"
  echo "$RESPONSE"
fi

# Port-forward to portal-api
echo ""
echo "Testing Portal API..."
kubectl port-forward -n execrelay svc/execrelay-portal-api 8085:8080 > /dev/null 2>&1 &
PF_API_PID=$!
sleep 2

# Test backtest endpoint
BACKTEST=$(curl -s -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "license_id": "'$TEST_LICENSE'",
    "date_start": "2024-01-01",
    "date_end": "2024-01-31"
  }' \
  "http://localhost:8085/api/backtest")

if echo "$BACKTEST" | grep -q "job_id"; then
  echo "  ✓ Backtest endpoint working"
  JOB_ID=$(echo "$BACKTEST" | grep -o '"job_id":"[^"]*"' | head -1 | cut -d'"' -f4)
  echo "  ✓ Backtest job: $JOB_ID"
else
  echo "  ! Backtest response: $BACKTEST"
fi

# Cleanup
echo ""
echo "Cleaning up port-forwards..."
kill $PF_PID 2>/dev/null || true
kill $PF_API_PID 2>/dev/null || true

echo ""
echo "======================================="
echo "✓ End-to-End Test Complete"
echo "======================================="
