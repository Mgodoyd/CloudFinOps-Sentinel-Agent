#!/usr/bin/env bash
# Deploy CloudFinOps Sentinel to Cloud Run, with Cloud Scheduler driving it.
#
# Prerequisites: gcloud CLI, authenticated as a principal that can enable APIs
# and grant IAM. Run from the repository root:  ./deploy/deploy.sh
set -euo pipefail

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
  roles/aiplatform.user
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
  read -rsp "   Paste your Gemini API key: " KEY; echo
  printf '%s' "$KEY" | gcloud secrets create gemini-api-key \
    --data-file=- --project="$PROJECT"
fi
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:${SA}" --role=roles/secretmanager.secretAccessor \
  --project="$PROJECT" --quiet >/dev/null

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
  --set-env-vars="PROJECT_ID=${PROJECT},REGION=${REGION},STATE_BACKEND=firestore,MOCK_MODE=false,DRY_RUN=true,GEMINI_MODEL=gemini-3.5-flash-lite" \
  --set-secrets="GEMINI_API_KEY=gemini-api-key:latest,DASHBOARD_TOKEN=sentinel-dashboard-token:latest"

URL="$(gcloud run services describe "$SERVICE" --region="$REGION" \
        --project="$PROJECT" --format='value(status.url)')"
echo "▸ Deployed: $URL"

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
echo "  DRY_RUN is ON. To let the agent apply changes for real:"
echo "    gcloud run services update $SERVICE --region=$REGION \\"
echo "      --update-env-vars=DRY_RUN=false"
