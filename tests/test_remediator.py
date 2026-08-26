from app.core.config import settings
from app.tools import gcp_remediator as r
from app.tools.memory_tools import memory_bank


def test_low_value_resize_executes_directly():
    """Level 1: dispatched by the agent itself, no approval ticket."""
    result = r.resize_cloud_run("svc-small", "512Mi", estimated_savings=10.0)
    assert not result.startswith(("SKIPPED", "PENDING_APPROVAL", "FAILED"))
    assert memory_bank.check_history("svc-small")["found"] is True
    assert memory_bank.has_pending_approval("svc-small") is False


def test_high_value_resize_escalates_to_human():
    """The autonomy matrix is enforced in code, not just in the prompt."""
    savings = settings.HIGH_RISK_ROI_THRESHOLD + 25
    result = r.resize_cloud_run("svc-prod", "1Gi", estimated_savings=savings)
    assert result.startswith("PENDING_APPROVAL")
    assert memory_bank.has_pending_approval("svc-prod") is True
    assert memory_bank.check_history("svc-prod")["found"] is False


def test_disk_deletion_always_requires_approval():
    assert r.delete_orphan_disk("disk-1", estimated_savings=1.0).startswith("PENDING_APPROVAL")


def test_a_resource_really_remediated_is_not_touched_again(monkeypatch):
    """Loop protection applies to changes that actually happened."""
    monkeypatch.setattr(settings, "DRY_RUN", False)
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr("app.tools.gcp_actions.resize_service",
                        lambda *a, **k: (True, "APPLIED"))

    r.resize_cloud_run("svc-dup", "512Mi", estimated_savings=5.0)
    assert r.resize_cloud_run("svc-dup", "256Mi", estimated_savings=5.0).startswith("SKIPPED")


def test_a_dry_run_does_not_block_the_real_action():
    """A simulated action changed nothing, so the waste is still there. Blocking
    would leave an anomaly on the dashboard that no one could ever resolve."""
    first = r.resize_cloud_run("svc-sim", "512Mi", estimated_savings=5.0)
    assert first.startswith("DRY_RUN")

    second = r.resize_cloud_run("svc-sim", "512Mi", estimated_savings=5.0)
    assert not second.startswith("SKIPPED")


def test_duplicate_approval_tickets_are_suppressed():
    r.request_human_approval("svc-x", "Resize", 99.0)
    assert r.request_human_approval("svc-x", "Resize", 99.0).startswith("SKIPPED")
    assert len(memory_bank.snapshot()["approvals"]) == 1


def test_execute_approved_action_logs_savings():
    # target_memory is what execution applies; a ticket without one is refused.
    r.request_human_approval("svc-y", "Resize to 512Mi", 42.0, target_memory="512Mi")
    memory_bank.resolve_approval("svc-y", "APPROVED")
    result = r.execute_approved_action("svc-y")
    assert not result.startswith(("FAILED", "No approved action"))
    assert memory_bank.total_savings() == 42.0


def test_execute_without_approval_is_a_noop():
    assert "No approved action" in r.execute_approved_action("svc-unknown")


# --- Safety gate ---------------------------------------------------------
def test_dry_run_blocks_real_mutations(monkeypatch):
    """With DRY_RUN on, tools must report the change without calling GCP."""
    from app.tools import gcp_actions

    called = []
    monkeypatch.setattr(settings, "DRY_RUN", True)

    def explode(*a, **k):
        called.append(a)
        raise AssertionError("the GCP client must not be constructed during a dry run")

    monkeypatch.setattr("google.cloud.run_v2.ServicesClient", explode)
    ok, message = gcp_actions.resize_service("svc-dry", "512Mi")
    assert ok is True
    assert message.startswith("DRY_RUN")
    assert not called


def test_dry_run_marks_remediation_as_not_applied(monkeypatch):
    monkeypatch.setattr(settings, "DRY_RUN", True)
    r.resize_cloud_run("svc-flag", "512Mi", estimated_savings=5.0)
    record = memory_bank.snapshot()["remediations"][-1]
    assert record["applied"] is False


def test_failed_mutation_is_not_booked_as_savings(monkeypatch):
    """A GCP failure must not inflate the savings counter."""
    monkeypatch.setattr(settings, "DRY_RUN", False)
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(
        "app.tools.gcp_actions.resize_service",
        lambda *a, **k: (False, "FAILED to resize svc-boom: PermissionDenied"),
    )
    before = memory_bank.total_savings()
    result = r.resize_cloud_run("svc-boom", "512Mi", estimated_savings=9.0)
    assert result.startswith("FAILED")
    assert memory_bank.total_savings() == before
    assert memory_bank.check_history("svc-boom")["found"] is False


def test_approved_action_applies_the_ticket_target(monkeypatch):
    """Approving must resize to the memory the agent proposed, not a default."""
    captured = {}
    monkeypatch.setattr(settings, "DRY_RUN", False)
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(
        "app.tools.gcp_actions.resize_service",
        lambda sid, mem, **k: (captured.update(id=sid, mem=mem), (True, "APPLIED"))[1],
    )
    r.request_human_approval("svc-t", "Resize to 256Mi", 55.0, target_memory="256Mi")
    memory_bank.resolve_approval("svc-t", "APPROVED")
    r.execute_approved_action("svc-t")
    assert captured == {"id": "svc-t", "mem": "256Mi"}
