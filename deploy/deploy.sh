#!/usr/bin/env bash
# DRE-Capital — GCP Cloud Run deployment
# Deploys the property pipeline to Google Cloud Run with Cloud SQL (Postgres).
#
# Prereqs: gcloud auth login, gcloud config set project <PROJECT_ID>
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="dre-capital"
DB_INSTANCE="dre-capital-db"
DB_NAME="dre_capital"
DB_USER="dre_app"
DB_PASSWORD="${DB_PASSWORD:-$(openssl rand -base64 24 | tr -d '/+=' | head -c 24)}"

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
echo "[1/6] Enabling APIs…"
gcloud services enable run.googleapis.com \
  sqladmin.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  --project "$PROJECT_ID" --quiet

# ── 2. Cloud SQL Postgres instance ───────────────────────────
echo "[2/6] Checking Cloud SQL instance…"
if ! gcloud sql instances describe "$DB_INSTANCE" --project "$PROJECT_ID" --quiet >/dev/null 2>&1; then
  echo "  Creating Cloud SQL Postgres instance ($DB_INSTANCE)…"
  gcloud sql instances create "$DB_INSTANCE" \
    --database-version=POSTGRES_16 \
    --tier=db-f1-micro \
    --region="$REGION" \
    --project "$PROJECT_ID" --quiet
else
  echo "  Instance exists."
fi

# Create database + user
gcloud sql databases create "$DB_NAME" --instance="$DB_INSTANCE" --project "$PROJECT_ID" --quiet 2>/dev/null || true
gcloud sql users create "$DB_USER" --instance="$DB_INSTANCE" \
  --password="$DB_PASSWORD" --project "$PROJECT_ID" --quiet 2>/dev/null || true

# Get connection name
CONN_NAME=$(gcloud sql instances describe "$DB_INSTANCE" --project "$PROJECT_ID" --format="value(connectionName)")
echo "  Connection: $CONN_NAME"

# ── 3. Secrets ───────────────────────────────────────────────
echo "[3/6] Storing secrets…"
SECRET_ID="dre-capital-db-url"
DB_URL="postgresql+psycopg2://${DB_USER}:${DB_PASSWORD}@/${DB_NAME}?host=/cloudsql/${CONN_NAME}"

echo "$DB_URL" | gcloud secrets create "$SECRET_ID" --data-file=- --project "$PROJECT_ID" --quiet 2>/dev/null || \
  echo "$DB_URL" | gcloud secrets versions add "$SECRET_ID" --data-file=- --project "$PROJECT_ID" --quiet

SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || echo "$(openssl rand -hex 32)")
echo "$SECRET_KEY" | gcloud secrets create "dre-capital-secret-key" --data-file=- --project "$PROJECT_ID" --quiet 2>/dev/null || \
  echo "$SECRET_KEY" | gcloud secrets versions add "dre-capital-secret-key" --data-file=- --project "$PROJECT_ID" --quiet

# ── 4. Build container ────────────────────────────────────────
echo "[4/6] Building container…"
gcloud builds submit --tag "gcr.io/${PROJECT_ID}/${SERVICE_NAME}" \
  --project "$PROJECT_ID" --quiet

# ── 5. Deploy to Cloud Run ───────────────────────────────────
echo "[5/6] Deploying to Cloud Run…"
gcloud run deploy "$SERVICE_NAME" \
  --image "gcr.io/${PROJECT_ID}/${SERVICE_NAME}" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi --cpu 1 \
  --min-instances 0 --max-instances 10 \
  --add-cloudsql-instances "$CONN_NAME" \
  --set-secrets "DATABASE_URL=dre-capital-db-url:latest" \
  --set-secrets "SECRET_KEY=dre-capital-secret-key:latest" \
  --set-env-vars "APP_BASE_URL=https://${SERVICE_NAME}-${REGION}.run.app" \
  --project "$PROJECT_ID" --quiet

# ── 6. Done ──────────────────────────────────────────────────
echo "[6/6] Deployment complete!"
echo ""
echo "URL: https://${SERVICE_NAME}-${REGION}.run.app"
echo ""
echo "Next steps:"
echo "  1. Set Twilio env: gcloud run services update $SERVICE_NAME --region $REGION \\"
echo "     --set-env-vars TWILIO_ACCOUNT_SID=...,TWILIO_AUTH_TOKEN=...,TWILIO_FROM_NUMBER=..."
echo "  2. Open the URL above and start your daily loop."
