from app.core.config import settings
from app.tools.preflight import run_preflight


def test_preflight_skips_gcp_in_mock_mode():
    report = run_preflight()
    assert report["mock_mode"] is True
    assert report["ready"] is True
    names = {c["name"] for c in report["checks"]}
    assert "Credentials" in names
    assert all(c["status"] != "fail" for c in report["checks"])


def test_preflight_reports_the_write_gate():
    report = run_preflight()
    assert report["dry_run"] is settings.DRY_RUN


def test_failures_carry_a_fix(monkeypatch):
    """Every blocking failure must tell the operator how to resolve it."""
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    report = run_preflight()
    for check in report["checks"]:
        if check["status"] == "fail":
            assert check["fix"], f"{check['name']} failed without a remedy"


# --- Gemini check classification -----------------------------------------
def test_quota_is_a_warning_not_a_blocking_failure():
    """A rate limit proves the key works; it must not read as misconfigured."""
    from app.tools.preflight import _gemini_failure

    result = _gemini_failure(Exception("429 RESOURCE_EXHAUSTED quota exceeded"))
    assert result["status"] == "warn"
    assert "quota" in result["detail"].lower()


def test_overload_is_a_warning():
    from app.tools.preflight import _gemini_failure

    assert _gemini_failure(Exception("503 UNAVAILABLE"))["status"] == "warn"


def test_a_bad_key_is_a_blocking_failure():
    from app.tools.preflight import _gemini_failure

    result = _gemini_failure(Exception("401 API key not valid"))
    assert result["status"] == "fail"
    assert "aistudio" in result["fix"]


def test_gemini_client_reference_is_held(monkeypatch):
    """Regression: a chained genai.Client(...).models... gets collected
    mid-request and reports 'client has been closed' instead of the real error."""
    import inspect

    from app.tools import preflight

    source = inspect.getsource(preflight.run_preflight)
    assert "genai.Client(api_key=settings.GEMINI_API_KEY).models" not in source, (
        "the client must be bound to a name before use, or it can be "
        "garbage-collected mid-request"
    )


def test_an_unresolved_project_fails_loudly(monkeypatch):
    """Regression: the default was a hardcoded project name, so a deploy that
    forgot PROJECT_ID would silently audit someone else's project."""
    from app.tools.preflight import run_preflight

    monkeypatch.setattr(settings, "PROJECT_ID", "")
    monkeypatch.setattr(settings, "MOCK_MODE", False)

    report = run_preflight()
    assert report["ready"] is False
    project_check = report["checks"][0]
    assert project_check["name"] == "Project"
    assert project_check["status"] == "fail"
    assert "PROJECT_ID" in project_check["fix"]


def test_no_hardcoded_project_remains_in_config():
    import pathlib

    source = pathlib.Path("app/core/config.py").read_text()
    assert "synox-ai" not in source, "a stale project name must not be a default"


def test_the_suggested_command_never_names_a_nonexistent_account(monkeypatch):
    """On Cloud Run, google.auth returns credentials whose
    `service_account_email` is the literal string "default" — the metadata
    alias, not an address. Printed into a fix it becomes
    `--member=serviceAccount:default`, which grants nothing and sends the
    operator to repair permissions on an account that does not exist.
    """
    from app.tools import preflight

    class ComputeCreds:
        service_account_email = "default"

    monkeypatch.setattr("httpx.get", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    assert preflight._resolve_sa_email(ComputeCreds()) is None, (
        "unknown is better than a wrong address: the fix line falls back to a "
        "placeholder the operator must replace, instead of one that looks real"
    )


def test_a_real_service_account_email_is_used_as_is(monkeypatch):
    class KeyCreds:
        service_account_email = "sentinel-agent@a-project.iam.gserviceaccount.com"

    from app.tools import preflight

    assert preflight._resolve_sa_email(KeyCreds()) == KeyCreds.service_account_email


def test_the_metadata_server_supplies_the_address_on_cloud_run(monkeypatch):
    from app.tools import preflight

    class ComputeCreds:
        service_account_email = "default"

    class Response:
        status_code = 200
        text = "sentinel-agent@a-project.iam.gserviceaccount.com\n"

    monkeypatch.setattr("httpx.get", lambda *a, **k: Response())
    assert preflight._resolve_sa_email(ComputeCreds()) == (
        "sentinel-agent@a-project.iam.gserviceaccount.com"
    )
