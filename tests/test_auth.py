"""Access control.

The dashboard can delete disks and release IP addresses irreversibly. An
unauthenticated deployment hands that to anyone with the URL.
"""

import pytest

from app.core import auth
from app.core.config import settings

WRITE_ENDPOINTS = [
    ("post", "/api/approvals", {"resource_id": "x", "status": "APPROVED"}),
    ("post", "/api/trigger", None),
    ("post", "/api/audit", None),
    ("post", "/api/reset", None),
]
READ_ENDPOINTS = [
    "/api/state", "/api/resources", "/api/preflight",
    "/api/history", "/api/trace", "/api/events",
]


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_TOKEN", "s3cret-token")
    auth._sessions.clear()
    yield "s3cret-token"
    auth._sessions.clear()


# --- Locked down ----------------------------------------------------------
@pytest.mark.parametrize("method,path,body", WRITE_ENDPOINTS)
def test_mutating_endpoints_reject_anonymous_callers(client, protected, method, path, body):
    response = getattr(client, method)(path, json=body) if body else getattr(client, method)(path)
    assert response.status_code == 401, f"{path} is open to the world"


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoints_reject_anonymous_callers(client, protected, path):
    """The inventory reveals the shape and cost of the whole estate."""
    assert client.get(path).status_code == 401


def test_health_stays_open(client, protected):
    """Cloud Run probes it, and it exposes nothing sensitive."""
    assert client.get("/health").status_code == 200


def test_auth_status_stays_open(client, protected):
    """The UI must be able to ask whether a login is needed."""
    body = client.get("/api/auth").json()
    assert body["configured"] is True
    assert "posture" in body


# --- Logging in -----------------------------------------------------------
def test_a_valid_token_opens_a_session(client, protected):
    assert client.post("/api/login", json={"token": protected}).status_code == 200
    assert client.get("/api/state").status_code == 200


def test_a_wrong_token_is_refused(client, protected):
    assert client.post("/api/login", json={"token": "wrong"}).status_code == 401
    assert client.get("/api/state").status_code == 401


def test_the_session_cookie_is_not_readable_by_scripts(client, protected):
    response = client.post("/api/login", json={"token": protected})
    cookie = response.headers["set-cookie"]
    assert "httponly" in cookie.lower(), "an XSS could otherwise steal the session"
    assert "samesite=strict" in cookie.lower().replace(" ", "")


def test_a_bearer_token_works_for_scripting(client, protected):
    assert client.get(
        "/api/state", headers={"Authorization": f"Bearer {protected}"}
    ).status_code == 200


def test_logout_ends_the_session(client, protected):
    client.post("/api/login", json={"token": protected})
    assert client.get("/api/state").status_code == 200
    client.post("/api/logout")
    assert client.get("/api/state").status_code == 401


# --- Fail closed ----------------------------------------------------------
def test_a_hosted_deployment_without_a_token_refuses_to_serve(client, monkeypatch):
    """Better to serve nothing than to serve delete buttons to the internet."""
    monkeypatch.setattr(settings, "DASHBOARD_TOKEN", "")
    monkeypatch.setenv("K_SERVICE", "cloudfinops-sentinel")
    assert client.get("/api/state").status_code == 401
    assert client.post("/api/reset").status_code == 401


def test_local_development_stays_convenient(client, monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_TOKEN", "")
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert client.get("/api/state").status_code == 200


# --- The webhook is a separate credential ---------------------------------
def test_the_webhook_rejects_anonymous_calls(client, protected):
    assert client.post("/webhook/pubsub", json={"t": "x"}).status_code == 401


def test_the_webhook_accepts_its_own_token(client, monkeypatch):
    monkeypatch.setattr(settings, "DASHBOARD_TOKEN", "dash")
    monkeypatch.setattr(settings, "WEBHOOK_TOKEN", "hook")
    assert client.post(
        "/webhook/pubsub", json={"t": "x"}, headers={"X-Sentinel-Token": "hook"}
    ).status_code == 200


def test_the_webhook_token_does_not_unlock_the_dashboard(client, monkeypatch):
    """A leaked scheduler credential must not grant the approval buttons."""
    monkeypatch.setattr(settings, "DASHBOARD_TOKEN", "dash")
    monkeypatch.setattr(settings, "WEBHOOK_TOKEN", "hook")
    assert client.post("/api/login", json={"token": "hook"}).status_code == 401
    assert client.get(
        "/api/state", headers={"Authorization": "Bearer hook"}
    ).status_code == 401


def test_token_comparison_is_constant_time():
    """A check that returns early leaks the token's length."""
    import inspect

    assert "compare_digest" in inspect.getsource(auth._matches)
