#!/usr/bin/env bash
# Deploy CloudFinOps Sentinel to Cloud Run, with Cloud Scheduler driving it.
#
# Prerequisites: gcloud CLI, authenticated as a principal that can enable APIs
# and grant IAM. Run from the repository root:  ./deploy/deploy.sh
set -euo pipefail

# Read .env first, so the values that already work on a laptop reach Cloud Run
# instead of being retyped at a prompt. Sourced before anything uses them.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi


PROJECT="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-cloudfinops-sentinel}"
SA_NAME="sentinel-agent"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

[ -n "$PROJECT" ] || { echo "Set PROJECT_ID or run: gcloud config set project <id>"; exit 1; }
echo "▸ project=$PROJECT region=$REGION service=$SERVICE"

echo "▸ Enabling APIs"
gcloud services enable \
  run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  firestore.googleapis.com monitoring.googleapis.com compute.googleapis.com \
  cloudscheduler.googleapis.com secretmanager.googleapis.com \
  cloudtrace.googleapis.com \
  --project="$PROJECT"

echo "▸ Service account"
gcloud iam service-accounts create "$SA_NAME" \
  --display-name="CloudFinOps Sentinel" --project="$PROJECT" 2>/dev/null || true

# Least privilege: read everything it audits, write only what it remediates.
for ROLE in \
  roles/run.viewer roles/run.admin \
  roles/monitoring.viewer \
  roles/compute.viewer roles/compute.storageAdmin \
  roles/artifactregistry.reader \
  roles/datastore.user \
  roles/cloudtrace.agent \
  roles/logging.logWriter \
  roles/aiplatform.user \
  roles/bigquery.jobUser roles/bigquery.dataViewer
do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:${SA}" --role="$ROLE" --condition=None --quiet >/dev/null
  echo "   granted $ROLE"
done

echo "▸ Firestore (skipped if it already exists)"
gcloud firestore databases create --location="$REGION" --project="$PROJECT" 2>/dev/null || true

echo "▸ Dashboard token → Secret Manager"
# The dashboard can delete disks. It is never deployed without a credential.
if ! gcloud secrets describe sentinel-dashboard-token --project="$PROJECT" >/dev/null 2>&1; then
  TOKEN="$(openssl rand -base64 32 | tr -d '/+=' | head -c 40)"
  printf '%s' "$TOKEN" | gcloud secrets create sentinel-dashboard-token \
    --data-file=- --project="$PROJECT"
  echo "   generated — save this, it unlocks the dashboard:"
  echo "   $TOKEN"
fi
gcloud secrets add-iam-policy-binding sentinel-dashboard-token \
  --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor \
  --project="$PROJECT" --quiet >/dev/null

echo "▸ Gemini API key → Secret Manager"
if ! gcloud secrets describe gemini-api-key --project="$PROJECT" >/dev/null 2>&1; then
  KEY="${GEMINI_API_KEY:-}"
  [ -n "$KEY" ] && echo "   using GEMINI_API_KEY from .env" \
    || { read -rsp "   Paste your Gemini API key: " KEY; echo; }
  printf '%s' "$KEY" | gcloud secrets create gemini-api-key \
    --data-file=- --project="$PROJECT"
fi
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor \
  --project="$PROJECT" --quiet >/dev/null

# --- Optional channels and cost source -------------------------------------
# Anything unset is simply not passed, and that feature stays off.
OPTIONAL_ENV=""
add_env() {  # name value
  [ -n "$2" ] || return 0
  OPTIONAL_ENV="${OPTIONAL_ENV},$1=$2"
  echo "   $1 will be set"
}

OPTIONAL_SECRETS=""
add_secret() {  # secret-name env-name value
  [ -n "$3" ] || return 0
  printf '%s' "$3" | gcloud secrets create "$1" --data-file=- --project="$PROJECT" \
    2>/dev/null || printf '%s' "$3" | gcloud secrets versions add "$1" \
    --data-file=- --project="$PROJECT" >/dev/null
  gcloud secrets add-iam-policy-binding "$1" \
    --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor \
    --project="$PROJECT" --quiet >/dev/null
  OPTIONAL_SECRETS="${OPTIONAL_SECRETS},$2=$1:latest"
  echo "   $2 stored in Secret Manager"
}

echo "▸ Optional configuration"
add_env GEMMA_MODEL "${GEMMA_MODEL:-}"
add_env TELEGRAM_CHAT_ID "${TELEGRAM_CHAT_ID:-}"
add_env BILLING_EXPORT_TABLE "${BILLING_EXPORT_TABLE:-}"
# A bot token and a Slack webhook URL are credentials - the webhook URL is a
# bearer token that happens to look like a link - so they go to Secret Manager
# rather than into the service's environment, where the console shows them.
add_secret sentinel-telegram-token TELEGRAM_BOT_TOKEN "${TELEGRAM_BOT_TOKEN:-}"
add_secret sentinel-slack-webhook SLACK_WEBHOOK_URL "${SLACK_WEBHOOK_URL:-}"
[ -n "$OPTIONAL_ENV$OPTIONAL_SECRETS" ] || echo "   none set; notifications and billing stay off"

echo "▸ Building and deploying"
gcloud run deploy "$SERVICE" \
  --source . \
  --project="$PROJECT" \
  --region="$REGION" \
  --service-account="$SA" \
  --allow-unauthenticated \
  --memory=1Gi --cpu=1 \
  --min-instances=0 --max-instances=3 \
  --timeout=600 \
  --set-env-vars="PROJECT_ID=${PROJECT},REGION=${REGION},STATE_BACKEND=firestore,MOCK_MODE=false,DRY_RUN=true,GEMINI_MODEL=gemini-3.5-flash-lite${OPTIONAL_ENV}" \
  --set-secrets="GEMINI_API_KEY=gemini-api-key:latest,DASHBOARD_TOKEN=sentinel-dashboard-token:latest${OPTIONAL_SECRETS}"

URL="$(gcloud run services describe "$SERVICE" --region="$REGION" \
        --project="$PROJECT" --format='value(status.url)')"
echo "▸ Deployed: $URL"

# A notification links back to the deck, and the deck's URL only exists once
# Cloud Run has assigned one — so this is a second, cheap revision rather than
# a value the operator has to know in advance.
echo "▸ Dashboard URL for notification links"
gcloud run services update "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --update-env-vars="DASHBOARD_URL=${URL}" --quiet >/dev/null
echo "   DASHBOARD_URL=${URL}"

echo "▸ Cloud Scheduler — hourly audits"
gcloud scheduler jobs delete sentinel-hourly --location="$REGION" \
  --project="$PROJECT" --quiet 2>/dev/null || true
gcloud scheduler jobs create http sentinel-hourly \
  --location="$REGION" \
  --project="$PROJECT" \
  --schedule="0 * * * *" \
  --time-zone="Etc/UTC" \
  --uri="${URL}/webhook/pubsub" \
  --http-method=POST \
  --headers="Content-Type=application/json,X-Sentinel-Token=$(gcloud secrets versions access latest --secret=sentinel-dashboard-token --project=$PROJECT)" \
  --message-body='{"trigger":"cloud-scheduler","schedule":"hourly"}' \
  --attempt-deadline=600s

echo
echo "✓ Done."
echo "  Dashboard : $URL"
echo "  Preflight : ${URL}/api/preflight"
echo "  Schedule  : hourly, on the hour"
echo
echo "  Check what is on:  curl -H \"Authorization: Bearer <token>\" ${URL}/api/preflight"
echo "  It reports the cost source and which notification channels are live."
echo
echo "  DRY_RUN is ON. To let the agent apply changes for real:"
echo "    gcloud run services update $SERVICE --region=$REGION \\"
echo "      --update-env-vars=DRY_RUN=false"
