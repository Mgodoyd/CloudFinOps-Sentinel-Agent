"""Preflight: verify this deployment can actually do real work.

Checks credentials, every API the agent depends on, and write access — then
reports exactly which `gcloud` command fixes each failure. Run it as a CLI:

    python -m app.tools.preflight
"""

import json
import logging
from typing import Any, Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

OK, FAIL, WARN, SKIP = "ok", "fail", "warn", "skip"


def _check(name: str, status: str, detail: str, fix: str = "") -> Dict[str, str]:
    return {"name": name, "status": status, "detail": detail, "fix": fix}


def _is_quota(exc: Exception) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


def _uses_a_different_api(model: str) -> Optional[Dict[str, str]]:
    """Catch models that exist but answer a different protocol.

    Live models (`bidiGenerateContent`) speak the streaming Live API over a
    WebSocket. Configuring one here fails in a way that looks like a broken
    credential, so name the real reason.
    """
    try:
        from google import genai

        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        for candidate in client.models.list():
            if candidate.name.replace("models/", "") != model:
                continue
            actions = candidate.supported_actions or []
            if "generateContent" in actions:
                return None
            return _check(
                "Gemini (API key)", FAIL,
                f"'{model}' does not support generateContent — it exposes "
                f"{', '.join(actions) or 'no compatible action'}. "
                "Live models speak the bidirectional streaming API, which this agent "
                "does not use.",
                "Pick a model whose supported actions include generateContent, "
                "e.g. gemini-3.5-flash-lite.",
            )
    except Exception:
        return None  # listing is a convenience; never block preflight on it
    return None


def _is_model_missing(exc: Exception) -> bool:
    text = str(exc)
    return "404" in text or "NOT_FOUND" in text or "no longer available" in text


def _available_models() -> list:
    """Ask the API which models this key can actually call."""
    try:
        from google import genai

        client = genai.Client(api_key=settings.GEMINI_API_KEY)
        names = [
            m.name.replace("models/", "")
            for m in client.models.list()
            if "generateContent" in (m.supported_actions or [])
        ]
        # Flash models are what this agent wants: cheap, fast, tool-capable.
        return sorted(n for n in names if "flash" in n and "image" not in n
                      and "tts" not in n and "lite" not in n)[:6]
    except Exception:
        return []


def _gemini_failure(exc: Exception, name: str = "Gemini (API key)") -> Dict[str, str]:
    """Distinguish a retired model from a bad credential.

    A 404 here means "this model is not available to your key" — the key itself
    is fine. Telling the operator to check their API key sends them to fix
    something that is not broken.
    """
    if _is_model_missing(exc) and not _is_quota(exc):
        options = _available_models()
        suggestion = (
            f"Set GEMINI_MODEL to one of: {', '.join(options)}"
            if options
            else "Run `client.models.list()` to see which models your key can call."
        )
        return _check(
            name, FAIL,
            f"Model '{settings.GEMINI_MODEL}' is not available to this API key "
            "(Google retires models for keys created after a cutoff). The key itself is valid.",
            suggestion,
        )
    if _is_quota(exc):
        return _check(
            name, WARN,
            "Credentials work, but the quota is currently exhausted. Audits will "
            "fall back to the deterministic heuristic until it resets.",
            "The free tier allows 5 requests/minute. Lower MAX_TOOL_CALLS or move "
            "to a paid tier.",
        )
    if "503" in str(exc) or "UNAVAILABLE" in str(exc):
        return _check(name, WARN, "Gemini is temporarily overloaded; retry shortly.")
    return _check(
        name, FAIL, f"{type(exc).__name__}: {str(exc)[:160]}",
        "Check GEMINI_API_KEY at https://aistudio.google.com/apikey",
    )


_SA_EMAIL: str = "<YOUR_SA_EMAIL>"


def _classify(exc: Exception, api: str, permission: str) -> Dict[str, str]:
    """Turn a Google API exception into an actionable instruction."""
    text = str(exc)
    if "has not been used in project" in text or "is disabled" in text:
        return {
            "detail": f"The {api} API is not enabled on this project.",
            "fix": f"gcloud services enable {api} --project={settings.PROJECT_ID}",
        }
    if "403" in text or "PermissionDenied" in type(exc).__name__:
        return {
            "detail": f"The service account lacks '{permission}'.",
            # cloudresourcemanager is what add-iam-policy-binding calls. If it is
            # disabled the grant returns its own 403, which reads like the role
            # was refused rather than never attempted — so enabling it is part
            # of the fix, not a separate discovery.
            "fix": (
                f"gcloud services enable cloudresourcemanager.googleapis.com "
                f"--project={settings.PROJECT_ID}\n"
                f"gcloud projects add-iam-policy-binding {settings.PROJECT_ID} \\\n"
                f"  --member=serviceAccount:{_SA_EMAIL} --role=<ROLE>"
            ),
        }
    return {"detail": f"{type(exc).__name__}: {text[:160]}", "fix": ""}


def run_preflight() -> Dict[str, Any]:
    """Run every check and return a structured report."""
    checks: List[Dict[str, str]] = []

    # --- project ----------------------------------------------------------
    sa_email = None
    if not settings.PROJECT_ID:
        checks.append(
            _check(
                "Project", FAIL,
                "No project could be resolved from the service-account key, the "
                "environment, or the metadata server.",
                "Set PROJECT_ID explicitly, or deploy with "
                "--set-env-vars PROJECT_ID=<your-project>.",
            )
        )
        checks.append(_billing_check())
        checks.append(_notification_check())
        return _summarise(checks, sa_email)

    # --- credentials ------------------------------------------------------
    if settings.MOCK_MODE:
        checks.append(_check("Credentials", SKIP, "MOCK_MODE is on; GCP is not contacted."))
    else:
        try:
            import google.auth

            creds, detected = google.auth.default()
            sa_email = _resolve_sa_email(creds)
            if sa_email:
                global _SA_EMAIL
                _SA_EMAIL = sa_email
            checks.append(
                _check(
                    "Credentials",
                    OK,
                    f"Authenticated as {sa_email or 'ADC user'} on project {detected or settings.PROJECT_ID}.",
                )
            )
        except Exception as exc:
            checks.append(
                _check(
                    "Credentials", FAIL, f"No usable credentials: {exc}",
                    "Place a service-account JSON in the project root, or run "
                    "'gcloud auth application-default login'.",
                )
            )
            checks.append(_billing_check())
            checks.append(_notification_check())
            return _summarise(checks, sa_email)

    if settings.MOCK_MODE:
        checks.append(_billing_check())
        checks.append(_notification_check())
        return _summarise(checks, sa_email)

    # --- Cloud Run: read --------------------------------------------------
    services: List[str] = []
    try:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        found = list(
            client.list_services(
                request=run_v2.ListServicesRequest(
                    parent=f"projects/{settings.PROJECT_ID}/locations/{settings.REGION}"
                )
            )
        )
        services = [s.name.split("/")[-1] for s in found]
        checks.append(
            _check(
                "Cloud Run (read)",
                OK if services else WARN,
                f"Found {len(services)} service(s) in {settings.REGION}"
                + (f": {', '.join(services[:5])}" if services else " — nothing to audit yet."),
            )
        )
    except Exception as exc:
        info = _classify(exc, "run.googleapis.com", "run.services.list")
        info["fix"] = info["fix"].replace("<ROLE>", "roles/run.viewer")
        checks.append(_check("Cloud Run (read)", FAIL, info["detail"], info["fix"]))

    # --- Cloud Run: write -------------------------------------------------
    if not settings.writes_enabled:
        checks.append(
            _check(
                "Cloud Run (write)", SKIP,
                "DRY_RUN is on — the agent reports changes instead of applying them.",
                "Set DRY_RUN=false in .env to let the agent modify live services.",
            )
        )
    elif services:
        try:
            from google.cloud import run_v2

            client = run_v2.ServicesClient()
            name = (f"projects/{settings.PROJECT_ID}/locations/{settings.REGION}"
                    f"/services/{services[0]}")
            service = client.get_service(request=run_v2.GetServiceRequest(name=name))

            # validate_only proves the permission without persisting anything —
            # the only honest way to test writes on live infrastructure.
            client.update_service(
                request=run_v2.UpdateServiceRequest(service=service, validate_only=True)
            )
            checks.append(
                _check(
                    "Cloud Run (write)", WARN,
                    "DRY_RUN is OFF and services.update is permitted. Approved actions "
                    "WILL modify live infrastructure.",
                )
            )
        except Exception as exc:
            info = _classify(exc, "run.googleapis.com", "run.services.update")
            info["fix"] = info["fix"].replace("<ROLE>", "roles/run.admin")
            checks.append(_check("Cloud Run (write)", FAIL, info["detail"], info["fix"]))

    # --- Cloud Monitoring -------------------------------------------------
    if not settings.USE_REAL_METRICS:
        checks.append(_check("Cloud Monitoring", SKIP, "USE_REAL_METRICS is off; utilization is modelled."))
    else:
        try:
            from app.tools import gcp_monitoring

            data = gcp_monitoring.fetch_fleet_utilization()
            if data is None:
                checks.append(
                    _check(
                        "Cloud Monitoring", FAIL,
                        "Metrics API unreachable; utilization falls back to a model.",
                        f"gcloud services enable monitoring.googleapis.com --project={settings.PROJECT_ID}\n"
                        "and grant roles/monitoring.viewer",
                    )
                )
            else:
                checks.append(
                    _check(
                        "Cloud Monitoring",
                        OK if data else WARN,
                        f"Real utilization available for {len(data)} service(s)."
                        if data
                        else "Connected, but no Cloud Run metrics yet (services need traffic).",
                    )
                )
        except Exception as exc:
            checks.append(_check("Cloud Monitoring", FAIL, str(exc)[:160]))

    # --- Gemini -----------------------------------------------------------
    if settings.USE_VERTEX:
        try:
            from google import genai

            client = genai.Client(
                vertexai=True, project=settings.PROJECT_ID, location=settings.REGION
            )
            client.models.generate_content(model=settings.GEMINI_MODEL, contents="ping")
            checks.append(_check("Gemini (Vertex AI)", OK, f"{settings.GEMINI_MODEL} reachable."))
        except Exception as exc:
            if _is_quota(exc):
                checks.append(_gemini_failure(exc, "Gemini (Vertex AI)"))
            else:
                info = _classify(exc, "aiplatform.googleapis.com", "aiplatform.endpoints.predict")
                info["fix"] = info["fix"].replace("<ROLE>", "roles/aiplatform.user")
                checks.append(_check("Gemini (Vertex AI)", FAIL, info["detail"], info["fix"]))
    elif settings.GEMINI_API_KEY:
        wrong_api = _uses_a_different_api(settings.GEMINI_MODEL)
        if wrong_api:
            checks.append(wrong_api)
            checks.append(_billing_check())
            checks.append(_notification_check())
            return _summarise(checks, sa_email)
        try:
            from google import genai

            # Keep a reference: a chained `genai.Client(...).models...` lets the
            # client be collected mid-request, which surfaces as the misleading
            # "Cannot send a request, as the client has been closed."
            client = genai.Client(api_key=settings.GEMINI_API_KEY)
            client.models.generate_content(model=settings.GEMINI_MODEL, contents="ping")
            checks.append(_check("Gemini (API key)", OK, f"{settings.GEMINI_MODEL} reachable."))
        except Exception as exc:
            checks.append(_gemini_failure(exc))
    else:
        checks.append(
            _check(
                "Gemini", WARN,
                "No LLM configured; the agent uses its deterministic heuristic.",
                "Set GEMINI_API_KEY=<key>, or USE_VERTEX=true to use the service account.",
            )
        )

    checks.append(_billing_check())
    checks.append(_notification_check())
    return _summarise(checks, sa_email)


def _billing_check() -> Dict[str, str]:
    """Whether costs are estimated or reconciled against the invoice.

    Estimating is the honest default and works with nothing set up, so an
    unconfigured export is a note rather than a warning. But a FinOps agent
    claiming savings against a number finance has never seen is the first thing
    a reviewer will challenge, and this says which one you are looking at.
    """
    from app.tools import gcp_billing

    if not gcp_billing.is_configured():
        return _check(
            "Cost source", SKIP,
            "Costs are estimated from the allocation; no billing export configured.",
            "Set BILLING_EXPORT_TABLE to reconcile against what Google actually charged.",
        )
    if settings.MOCK_MODE:
        return _check("Cost source", SKIP, "MOCK_MODE is on; the export is not queried.")

    try:
        billed = gcp_billing.fetch_billed_costs()
    except Exception as exc:  # pragma: no cover - defensive
        return _check("Cost source", FAIL, str(exc)[:160])

    if billed is None:
        exc = gcp_billing.last_error()
        text = str(exc) if exc else ""

        # The three ways this actually fails, each with a different fix. Naming
        # them beats one generic line that sends the operator to re-grant roles
        # that were already correct.
        if "was not found in location" in text or "Not found: Dataset" in text:
            detail = (
                f"The dataset is not in the location the query ran in. {text[:160]}"
            )
            fix = ("The dataset's region is fixed at creation. Recreate it in the "
                   "same region as the service, or set the job location to match.")
        elif "has not been used in project" in text or "is disabled" in text:
            detail = "The BigQuery API is not enabled on this project."
            fix = (f"gcloud services enable bigquery.googleapis.com "
                   f"--project={settings.PROJECT_ID}")
        elif "403" in text or "Access Denied" in text or "PermissionDenied" in text:
            detail = f"{_SA_EMAIL} cannot read the billing export."
            fix = (f"gcloud projects add-iam-policy-binding {settings.PROJECT_ID} \\\n"
                   f"  --member=serviceAccount:{_SA_EMAIL} "
                   f"--role=roles/bigquery.jobUser\n"
                   f"Then the same with --role=roles/bigquery.dataViewer.")
        elif "Not found: Table" in text or "404" in text:
            detail = f"No such table: {settings.BILLING_EXPORT_TABLE}"
            fix = ("Check BILLING_EXPORT_TABLE. The export takes up to 24h to create "
                   "it, and only the Detailed export has the columns this reads.")
        elif "invalidQuery" in text or "400" in text:
            # Not a permissions problem, and suggesting roles here sends the
            # operator to fix an account that is already correct.
            detail = f"The billing query was rejected. {text[:200]}"
            fix = "This is a bug in the query, not in your configuration."
        else:
            detail = (f"Could not read {settings.BILLING_EXPORT_TABLE}."
                      + (f" {text[:160]}" if text else ""))
            fix = "Grant roles/bigquery.jobUser and roles/bigquery.dataViewer."
        return _check("Cost source", FAIL, detail, fix)

    attributed = len(billed.get("costs") or {})
    days = billed.get("days_covered") or 0
    return _check(
        "Cost source", OK,
        f"Reconciled against the billing export: {attributed} attributed "
        f"resource(s) over {days:.1f} day(s)."
        + ("  The export has not produced rows yet — it takes up to 24h and does "
           "not backfill." if not attributed else ""),
    )


def _resolve_sa_email(creds: Any) -> Optional[str]:
    """The identity this process actually runs as.

    On Cloud Run `google.auth.default()` returns compute-engine credentials
    whose `service_account_email` is the literal string "default" — the
    metadata alias, not an address. Printed into a suggested command that
    becomes `--member=serviceAccount:default`, which grants nothing and sends
    the operator to fix permissions on an account that does not exist.

    The metadata server knows the real address, so ask it.
    """
    email = getattr(creds, "service_account_email", None)
    if email and email != "default":
        return email

    try:
        import httpx

        response = httpx.get(
            "http://metadata.google.internal/computeMetadata/v1/"
            "instance/service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"},
            timeout=2.0,
        )
        if response.status_code == 200 and "@" in response.text:
            return response.text.strip()
    except Exception:
        pass  # not on GCP, or the metadata server is not reachable
    return None


def _notification_check() -> Dict[str, str]:
    """Whether an approval will actually reach anyone.

    A Level 2 ticket nobody is told about is a human-in-the-loop that depends on
    someone opening the dashboard. Silence is a valid choice, so this warns
    rather than fails — but it has to be visible before a scheduled run raises a
    ticket at 3am into an empty room.
    """
    from app.tools.notifications import configured_channels

    channels = configured_channels()
    if channels:
        return _check(
            "Notifications", OK,
            f"Approval tickets are pushed to {', '.join(channels)}.",
        )
    return _check(
        "Notifications", WARN,
        "No channel configured; approval tickets wait in the dashboard only.",
        "Set SLACK_WEBHOOK_URL, or TELEGRAM_BOT_TOKEN with TELEGRAM_CHAT_ID.",
    )


def _summarise(checks: List[Dict[str, str]], sa_email: Any) -> Dict[str, Any]:
    failed = [c for c in checks if c["status"] == FAIL]
    return {
        "ready": not failed,
        "project_id": settings.PROJECT_ID,
        "region": settings.REGION,
        "service_account": sa_email,
        "dry_run": settings.DRY_RUN,
        "mock_mode": settings.MOCK_MODE,
        "checks": checks,
        "failures": len(failed),
    }


def main() -> None:
    logging.basicConfig(level=logging.CRITICAL)
    report = run_preflight()

    GLYPH = {OK: "\033[32m✓\033[0m", FAIL: "\033[31m✗\033[0m",
             WARN: "\033[33m!\033[0m", SKIP: "\033[90m-\033[0m"}

    print(f"\n  CloudFinOps Sentinel — preflight")
    print(f"  project: {report['project_id']}   region: {report['region']}")
    if report["service_account"]:
        print(f"  identity: {report['service_account']}")
    print(f"  dry_run: {report['dry_run']}   mock_mode: {report['mock_mode']}\n")

    for c in report["checks"]:
        print(f"  {GLYPH[c['status']]} {c['name']}")
        print(f"      {c['detail']}")
        if c["fix"]:
            for line in c["fix"].split("\n"):
                print(f"      \033[36m→ {line}\033[0m")
        print()

    if report["ready"]:
        print("  \033[32mReady for real workloads.\033[0m\n")
    else:
        print(f"  \033[31m{report['failures']} blocking issue(s).\033[0m "
              "Fix the items above, then re-run.\n")


if __name__ == "__main__":
    main()
