"""The Firestore backend, which is the only thing standing between a Cloud Run
revision and an agent with amnesia.

Cloud Run's filesystem is ephemeral. Every deploy is a new container, so a
file-backed memory bank starts empty and the loop protection silently stops
working: the agent re-proposes what it already fixed, and re-raises what a human
already rejected. That failure is invisible — nothing errors, the agent just
quietly forgets.

The suite otherwise runs on the file backend, so this exercises the Firestore
path against a fake client that behaves like the real one: a document that may
or may not exist, and a server that can be down.
"""

from typing import Any, Dict, Optional

import pytest

from app.tools.memory_tools import MemoryBank
from app.tools.state_store import (
    COLLECTION,
    DOCUMENT,
    FileStore,
    FirestoreStore,
    NullStore,
    build_store,
)


# ----------------------------------------------------------------------
# A Firestore stand-in: same surface the store actually uses.
# ----------------------------------------------------------------------
class FakeSnapshot:
    def __init__(self, data: Optional[Dict[str, Any]]):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data or {})


class FakeDocument:
    def __init__(self, server: "FakeFirestore", path: str):
        self.server = server
        self.path = path

    def get(self) -> FakeSnapshot:
        self.server.raise_if_down()
        self.server.reads += 1
        return FakeSnapshot(self.server.documents.get(self.path))

    def set(self, state: Dict[str, Any]) -> None:
        self.server.raise_if_down()
        self.server.writes += 1
        # Firestore stores a copy; keeping a reference would make the test pass
        # for the wrong reason by mutating in place.
        self.server.documents[self.path] = dict(state)


class FakeCollection:
    def __init__(self, server: "FakeFirestore", name: str):
        self.server = server
        self.name = name

    def document(self, name: str) -> FakeDocument:
        return FakeDocument(self.server, f"{self.name}/{name}")


class FakeFirestore:
    """Survives across store instances, the way a real database does."""

    def __init__(self, down: bool = False):
        self.documents: Dict[str, Dict[str, Any]] = {}
        self.down = down
        self.reads = 0
        self.writes = 0

    def raise_if_down(self) -> None:
        if self.down:
            raise RuntimeError("503 Firestore unavailable")

    def collection(self, name: str) -> FakeCollection:
        return FakeCollection(self, name)


@pytest.fixture
def server():
    return FakeFirestore()


def store_on(server: FakeFirestore) -> FirestoreStore:
    store = FirestoreStore(project_id="test-project")
    store._client = server  # the real one is built lazily on first use
    return store


# ----------------------------------------------------------------------
# 1. Round trip
# ----------------------------------------------------------------------
def test_an_empty_database_is_not_an_error(server):
    """A first run has nothing to restore; that is normal, not a failure."""
    assert store_on(server).load() is None


def test_state_survives_a_round_trip(server):
    store = store_on(server)
    state = {"remediations": [{"resource_id": "checkout-api", "savings": 148.15}],
             "approvals": [], "events": [], "runs": []}

    assert store.save(state) is True
    assert store.load() == state


def test_it_writes_to_the_documented_location(server):
    """deploy.sh and the runbook name this path; a silent change breaks both."""
    store_on(server).save({"runs": []})
    assert f"{COLLECTION}/{DOCUMENT}" in server.documents


def test_saving_replaces_rather_than_merges(server):
    store = store_on(server)
    store.save({"approvals": [{"resource_id": "a"}]})
    store.save({"approvals": []})

    assert store.load() == {"approvals": []}, (
        "a resolved ticket must disappear; a merge would resurrect it"
    )


# ----------------------------------------------------------------------
# 2. The property the demo depends on
# ----------------------------------------------------------------------
def test_the_memory_bank_survives_a_new_revision(server, monkeypatch):
    """The claim the architecture rests on, end to end.

    A new Cloud Run revision is a new process with a new MemoryBank reading the
    same Firestore document. What the previous revision remediated must still be
    known, or the agent starts proposing it again.
    """
    monkeypatch.setattr("app.tools.memory_tools.build_store", lambda *a, **k: store_on(server))

    before = MemoryBank(state_file="ignored", backend="firestore")
    before.log_remediation(
        event_id="resize_checkout-api",
        resource_id="checkout-api",
        action="resize to 2Gi",
        savings=148.15,
    )
    assert before.check_history("checkout-api")["found"] is True

    # --- the revision is replaced; nothing survives except Firestore ---
    after = MemoryBank(state_file="ignored", backend="firestore")

    history = after.check_history("checkout-api")
    assert history["found"] is True, (
        "the new revision forgot a remediation the previous one applied — this "
        "is exactly the amnesia Firestore exists to prevent"
    )
    assert history["last_action"] == "resize to 2Gi"


def test_a_rejection_survives_a_new_revision(server, monkeypatch):
    """A human said no once. They must not be asked again after a deploy."""
    monkeypatch.setattr("app.tools.memory_tools.build_store", lambda *a, **k: store_on(server))

    before = MemoryBank(state_file="ignored", backend="firestore")
    before.add_approval({"resource_id": "prod-api", "proposed_action": "Resize to 512Mi",
                         "estimated_roi": 90.0})
    before.resolve_approval("prod-api", "REJECTED")

    after = MemoryBank(state_file="ignored", backend="firestore")
    assert after.last_rejection("prod-api") is not None


# ----------------------------------------------------------------------
# 3. Degrading when Firestore is not there
# ----------------------------------------------------------------------
def test_an_unreachable_firestore_does_not_raise():
    """An agent that cannot read its history still beats one that will not boot."""
    store = store_on(FakeFirestore(down=True))
    assert store.load() is None
    assert store.save({"runs": []}) is False


def test_a_failed_load_stops_retrying(server):
    """One warning, not one per write for the life of the process."""
    server.down = True
    store = store_on(server)
    store.load()

    server.reads = server.writes = 0
    store.load()
    store.save({"runs": []})

    assert server.reads == 0 and server.writes == 0


def test_the_agent_still_starts_without_firestore(monkeypatch):
    monkeypatch.setattr("app.tools.memory_tools.build_store",
                        lambda *a, **k: store_on(FakeFirestore(down=True)))
    bank = MemoryBank(state_file="ignored", backend="firestore")

    bank.log_event("still running", level="INFO")
    assert bank.snapshot()["events"], "the bank must work in memory even with no backend"


# ----------------------------------------------------------------------
# 4. Backend selection — how Cloud Run gets Firestore without being told
# ----------------------------------------------------------------------
def test_cloud_run_is_detected_and_gets_firestore(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "cloudfinops-sentinel")
    assert isinstance(build_store("auto", "p", "data/memory_bank.json"), FirestoreStore)


def test_off_cloud_run_auto_stays_on_a_file(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert isinstance(build_store("auto", "p", "data/memory_bank.json"), FileStore)


def test_the_backend_can_be_forced_either_way(monkeypatch):
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert isinstance(build_store("firestore", "p", "data/x.json"), FirestoreStore)
    monkeypatch.setenv("K_SERVICE", "cloudfinops-sentinel")
    assert isinstance(build_store("file", "p", "data/x.json"), FileStore)
    assert isinstance(build_store("none", "p", "data/x.json"), NullStore)
