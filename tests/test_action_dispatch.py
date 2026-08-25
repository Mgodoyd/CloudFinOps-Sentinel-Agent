"""An approved action must run the handler that action actually needs.

Regression: every approval called resize_service, so approving "delete orphaned
disk" issued a Cloud Run services.patch against a disk name — a resource that
does not exist — and still booked the savings as if the disk had been deleted.
"""

import pytest

from app.core.config import settings
from app.tools import gcp_actions, gcp_remediator as r
from app.tools.memory_tools import memory_bank


def approve(resource_id):
    memory_bank.resolve_approval(resource_id, "APPROVED")


def test_disk_approval_calls_the_disk_handler(monkeypatch):
    calls = {}
    monkeypatch.setattr(gcp_actions, "delete_disk",
                        lambda rid, zone="": (calls.update(disk=rid, zone=zone), (True, "APPLIED"))[1])
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: pytest.fail("a disk must never be resized"))

    r.delete_orphan_disk("orphan-disk-1", estimated_savings=4.0, zone="us-central1-a")
    approve("orphan-disk-1")
    r.execute_approved_action("orphan-disk-1")

    assert calls == {"disk": "orphan-disk-1", "zone": "us-central1-a"}


def test_static_ip_approval_calls_the_address_handler(monkeypatch):
    calls = {}
    monkeypatch.setattr(gcp_actions, "release_address",
                        lambda rid, region="": (calls.update(ip=rid, region=region), (True, "APPLIED"))[1])
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: pytest.fail("an IP must never be resized"))

    r.request_human_approval(
        "ip-1", "Release unused static IP", 7.2,
        action_type="release_address", action_params={"region": "us-central1"},
    )
    approve("ip-1")
    r.execute_approved_action("ip-1")

    assert calls == {"ip": "ip-1", "region": "us-central1"}


def test_service_approval_still_resizes(monkeypatch):
    calls = {}
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda rid, mem, **k: (calls.update(svc=rid, mem=mem), (True, "APPLIED"))[1])

    r.request_human_approval("svc-1", "Resize", 99.0, target_memory="256Mi")
    approve("svc-1")
    r.execute_approved_action("svc-1")

    assert calls == {"svc": "svc-1", "mem": "256Mi"}


def test_unknown_action_type_is_refused_not_guessed(monkeypatch):
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: pytest.fail("must not fall back to resizing"))

    r.request_human_approval("thing-1", "Do something", 10.0, action_type="teleport")
    approve("thing-1")
    result = r.execute_approved_action("thing-1")

    assert result.startswith("REFUSED")
    assert memory_bank.check_history("thing-1")["found"] is False


def test_a_failed_handler_books_no_savings(monkeypatch):
    monkeypatch.setattr(gcp_actions, "delete_disk", lambda rid, zone="": (False, "FAILED: 404"))

    r.delete_orphan_disk("disk-missing", estimated_savings=9.0, zone="z")
    approve("disk-missing")
    before = memory_bank.total_savings()
    result = r.execute_approved_action("disk-missing")

    assert result.startswith("FAILED")
    assert memory_bank.total_savings() == before


def test_dry_run_shows_the_right_api_for_a_disk(monkeypatch):
    """The dry-run payload must describe a disk delete, not a services.patch."""
    monkeypatch.setattr(settings, "DRY_RUN", True)
    ok, message = gcp_actions.delete_disk("orphan-disk-1", zone="us-central1-a")
    assert ok
    assert "disk" in message

    from app.core import trace as trace_mod

    step = trace_mod.tracer.steps()[-1]
    sent = step["detail"]["would_send"]
    assert sent["method"] == "disks.delete"
    assert sent["disk"] == "orphan-disk-1"
    assert "services.patch" not in str(sent)
