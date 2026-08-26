import os
import tempfile

import pytest

# Point the agent at an isolated, simulated environment before app import.
# These must not inherit the operator's .env: a test run must never touch live
# infrastructure, whatever the local configuration says.
# The suite must be hermetic: it runs identically on a clean clone with no
# service-account key, no .env and no network.
os.environ["PROJECT_ID"] = "test-project"
os.environ["MOCK_MODE"] = "true"
os.environ["DRY_RUN"] = "true"
os.environ["GEMINI_API_KEY"] = ""
os.environ["USE_VERTEX"] = "false"
os.environ["OTEL_ENABLED"] = "false"
os.environ["STATE_BACKEND"] = "file"
os.environ["DASHBOARD_TOKEN"] = "test-token"
os.environ["STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "memory_bank.json")
# Notification channels are read from .env like everything else, so an operator
# with a real bot token configured would have the suite deliver test tickets to
# their actual chat. Cleared explicitly: the tests that need a channel set one
# themselves.
os.environ["SLACK_WEBHOOK_URL"] = ""
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["TELEGRAM_CHAT_ID"] = ""
os.environ["DASHBOARD_URL"] = ""

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.tools.memory_tools import memory_bank  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    memory_bank.reset()
    yield
    memory_bank.reset()


@pytest.fixture
def client():
    """An authenticated client. There is no unauthenticated mode to test with."""
    with TestClient(app) as c:
        c.post("/api/login", json={"token": "test-token"})
        yield c
