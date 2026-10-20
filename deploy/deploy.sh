#!/usr/bin/env bash
# DRE-Capital — GCP Cloud Run deployment.
#
# Storage: SQLite inside the container, so the service is pinned to ONE
# instance (every user sees the same database). The database is recreated on
# each deploy/restart (fresh demo data via ensure_admin); anything that must
# survive — the session key and the company Quo API key — lives in Secret
# Manager. Personal Quo keys saved in the UI reset with the database.
#
# Re-deploys are safe: env vars are UPDATED, never wiped. Only variables you
# export are changed; everything else keeps its current value.
#
#   PROJECT_ID=dre-capital bash deploy/deploy.sh                 # code-only redeploy
#   QUO_API_KEY=... PROJECT_ID=dre-capital bash deploy/deploy.sh  # also rotate the Quo key
#
# Optional exports: QUO_API_KEY, CALLING_PROVIDER, GOOGLE_CLIENT_ID,
# GOOGLE_CLIENT_SECRET, RENTCAST_API_KEY, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL,
# ADMIN_EMAIL, ADMIN_PASSWORD, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER.
#
# Prereqs: gcloud auth login, gcloud config set project <PROJECT_ID>
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
REGION="${REGION:-us-central1}"
SERVICE_NAME="dre-capital"
SESSION_SECRET="dre-capital-secret-key"
QUO_SECRET="dre-capital-quo-api-key"

echo "=== DRE-Capital GCP Deployment ==="
echo "Project:  $PROJECT_ID"
echo "Region:   $REGION"
echo "Service:  $SERVICE_NAME"
echo ""

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "ERROR: Set PROJECT_ID: gcloud config set project <your-project>"
  exit 1
fi

PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
RUNTIME_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
APP_BASE_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"

secret_exists() { gcloud secrets describe "$1" --project "$PROJECT_ID" >/dev/null 2>&1; }
service_exists() { gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; }

# ── 1. Enable APIs ────────────────────────────────────────────
echo "[1/4] Enabling APIs…"
gcloud services enable run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  --project "$PROJECT_ID" --quiet

# ── 2. Secrets ───────────────────────────────────────────────
echo "[2/4] Secrets…"
# Session key: create once. Rotating it on every deploy logged every user out.
if ! secret_exists "$SESSION_SECRET"; then
  SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32)
  printf '%s' "$SECRET_KEY" | gcloud secrets create "$SESSION_SECRET" --data-file=- --project "$PROJECT_ID" --quiet
fi

# Company Quo API key: stored only in Secret Manager, never in env/CLI history of the service.
if [ -n "${QUO_API_KEY:-}" ]; then
  if secret_exists "$QUO_SECRET"; then
    printf '%s' "$QUO_API_KEY" | gcloud secrets versions add "$QUO_SECRET" --data-file=- --project "$PROJECT_ID" --quiet
  else
    printf '%s' "$QUO_API_KEY" | gcloud secrets create "$QUO_SECRET" --data-file=- --project "$PROJECT_ID" --quiet
  fi
  echo "  Quo API key stored in Secret Manager ($QUO_SECRET)."
fi
SECRETS="SECRET_KEY=${SESSION_SECRET}:latest"
for s in "$SESSION_SECRET" "$QUO_SECRET"; do
  if secret_exists "$s"; then
    gcloud secrets add-iam-policy-binding "$s" --project "$PROJECT_ID" \
      --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretAccessor --quiet >/dev/null
  fi
done
if secret_exists "$QUO_SECRET"; then
  SECRETS="${SECRETS},QUO_API_KEY=${QUO_SECRET}:latest"
fi

# ── 3. Build container ────────────────────────────────────────
echo "[3/4] Building container…"
gcloud builds submit --tag "gcr.io/${PROJECT_ID}/${SERVICE_NAME}" \
  --project "$PROJECT_ID" --quiet

# ── 4. Deploy to Cloud Run ───────────────────────────────────
echo "[4/4] Deploying to Cloud Run…"
# Always-set values.
ENV_VARS="APP_BASE_URL=${APP_BASE_URL},DATABASE_URL=sqlite:///./dre_capital.db"
# First deploy gets default admin credentials; later deploys keep the current ones unless exported.
if ! service_exists; then
  ENV_VARS="${ENV_VARS},ADMIN_EMAIL=${ADMIN_EMAIL:-admin@dre-capital.com},ADMIN_PASSWORD=${ADMIN_PASSWORD:-capital2026}"
  ENV_VARS="${ENV_VARS},LLM_BASE_URL=${LLM_BASE_URL:-https://api.openai.com/v1},LLM_MODEL=${LLM_MODEL:-gpt-4o-mini}"
else
  for v in ADMIN_EMAIL ADMIN_PASSWORD LLM_BASE_URL LLM_MODEL; do
    if [ -n "${!v:-}" ]; then ENV_VARS="${ENV_VARS},${v}=${!v}"; fi
  done
fi
# Optional values: only changed when exported.
for v in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET RENTCAST_API_KEY LLM_API_KEY CALLING_PROVIDER \
         TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN TWILIO_FROM_NUMBER; do
  if [ -n "${!v:-}" ]; then ENV_VARS="${ENV_VARS},${v}=${!v}"; fi
done

gcloud run deploy "$SERVICE_NAME" \
  --image "gcr.io/${PROJECT_ID}/${SERVICE_NAME}" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi --cpu 1 \
  --min-instances 1 --max-instances 1 \
  --update-secrets "$SECRETS" \
  --update-env-vars "$ENV_VARS" \
  --project "$PROJECT_ID" --quiet

# ── Done ─────────────────────────────────────────────────────
echo ""
echo "=== Deployment complete! ==="
echo "URL: ${APP_BASE_URL}"
echo "Health: $(curl -fsS "${APP_BASE_URL}/api/health" 2>/dev/null || echo 'not responding yet')"
