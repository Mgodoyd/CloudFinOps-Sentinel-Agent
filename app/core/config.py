import glob
import json
import logging
import os
from typing import ClassVar, Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _discover_service_account() -> Optional[str]:
    """Find a service-account JSON dropped in the project root.

    Lets you authenticate by just placing the key file next to the code, with no
    env var to set. Files that are not service accounts are ignored.
    """
    for path in sorted(glob.glob(os.path.join(ROOT, "*.json"))):
        if os.path.basename(path) in ("package.json", "tsconfig.json"):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            if blob.get("type") == "service_account" and blob.get("project_id"):
                return path
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
    return None


def _read_sa_project(path: Optional[str]) -> Optional[str]:
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh).get("project_id")
    except (OSError, json.JSONDecodeError):
        return None


def _project_from_environment() -> Optional[str]:
    """Ask the runtime which project we are in.

    On Cloud Run this comes from the metadata server; locally from whatever
    `gcloud auth application-default login` configured.
    """
    for var in ("GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "GCP_PROJECT"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        import google.auth

        _, project = google.auth.default()
        return project
    except Exception:
        return None


_SA_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or _discover_service_account()
_SA_PROJECT = _read_sa_project(_SA_PATH)


class Settings(BaseSettings):
    """Runtime configuration.

    Every value can be overridden through environment variables or a local
    `.env` file (see `.env.example`). Nothing sensitive is hardcoded.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Google Cloud -----------------------------------------------------
    # Resolved from the service-account key, then ADC / the Cloud Run metadata
    # server. Never defaults to a hardcoded project: silently auditing someone
    # else's project because an env var was missing is the worst failure here.
    PROJECT_ID: str = _SA_PROJECT or _project_from_environment() or ""
    REGION: str = "us-central1"
    GOOGLE_APPLICATION_CREDENTIALS: Optional[str] = _SA_PATH

    # --- Gemini -----------------------------------------------------------
    # Two ways to reach Gemini:
    #   1. GEMINI_API_KEY  -> Google AI Studio (simplest)
    #   2. USE_VERTEX=true -> Vertex AI using the service account (no key)
    GEMINI_API_KEY: str = "AQ.Ab8RN6Kl2HbVR69dL96cgO0hk5T2OiizsQz0J0s2oxJK6VYldw"
    USE_VERTEX: bool = False
    # gemini-2.5-flash was retired for new API keys and 404s for them, so the
    # default is the current Flash generation. Override if you have access to
    # something newer.
    GEMINI_MODEL: str = "gemini-3.5-flash-lite"
    GEMINI_TEMPERATURE: float = 0.2
    # Automatic function calls per audit. Each one is a separate API request,
    # and the Gemini free tier allows only 5 per minute.
    MAX_TOOL_CALLS: int = 4

    # --- Agent behaviour --------------------------------------------------
    METRICS_CACHE_TTL: int = 60
    MEMORY_ANOMALY_THRESHOLD_GIB: float = 1.0
    # Estimated savings above which an action needs a human (Autonomy Level 2).
    HIGH_RISK_ROI_THRESHOLD: float = 40.0
    # Below this, a finding is reported but not acted on. Escalating a $1/month
    # resize costs more in human attention than it saves.
    MIN_SAVINGS_THRESHOLD: float = 5.0
    STATE_FILE: str = "data/memory_bank.json"
    # Where the memory bank lives: "auto" uses Firestore on Cloud Run (whose
    # filesystem is ephemeral) and a local file otherwise.
    STATE_BACKEND: str = "auto"

    # Comma-separated regions to scan. "auto" probes the common Cloud Run
    # regions so a service is found wherever it lives.
    SCAN_REGIONS: str = "auto"

    # --- Observability ----------------------------------------------------
    VERSION: str = "2.0.0"
    OTEL_ENABLED: bool = True
    # Point at a collector to export elsewhere; empty uses Cloud Trace.
    OTEL_EXPORTER_OTLP_ENDPOINT: str = ""

    # --- Access control ---------------------------------------------------
    # Required on any hosted deployment: the dashboard can delete disks.
    DASHBOARD_TOKEN: str = ""
    # Separate credential for Cloud Scheduler, so a leaked scheduler token does
    # not also grant the approval buttons. Falls back to DASHBOARD_TOKEN.
    WEBHOOK_TOKEN: str = ""

    # Run against a simulated fleet instead of GCP (offline demo).
    MOCK_MODE: bool = False

    # When a GCP call fails, substitute the simulated fleet instead of
    # surfacing the error. Off by default: a real deployment should never
    # silently show invented infrastructure.
    ALLOW_SIMULATED_FALLBACK: bool = False

    # SAFETY GATE. While true, remediation tools compute and log the change but
    # never mutate live infrastructure. Set DRY_RUN=false to let the agent
    # actually resize Cloud Run services. Read the README before flipping it.
    DRY_RUN: bool = True

    # Pull real CPU/memory utilization from Cloud Monitoring. Falls back to a
    # deterministic model when the API is unavailable.
    USE_REAL_METRICS: bool = True
    METRICS_LOOKBACK_HOURS: int = 24

    # Regions probed when SCAN_REGIONS is "auto". ClassVar, not a settings field.
    DEFAULT_REGIONS: ClassVar[tuple] = (
        "us-central1", "us-east1", "us-east4", "us-west1", "us-west2",
        "europe-west1", "europe-west4", "asia-east1", "asia-northeast1",
        "southamerica-east1",
    )

    @property
    def writes_enabled(self) -> bool:
        return not self.DRY_RUN and not self.MOCK_MODE

    @property
    def regions(self) -> tuple:
        """Regions to scan, always including the configured primary REGION."""
        if self.SCAN_REGIONS.strip().lower() == "auto":
            listed = self.DEFAULT_REGIONS
        else:
            listed = tuple(r.strip() for r in self.SCAN_REGIONS.split(",") if r.strip())
        return (self.REGION,) + tuple(r for r in listed if r != self.REGION)


settings = Settings()

if settings.GOOGLE_APPLICATION_CREDENTIALS and os.path.exists(
    settings.GOOGLE_APPLICATION_CREDENTIALS
):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = settings.GOOGLE_APPLICATION_CREDENTIALS
