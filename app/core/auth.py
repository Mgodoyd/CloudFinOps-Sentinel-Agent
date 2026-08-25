"""Access control for the dashboard and the API.

The dashboard can delete disks and release IP addresses — irreversibly. An
unauthenticated deployment means anyone with the URL can do that, so this fails
closed: on Cloud Run without a configured token, mutating endpoints are refused
outright rather than left open.

Two credentials, deliberately separate:

* ``DASHBOARD_TOKEN`` — a human operator, exchanged for an HttpOnly cookie.
* ``WEBHOOK_TOKEN``  — Cloud Scheduler calling the audit webhook. A scheduler
  leaking its token must not also hand over the approval buttons.
"""

import hmac
import logging
import os
import secrets
from typing import Optional

from fastapi import Cookie, Header, HTTPException, Request, status

from app.core.config import settings

logger = logging.getLogger(__name__)

SESSION_COOKIE = "sentinel_session"
# Sessions live in memory: a restart logs everyone out, which is the right
# trade-off for a single-service agent with no user database.
_sessions: set = set()


def is_managed_runtime() -> bool:
    """True when running somewhere the URL may be reachable by others."""
    return bool(os.environ.get("K_SERVICE"))


def auth_configured() -> bool:
    return bool(settings.DASHBOARD_TOKEN)


def _matches(provided: str, expected: str) -> bool:
    # Constant-time: a token check that returns early leaks its length.
    return bool(expected) and hmac.compare_digest(provided, expected)


def issue_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions.add(token)
    return token


def revoke_session(token: Optional[str]) -> None:
    _sessions.discard(token)


def verify_login(token: str) -> bool:
    return _matches(token, settings.DASHBOARD_TOKEN)


# ----------------------------------------------------------------------
# Dependencies
# ----------------------------------------------------------------------
def _unauthorised(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


async def require_operator(
    request: Request,
    sentinel_session: Optional[str] = Cookie(default=None),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Guard every endpoint a person drives.

    Accepts the session cookie or a bearer token, so the dashboard and `curl`
    both work without a second auth path to keep correct.
    """
    if not auth_configured():
        if is_managed_runtime():
            # Fail closed. An open dashboard on a public URL can delete disks.
            raise _unauthorised(
                "DASHBOARD_TOKEN is not configured. Refusing to serve an "
                "unauthenticated dashboard on a hosted deployment."
            )
        return  # local development

    if sentinel_session and sentinel_session in _sessions:
        return
    if authorization and authorization.startswith("Bearer "):
        if _matches(authorization[7:], settings.DASHBOARD_TOKEN):
            return

    raise _unauthorised("Authentication required.")


async def require_webhook(
    x_sentinel_token: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
) -> None:
    """Guard the scheduler entrypoint.

    Cloud Scheduler can send an OIDC token; a shared secret header is accepted
    too so the webhook works without configuring a service identity.
    """
    expected = settings.WEBHOOK_TOKEN or settings.DASHBOARD_TOKEN
    if not expected:
        if is_managed_runtime():
            raise _unauthorised("No webhook credential configured.")
        return

    if x_sentinel_token and _matches(x_sentinel_token, expected):
        return
    if authorization and authorization.startswith("Bearer ") and _matches(
        authorization[7:], expected
    ):
        return

    raise _unauthorised("Invalid webhook credential.")


def describe() -> dict:
    """What the operator should know about the current posture."""
    return {
        "configured": auth_configured(),
        "managed_runtime": is_managed_runtime(),
        "active_sessions": len(_sessions),
        "posture": (
            "protected" if auth_configured()
            else ("refusing (no token on a hosted deployment)" if is_managed_runtime()
                  else "open (local development)")
        ),
    }
