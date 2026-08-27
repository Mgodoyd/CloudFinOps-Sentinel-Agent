#!/usr/bin/env bash
# Enable the APIs the agent needs and grant its service account the roles it
# needs, then show what actually landed.
#
# Exists because these commands are long, contain domains that chat clients
# turn into links, and span multiple lines — every one of which is a way for a
# pasted command to arrive broken. Run this instead:
#
#     bash scripts/grant_roles.sh
#
# Safe to run repeatedly: enabling an enabled API and granting a granted role
# are both no-ops.
set -uo pipefail

PROJECT="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT" ] || { echo "Set PROJECT_ID or run: gcloud config set project <id>"; exit 1; }

SA_NAME="${SA_NAME:-sentinel-agent}"
SA="${SA_NAME}@${PROJECT}.iam.gserviceaccount.com"

echo "project = $PROJECT"
echo "account = $SA"
echo

# cloudresourcemanager is what add-iam-policy-binding itself calls. With it
# disabled every grant below returns a 403 that reads like the role was refused
# rather than never attempted, so it goes first and on its own.
echo "▸ Enabling APIs"
gcloud services enable \
  cloudresourcemanager.googleapis.com \
  bigquery.googleapis.com \
  run.googleapis.com \
  monitoring.googleapis.com \
  compute.googleapis.com \
  artifactregistry.googleapis.com \
  firestore.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudtrace.googleapis.com \
  --project="$PROJECT" || {
    echo "  Could not enable APIs. Your user may lack serviceusage.services.enable."
    exit 1
  }
echo "  done"
echo

echo "▸ Granting roles"
FAILED=0
for ROLE in \
  roles/run.viewer roles/run.admin \
  roles/monitoring.viewer \
  roles/compute.viewer roles/compute.storageAdmin \
  roles/artifactregistry.repoAdmin \
  roles/datastore.user \
  roles/cloudtrace.agent \
  roles/logging.logWriter \
  roles/aiplatform.user \
  roles/bigquery.jobUser roles/bigquery.dataViewer
do
  if gcloud projects add-iam-policy-binding "$PROJECT" \
       --member="serviceAccount:${SA}" --role="$ROLE" \
       --condition=None --quiet >/dev/null 2>&1
  then
    echo "  ok      $ROLE"
  else
    echo "  FAILED  $ROLE"
    FAILED=1
  fi
done
echo

echo "▸ What the account actually has now"
gcloud projects get-iam-policy "$PROJECT" \
  --flatten="bindings[].members" \
  --filter="bindings.members:${SA}" \
  --format="value(bindings.role)" | sort | sed 's/^/  /'

if [ "$FAILED" -ne 0 ]; then
  echo
  echo "Some grants failed. The usual cause is that your own user cannot modify"
  echo "IAM on this project — you need roles/resourcemanager.projectIamAdmin or"
  echo "roles/owner."
  exit 1
fi

echo
echo "IAM takes up to a minute to propagate. Then re-run preflight:"
echo "  curl -H \"Authorization: Bearer \$TOKEN\" \$URL/api/preflight"
