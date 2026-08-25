# Deployment

## One command

```bash
./deploy/deploy.sh
```

Enables the APIs, creates a least-privilege service account, provisions
Firestore, stores the Gemini key in Secret Manager, builds and deploys to Cloud
Run, and schedules hourly audits.

Requires the `gcloud` CLI authenticated as a principal that can enable APIs and
grant IAM.

## What it grants, and why

| Role | Needed for |
|---|---|
| `run.viewer` | Discover Cloud Run services |
| `run.admin` | Apply an approved resize |
| `monitoring.viewer` | Read real CPU/memory peaks |
| `compute.viewer` | Find orphaned disks and unused IPs |
| `compute.storageAdmin` | Delete an approved orphaned disk |
| `artifactregistry.reader` | Find untagged images |
| `datastore.user` | Persist the Memory Bank in Firestore |
| `aiplatform.user` | Gemini via Vertex AI (only if `USE_VERTEX=true`) |

Drop the two write roles (`run.admin`, `compute.storageAdmin`) to run
permanently in read-only mode; the agent will report every finding and simply
never execute.

## Safety

`DRY_RUN=true` is deployed by default: the agent detects, reasons, plans and
raises approval tickets, but reports the change it *would* make instead of
applying it. Every dry-run step records the exact payload it would have sent, so
you can review it before enabling writes.

```bash
gcloud run services update cloudfinops-sentinel \
  --region=us-central1 --update-env-vars=DRY_RUN=false
```

## Verify the deployment

```bash
URL=$(gcloud run services describe cloudfinops-sentinel \
      --region=us-central1 --format='value(status.url)')

curl -s "$URL/health"          # mode, model, project
curl -s "$URL/api/preflight"   # APIs, roles, write access
curl -s -X POST "$URL/api/trigger"
```

`/api/preflight` is the honest answer to "is this really wired up?" — it checks
each API and permission and prints the exact command to fix anything missing.
