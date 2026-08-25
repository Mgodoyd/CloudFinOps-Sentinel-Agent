import os
import tempfile

import pytest

# Point the agent at an isolated, simulated environment before app import.
# These must not inherit the operator's .env: a test run must never touch live
# infrastructure, whatever the local configuration says.
os.environ["MOCK_MODE"] = "true"
os.environ["DRY_RUN"] = "true"
os.environ["GEMINI_API_KEY"] = ""
os.environ["USE_VERTEX"] = "false"
os.environ["STATE_FILE"] = os.path.join(tempfile.mkdtemp(), "memory_bank.json")

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
    with TestClient(app) as c:
        yield c
