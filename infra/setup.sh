#!/usr/bin/env bash
# FraudGraph - one-shot GCP setup.
# REVIEW BEFORE RUNNING: creates a bucket and a service account, enables APIs.
# Storage and BigQuery usage at this scale stays within the free tier, but the
# project needs billing enabled for Cloud Run / Vertex AI.
#
# Usage:
#   export PROJECT_ID=<your-project> BUCKET=<globally-unique-bucket-name>
#   bash infra/setup.sh
set -euo pipefail

: "${PROJECT_ID:?set PROJECT_ID}"
: "${BUCKET:?set BUCKET (globally unique, e.g. ${PROJECT_ID}-data)}"
REGION="${REGION:-asia-south1}"          # Mumbai; keep bucket + BQ colocated
DATASET="${DATASET:-fraudgraph}"
SA_NAME="${SA_NAME:-fraudgraph-app}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "${PROJECT_ID}"

echo "--- Enabling APIs (free) ---"
gcloud services enable bigquery.googleapis.com storage.googleapis.com \
  run.googleapis.com aiplatform.googleapis.com

echo "--- Data lake bucket (zones: raw/ curated/ results/) ---"
gcloud storage buckets create "gs://${BUCKET}" \
  --location="${REGION}" --uniform-bucket-level-access

echo "--- BigQuery dataset + tables ---"
bq --location="${REGION}" mk --dataset --force "${PROJECT_ID}:${DATASET}"
bq query --use_legacy_sql=false --project_id="${PROJECT_ID}" \
  < "$(dirname "$0")/schemas.sql"

echo "--- Service account (least-privilege roles) ---"
gcloud iam service-accounts create "${SA_NAME}" --display-name="FraudGraph app" || true
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.jobUser" --quiet
bq add-iam-policy-binding \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.dataEditor" \
  "${PROJECT_ID}:${DATASET}"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.objectAdmin"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/aiplatform.user" --quiet

cat <<EOF

Setup complete.
Next steps:
  1. Generate:  python generator/generate.py --edges 1e6 --out data/1m --seed 42
  2. Upload:    gcloud storage cp -r data/1m "gs://${BUCKET}/raw/1m"
  3. Load BQ:   bash infra/load_bq.sh   (or the bq load commands in that file)
EOF
