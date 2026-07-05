#!/usr/bin/env bash
# Load one generated scale from GCS into BigQuery (free-tier load jobs).
# Usage:
#   export PROJECT_ID=<project> BUCKET=<bucket> SCALE=1m
#   bash infra/load_bq.sh
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${BUCKET:?set BUCKET}"
SCALE="${SCALE:-1m}"
DATASET="${DATASET:-fraudgraph}"

# --replace on transactions/accounts: reloading a scale swaps the active graph.
# ring_assignments appends (ground truth rows coexist with detection runs).
bq load --source_format=PARQUET --replace \
  "${PROJECT_ID}:${DATASET}.transactions" "gs://${BUCKET}/raw/${SCALE}/transactions/*.parquet"
bq load --source_format=PARQUET --replace \
  "${PROJECT_ID}:${DATASET}.accounts" "gs://${BUCKET}/raw/${SCALE}/accounts/*.parquet"
bq load --source_format=PARQUET \
  "${PROJECT_ID}:${DATASET}.ring_assignments" "gs://${BUCKET}/raw/${SCALE}/ring_assignments/*.parquet"

bq query --use_legacy_sql=false --project_id="${PROJECT_ID}" "
SELECT 'transactions' AS t, COUNT(*) AS rows FROM \`${DATASET}.transactions\`
UNION ALL SELECT 'accounts', COUNT(*) FROM \`${DATASET}.accounts\`
UNION ALL SELECT 'ring_assignments', COUNT(*) FROM \`${DATASET}.ring_assignments\`"
