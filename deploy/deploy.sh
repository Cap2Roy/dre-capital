#!/usr/bin/env bash
# DRE-Capital — GCP Cloud Run deployment
# Deploys the property pipeline to Google Cloud Run.
#
# Uses SQLite (in-container) by default — sufficient for demo / small team.
# For production with Cloud SQL Postgres, set USE_CLOUD_SQL=true and ensure
# your project allows public IP or has a VPC connector for private IP.
#
# Prereqs: gcloud auth login, gcloud config set project <PROJECT_ID>
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="dre-capital"

echo "=== DRE-Capital GCP Deployment ==="
echo "Project:  $PROJECT_ID"
echo "Region:   $REGION"
echo "Service:  $SERVICE_NAME"
echo ""

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "ERROR: Set PROJECT_ID: gcloud config set project <your-project>"
  exit 1
fi

# ── 1. Enable APIs ────────────────────────────────────────────
echo "[1/4] Enabling APIs…"
gcloud services enable run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  --project "$PROJECT_ID" --quiet

# ── 2. Secrets ───────────────────────────────────────────────
echo "[2/4] Storing secrets…"
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || echo "$(openssl rand -hex 32)")
echo "$SECRET_KEY" | gcloud secrets create "dre-capital-secret-key" --data-file=- --project "$PROJECT_ID" --quiet 2>/dev/null || \
  echo "$SECRET_KEY" | gcloud secrets versions add "dre-capital-secret-key" --data-file=- --project "$PROJECT_ID" --quiet

# ── 3. Build container ────────────────────────────────────────
echo "[3/4] Building container…"
gcloud builds submit --tag "gcr.io/${PROJECT_ID}/${SERVICE_NAME}" \
  --project "$PROJECT_ID" --quiet

# ── 4. Deploy to Cloud Run ───────────────────────────────────
echo "[4/4] Deploying to Cloud Run…"
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@dre-capital.com}"
ADMIN_PASSWORD="${ADMIN_PASSWORD:-capital2026}"
gcloud run deploy "$SERVICE_NAME" \
  --image "gcr.io/${PROJECT_ID}/${SERVICE_NAME}" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi --cpu 1 \
  --min-instances 1 --max-instances 10 \
  --set-secrets "SECRET_KEY=dre-capital-secret-key:latest" \
  --set-env-vars "APP_BASE_URL=https://${SERVICE_NAME}-${REGION}.run.app" \
  --set-env-vars "ADMIN_EMAIL=${ADMIN_EMAIL}" \
  --set-env-vars "ADMIN_PASSWORD=${ADMIN_PASSWORD}" \
  --set-env-vars "DATABASE_URL=sqlite:///./dre_capital.db" \
  --project "$PROJECT_ID" --quiet

# ── Done ─────────────────────────────────────────────────────
echo ""
echo "=== Deployment complete! ==="
echo "URL: https://${SERVICE_NAME}-${REGION}.run.app"
echo ""
echo "Login: ${ADMIN_EMAIL} / ${ADMIN_PASSWORD}"
echo ""
echo "Next steps:"
echo "  1. Set Twilio env: gcloud run services update $SERVICE_NAME --region $REGION \\"
echo "     --set-env-vars TWILIO_ACCOUNT_SID=...,TWILIO_AUTH_TOKEN=...,TWILIO_FROM_NUMBER=..."
echo "  2. Open the URL above and start your daily loop."
