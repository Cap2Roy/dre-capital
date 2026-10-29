#!/usr/bin/env bash
# DRE-Capital — GCP Cloud Run deployment.
#
# Storage: Cloud SQL PostgreSQL (instance dre-capital-db, private IP) when it exists —
# data survives deploys; the connection URL is a Secret Manager secret. Without that
# instance the app falls back to SQLite inside the container (reset on every deploy).
# The service stays pinned to ONE instance (in-process scheduler + live call bridges).
#
# Re-deploys are safe: env vars are UPDATED, never wiped. Only variables you
# export are changed; everything else keeps its current value.
#
#   PROJECT_ID=dre-capital bash deploy/deploy.sh                 # code-only redeploy
#   QUO_API_KEY=... PROJECT_ID=dre-capital bash deploy/deploy.sh  # also rotate the Quo key
#   BLAND_API_KEY=... PROJECT_ID=dre-capital bash deploy/deploy.sh # set the AI voice agent key
#
# Optional exports: QUO_API_KEY, BLAND_API_KEY, AI_VOICE, CALLING_PROVIDER, GOOGLE_CLIENT_ID,
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
# Optional API keys kept in Secret Manager: ENV_VAR=secret-name (uploaded only when exported).
# TWILIO_* / OPENAI_API_KEY / XAI_API_KEY (Grok: scraping, scripts, Grok Voice calls) are the default keys for agents tagged "default key". Claude's
# default key is set on the Agents page (not here, so a developer's own ANTHROPIC_API_KEY
# in the shell never ends up in production).
OPTIONAL_SECRETS="QUO_API_KEY=dre-capital-quo-api-key BLAND_API_KEY=dre-capital-bland-api-key GOOGLE_MAPS_API_KEY=dre-capital-maps-api-key TWILIO_ACCOUNT_SID=dre-capital-twilio-account-sid TWILIO_AUTH_TOKEN=dre-capital-twilio-auth-token TWILIO_FROM_NUMBER=dre-capital-twilio-from-number TWILIO_API_KEY_SID=dre-capital-twilio-api-key-sid TWILIO_API_KEY_SECRET=dre-capital-twilio-api-key-secret OPENAI_API_KEY=dre-capital-openai-api-key XAI_API_KEY=dre-capital-xai-api-key"

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
  aiplatform.googleapis.com \
  --project "$PROJECT_ID" --quiet
# Live AI agent (Gemini Live on Vertex AI) runs as the Cloud Run service account.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/aiplatform.user --condition=None --quiet >/dev/null

# ── 2. Secrets ───────────────────────────────────────────────
echo "[2/4] Secrets…"
# Session key: create once. Rotating it on every deploy logged every user out.
if ! secret_exists "$SESSION_SECRET"; then
  SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))" 2>/dev/null || openssl rand -hex 32)
  printf '%s' "$SECRET_KEY" | gcloud secrets create "$SESSION_SECRET" --data-file=- --project "$PROJECT_ID" --quiet
fi

# Optional API keys (company Quo key, Bland AI key): exporting one stores/rotates it in
# Secret Manager; it is never set as a plain env var on the service.
SECRETS="SECRET_KEY=${SESSION_SECRET}:latest"
grant() {
  gcloud secrets add-iam-policy-binding "$1" --project "$PROJECT_ID" \
    --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretAccessor --quiet >/dev/null
}
grant "$SESSION_SECRET"
for pair in $OPTIONAL_SECRETS; do
  var="${pair%%=*}"; secret="${pair#*=}"
  if [ -n "${!var:-}" ]; then
    if secret_exists "$secret"; then
      printf '%s' "${!var}" | gcloud secrets versions add "$secret" --data-file=- --project "$PROJECT_ID" --quiet
    else
      printf '%s' "${!var}" | gcloud secrets create "$secret" --data-file=- --project "$PROJECT_ID" --quiet
    fi
    echo "  ${var} stored in Secret Manager (${secret})."
  fi
  if secret_exists "$secret"; then
    grant "$secret"
    SECRETS="${SECRETS},${var}=${secret}:latest"
  fi
done

# Quo connection for Sona calls (OAuth tokens rotate on every refresh): the app writes each new
# token set as a secret version, and reads the latest at start — so "Connect Quo" survives deploys.
QUO_OAUTH_SECRET="dre-capital-quo-mcp-oauth"
if ! secret_exists "$QUO_OAUTH_SECRET"; then
  printf '{}' | gcloud secrets create "$QUO_OAUTH_SECRET" --data-file=- --project "$PROJECT_ID" --quiet
fi
grant "$QUO_OAUTH_SECRET"
gcloud secrets add-iam-policy-binding "$QUO_OAUTH_SECRET" --project "$PROJECT_ID" \
  --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretVersionManager --quiet >/dev/null
SECRETS="${SECRETS},QUO_MCP_OAUTH=${QUO_OAUTH_SECRET}:latest"

# Database: Cloud SQL Postgres on a private IP (org policy forbids public IPs). The app user's
# password is generated once; the full connection URL lives only in Secret Manager.
DB_INSTANCE="${DB_INSTANCE:-dre-capital-db}"
DB_NAME="dre"
DB_USER="dre_app"
DB_URL_SECRET="dre-capital-database-url"
DB_FLAGS=""
if gcloud sql instances describe "$DB_INSTANCE" --project "$PROJECT_ID" >/dev/null 2>&1; then
  if ! secret_exists "$DB_URL_SECRET"; then
    echo "  Creating database ${DB_NAME} and user ${DB_USER} on ${DB_INSTANCE}…"
    gcloud sql databases describe "$DB_NAME" --instance "$DB_INSTANCE" --project "$PROJECT_ID" >/dev/null 2>&1 || \
      gcloud sql databases create "$DB_NAME" --instance "$DB_INSTANCE" --project "$PROJECT_ID" --quiet
    DB_PASS=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
    if gcloud sql users list --instance "$DB_INSTANCE" --project "$PROJECT_ID" --format="value(name)" | grep -qx "$DB_USER"; then
      gcloud sql users set-password "$DB_USER" --instance "$DB_INSTANCE" --password "$DB_PASS" --project "$PROJECT_ID" --quiet
    else
      gcloud sql users create "$DB_USER" --instance "$DB_INSTANCE" --password "$DB_PASS" --project "$PROJECT_ID" --quiet
    fi
    DB_IP=$(gcloud sql instances describe "$DB_INSTANCE" --project "$PROJECT_ID" --format="value(ipAddresses[0].ipAddress)")
    printf 'postgresql+psycopg2://%s:%s@%s:5432/%s?sslmode=require' "$DB_USER" "$DB_PASS" "$DB_IP" "$DB_NAME" | \
      gcloud secrets create "$DB_URL_SECRET" --data-file=- --project "$PROJECT_ID" --quiet
    unset DB_PASS
  fi
  grant "$DB_URL_SECRET"
  SECRETS="${SECRETS},DATABASE_URL=${DB_URL_SECRET}:latest"
  # Direct VPC egress so the service reaches the database's private IP.
  DB_FLAGS="--network default --subnet default --vpc-egress private-ranges-only"
  # A plain DATABASE_URL env var (the old in-container SQLite) can't become a secret in place.
  if service_exists && gcloud run services describe "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" \
       --format="value(spec.template.spec.containers[0].env)" | grep -q "sqlite:///"; then
    echo "  Removing the old SQLite DATABASE_URL…"
    gcloud run services update "$SERVICE_NAME" --region "$REGION" --project "$PROJECT_ID" \
      --remove-env-vars DATABASE_URL --quiet >/dev/null
  fi
fi

# Daily scan trigger: Cloud Run throttles background CPU, so Cloud Scheduler calls the
# token-protected /api/scraper/auto/cron endpoint once a day (the scan runs inside that request).
CRON_SECRET="dre-capital-cron-token"
if ! secret_exists "$CRON_SECRET"; then
  python3 -c "import secrets; print(secrets.token_urlsafe(32), end='')" | \
    gcloud secrets create "$CRON_SECRET" --data-file=- --project "$PROJECT_ID" --quiet
fi
grant "$CRON_SECRET"
SECRETS="${SECRETS},CRON_TOKEN=${CRON_SECRET}:latest"

# ── 3. Build container ────────────────────────────────────────
echo "[3/4] Building container…"
gcloud builds submit --tag "gcr.io/${PROJECT_ID}/${SERVICE_NAME}" \
  --project "$PROJECT_ID" --quiet

# ── 4. Deploy to Cloud Run ───────────────────────────────────
echo "[4/4] Deploying to Cloud Run…"
# Always-set values.
ENV_VARS="APP_BASE_URL=${APP_BASE_URL},GOOGLE_CLOUD_PROJECT=${PROJECT_ID}"
# Without Cloud SQL, fall back to SQLite inside the container (data resets on every deploy).
if [ -z "$DB_FLAGS" ]; then ENV_VARS="${ENV_VARS},DATABASE_URL=sqlite:///./dre_capital.db"; fi
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
for v in GOOGLE_CLIENT_ID GOOGLE_CLIENT_SECRET RENTCAST_API_KEY LLM_API_KEY CALLING_PROVIDER AI_VOICE \
         VERTEX_LIVE_MODEL VERTEX_LIVE_VOICE VERTEX_LOCATION; do
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
  --timeout 900 \
  --update-secrets "$SECRETS" \
  --update-env-vars "$ENV_VARS" \
  $DB_FLAGS \
  --project "$PROJECT_ID" --quiet

# ── 5. Daily scan schedule (06:00 Central) ──────────────────
gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID" --quiet
CRON_JOB="dre-capital-daily-scan"
CRON_TOKEN_VALUE=$(gcloud secrets versions access latest --secret "$CRON_SECRET" --project "$PROJECT_ID")
CRON_ARGS=(--location "$REGION" --project "$PROJECT_ID" --schedule "0 6 * * *" --time-zone "America/Chicago"
  --uri "${APP_BASE_URL}/api/scraper/auto/cron" --http-method POST --attempt-deadline 900s --quiet)
if gcloud scheduler jobs describe "$CRON_JOB" --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud scheduler jobs update http "$CRON_JOB" "${CRON_ARGS[@]}" --update-headers "X-Cron-Token=${CRON_TOKEN_VALUE}" >/dev/null
else
  gcloud scheduler jobs create http "$CRON_JOB" "${CRON_ARGS[@]}" --headers "X-Cron-Token=${CRON_TOKEN_VALUE}" >/dev/null
fi
unset CRON_TOKEN_VALUE
echo "Daily scan: Cloud Scheduler job ${CRON_JOB} (06:00 America/Chicago)."

# ── Done ─────────────────────────────────────────────────────
echo ""
echo "=== Deployment complete! ==="
echo "URL: ${APP_BASE_URL}"
echo "Health: $(curl -fsS "${APP_BASE_URL}/api/health" 2>/dev/null || echo 'not responding yet')"
