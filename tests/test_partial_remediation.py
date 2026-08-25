"""A partially fixed resource must remain actionable.

Regression chain found in the field:
  1. A ticket said "set min_instances to 0"; the executor only changed memory,
     so the saving was booked but never realised.
  2. The report claimed the resource was "escalated for approval" when the call
     had actually been skipped.
  3. The memory bank then blocked the resource forever, leaving the dashboard
     showing an anomaly with no way to resolve it.
"""

import pytest

from app.core.agent import CloudFinOpsAgent
from app.core.config import settings
from app.tools import gcp_actions, gcp_remediator as r
from app.tools.memory_tools import memory_bank


# --- 1. the approved shape is what gets applied ---------------------------
def test_min_instances_is_applied_not_silently_dropped(monkeypatch):
    captured = {}

    def fake(rid, memory, new_cpu="", new_min_instances=None):
        captured.update(id=rid, memory=memory, cpu=new_cpu, min_instances=new_min_instances)
        return True, "APPLIED"

    monkeypatch.setattr(gcp_actions, "resize_service", fake)

    r.request_human_approval(
        "svc", "Set min-instances to 0 and memory to 512Mi", 99.0,
        target_memory="512Mi", action_type="resize_service",
        action_params={"min_instances": 0, "cpu": "250m"},
    )
    memory_bank.resolve_approval("svc", "APPROVED")
    r.execute_approved_action("svc")

    assert captured["min_instances"] == 0, (
        "a ticket that promises min-instances 0 must actually set it"
    )
    assert captured["memory"] == "512Mi"
    assert captured["cpu"] == "250m"


def test_the_dry_run_payload_includes_min_instances(monkeypatch):
    monkeypatch.setattr(settings, "DRY_RUN", True)
    from app.core import trace as trace_mod

    gcp_actions.resize_service("svc", "512Mi", new_min_instances=0)
    sent = trace_mod.tracer.steps()[-1]["detail"]["would_send"]
    assert sent["changes"]["min_instances"] == 0


# --- 2. the report matches reality ----------------------------------------
def test_a_skipped_escalation_is_not_reported_as_escalated(monkeypatch):
    """The report said 'escalated' while the ticket was silently dropped —
    which is why the dashboard showed 1 anomaly and 0 pending."""
    a = CloudFinOpsAgent()

    r.request_human_approval("svc-dup", "Resize", 99.0)  # already pending
    line = a._dispatch(
        "svc-dup",
        {"resource_id": "svc-dup", "severity": "HIGH", "issue": "idle"},
        {"recommendation": "Resize", "monthly_saving": 99.0},
        99.0,
    )
    assert "escalated for approval" not in line
    assert "SKIPPED" in line or "already" in line


# --- 3. a changed resource becomes actionable again ------------------------
def test_a_resource_that_changed_can_be_acted_on_again(monkeypatch):
    """After a partial fix the resource still wastes money. Blocking it forever
    leaves an anomaly nobody can resolve."""
    monkeypatch.setattr(r, "_current_shape",
                        lambda rid: {"cpu": "1", "memory": "2Gi", "min_instances": 2})
    memory_bank.log_remediation(
        event_id="e1", resource_id="svc-partial", action="resize", savings=10.0,
        resource_state={"cpu": "1", "memory": "2Gi", "min_instances": 2},
    )
    assert r._already_handled("svc-partial").startswith("SKIPPED")

    # The service was resized; memory changed but it is still over-provisioned.
    monkeypatch.setattr(r, "_current_shape",
                        lambda rid: {"cpu": "1", "memory": "512Mi", "min_instances": 2})
    assert r._already_handled("svc-partial") == "", (
        "a changed-but-still-wasteful resource must be actionable again"
    )


def test_an_unchanged_resource_is_still_deduplicated(monkeypatch):
    """The loop protection must survive the fix."""
    shape = {"cpu": "1", "memory": "512Mi", "min_instances": 0}
    monkeypatch.setattr(r, "_current_shape", lambda rid: shape)
    memory_bank.log_remediation(
        event_id="e2", resource_id="svc-done", action="resize", savings=10.0,
        resource_state=shape,
    )
    assert r._already_handled("svc-done").startswith("SKIPPED")


def test_a_rejection_still_wins_over_a_shape_change(monkeypatch):
    """A human said no. Changing shape does not reopen that decision."""
    monkeypatch.setattr(r, "_current_shape", lambda rid: {"memory": "1Gi"})
    r.request_human_approval("svc-no", "Delete it", 50.0)
    memory_bank.resolve_approval("svc-no", "REJECTED")
    assert "rejected" in r._already_handled("svc-no")
