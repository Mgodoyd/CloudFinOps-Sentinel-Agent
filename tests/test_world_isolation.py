"""A ticket raised against the demo fleet must never touch a real project.

Found in the field. The README's own on-ramp is "no GCP project? run it with
MOCK_MODE=true" and then "point it at a real project". Both modes wrote to the
same memory bank, so the simulated fleet's approval tickets survived the
switch — and the agent tried to resize `ml-inference`, a service that only ever
existed in `SIMULATED_SERVICES`, against a live project:

    FAILED to resize ml-inference: NotFound: 404 Resource 'ml-inference' of
    kind 'SERVICE' in region 'us-central1' ... does not exist.

Two defences, because either alone leaves a hole: the two worlds get separate
state files, and a ticket records which world raised it so a shared or
pre-existing file still cannot execute across the boundary.
"""

import os

import pytest

from app.core.config import Settings, settings
from app.tools import gcp_actions, gcp_remediator as r
from app.tools.gcp_metrics import SIMULATED_SERVICES
from app.tools.memory_tools import memory_bank


# --- 1. the two worlds do not share a memory bank -------------------------
def test_the_simulated_fleet_gets_its_own_state_file(monkeypatch):
    # The suite pins STATE_FILE (see conftest) and an explicit value always
    # wins; this test is about the default, so it clears it first.
    monkeypatch.delenv("STATE_FILE", raising=False)
    real = Settings(MOCK_MODE=False, STATE_FILE="data/memory_bank.json")
    mock = Settings(MOCK_MODE=True, STATE_FILE="data/memory_bank.json")

    assert real.state_file != mock.state_file, (
        "a simulated run must not be able to write into the real memory bank"
    )
    assert mock.state_file == "data/memory_bank.mock.json"


def test_an_explicit_state_file_is_always_honoured(monkeypatch):
    """The split is a default, not a policy — tests and demos pin their own."""
    monkeypatch.setenv("STATE_FILE", "/tmp/pinned.json")
    assert Settings(MOCK_MODE=True, STATE_FILE="/tmp/pinned.json").state_file == "/tmp/pinned.json"


# --- 2. a ticket knows which world raised it ------------------------------
def test_a_ticket_records_the_world_it_was_raised_against():
    r.request_human_approval("svc-a", "Resize", 99.0, target_memory="512Mi")
    ticket = memory_bank.snapshot()["approvals"][0]
    # The suite runs with MOCK_MODE=true; see tests/conftest.py.
    assert ticket["data_source"] == "simulated"


def test_a_simulated_ticket_is_refused_against_a_real_project(monkeypatch):
    """The exact 404 above, prevented one step earlier."""
    called = []
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: called.append(a) or (True, "APPLIED"))

    simulated = SIMULATED_SERVICES[1]["resource_id"]  # ml-inference
    r.request_human_approval(simulated, "Resize", 99.0, target_memory="8Gi")
    memory_bank.resolve_approval(simulated, "APPROVED")

    # The operator points the agent at a real project.
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    message = r.execute_approved_action(simulated)

    assert message.startswith("REFUSED")
    assert "simulated infrastructure" in message
    assert not called, (
        f"{simulated} exists only in the demo fleet; it must never reach the "
        "Cloud Run API"
    )


def test_a_real_ticket_is_refused_against_the_demo_fleet(monkeypatch):
    """The mirror case: demo data must not book savings on real resources."""
    called = []
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: called.append(a) or (True, "APPLIED"))

    monkeypatch.setattr(settings, "MOCK_MODE", False)
    r.request_human_approval("real-svc", "Resize", 99.0, target_memory="512Mi")
    memory_bank.resolve_approval("real-svc", "APPROVED")

    monkeypatch.setattr(settings, "MOCK_MODE", True)
    message = r.execute_approved_action("real-svc")

    assert message.startswith("REFUSED")
    assert not called


def test_a_ticket_executes_normally_within_its_own_world(monkeypatch):
    called = []
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: called.append(a) or (True, "APPLIED"))

    r.request_human_approval("svc-same-world", "Resize", 99.0, target_memory="512Mi")
    memory_bank.resolve_approval("svc-same-world", "APPROVED")
    message = r.execute_approved_action("svc-same-world")

    assert not message.startswith("REFUSED")
    assert called, "the guard must not block a ticket raised in the current mode"


def test_a_legacy_ticket_without_an_origin_still_executes(monkeypatch):
    """Tickets written before this field existed must not become unusable."""
    called = []
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: called.append(a) or (True, "APPLIED"))

    r.request_human_approval("svc-legacy", "Resize", 99.0, target_memory="512Mi")
    for ticket in memory_bank.data["approvals"]:
        ticket.pop("data_source", None)
    memory_bank.resolve_approval("svc-legacy", "APPROVED")

    assert not r.execute_approved_action("svc-legacy").startswith("REFUSED")
    assert called
