"""Persistence backends.

Cloud Run's filesystem is ephemeral: without a shared store a new revision
starts with no memory of what it already remediated, and the loop protection
silently stops working.
"""

import json

import pytest

from app.tools.state_store import (
    FileStore,
    FirestoreStore,
    NullStore,
    build_store,
)

SAMPLE = {"remediations": [{"resource_id": "svc"}], "approvals": [], "events": [], "runs": []}


def test_file_store_round_trip(tmp_path):
    store = FileStore(str(tmp_path / "state.json"))
    assert store.load() is None
    assert store.save(SAMPLE) is True
    assert store.load() == SAMPLE


def test_file_store_writes_atomically(tmp_path):
    """A crash mid-write must not leave a half-parsed file behind."""
    path = tmp_path / "state.json"
    store = FileStore(str(path))
    store.save(SAMPLE)
    assert not (tmp_path / "state.json.tmp").exists()
    assert json.loads(path.read_text())["remediations"][0]["resource_id"] == "svc"


def test_file_store_survives_corruption(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not json")
    assert FileStore(str(path)).load() is None, "a corrupt file must not crash startup"


def test_null_store_persists_nothing():
    store = NullStore()
    assert store.save(SAMPLE) is True
    assert store.load() is None


def test_firestore_degrades_instead_of_refusing_to_start(monkeypatch):
    """An agent that cannot read its history is still better than one that
    will not boot."""
    store = FirestoreStore("proj")

    def explode():
        raise RuntimeError("permission denied")

    monkeypatch.setattr(store, "_doc", explode)
    assert store.load() is None
    assert store.save(SAMPLE) is False


def test_firestore_stops_retrying_after_a_failure(monkeypatch):
    calls = []
    store = FirestoreStore("proj")

    def explode():
        calls.append(1)
        raise RuntimeError("nope")

    monkeypatch.setattr(store, "_doc", explode)
    store.load()
    store.load()
    store.save(SAMPLE)
    assert len(calls) == 1, "a dead backend must not be retried on every write"


# --- Backend selection ----------------------------------------------------
def test_cloud_run_selects_firestore(monkeypatch, tmp_path):
    monkeypatch.setenv("K_SERVICE", "cloudfinops-sentinel")
    store = build_store("auto", "proj", str(tmp_path / "s.json"))
    assert isinstance(store, FirestoreStore), (
        "on Cloud Run a file would vanish with the revision"
    )


def test_local_selects_a_file(monkeypatch, tmp_path):
    monkeypatch.delenv("K_SERVICE", raising=False)
    assert isinstance(build_store("auto", "proj", str(tmp_path / "s.json")), FileStore)


@pytest.mark.parametrize(
    "backend,expected",
    [("firestore", FirestoreStore), ("file", FileStore), ("none", NullStore)],
)
def test_explicit_backend_wins(backend, expected, tmp_path, monkeypatch):
    monkeypatch.setenv("K_SERVICE", "anything")
    assert isinstance(build_store(backend, "proj", str(tmp_path / "s.json")), expected)


def test_memory_bank_reports_its_backend(tmp_path):
    from app.tools.memory_tools import MemoryBank

    bank = MemoryBank(state_file=str(tmp_path / "b.json"), backend="file")
    assert "file:" in bank.store.describe
    bank.log_remediation("e", "resize", 5.0, resource_id="svc")

    reopened = MemoryBank(state_file=str(tmp_path / "b.json"), backend="file")
    assert reopened.check_history("svc")["found"] is True, "history must survive a restart"
