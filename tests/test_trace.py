"""The execution trace must record what actually happened, with real payloads."""

import pytest

from app.core import trace as trace_mod
from app.core.trace import DISCOVERY, EXECUTION, Tracer


@pytest.fixture
def tracer():
    return Tracer()


def test_steps_are_ordered_and_numbered(tracer):
    tracer.step(DISCOVERY, "first")
    tracer.step(DISCOVERY, "second")
    seqs = [s["seq"] for s in tracer.steps()]
    assert seqs == sorted(seqs) == [1, 2]


def test_since_returns_only_newer_steps(tracer):
    tracer.step(DISCOVERY, "a")
    tracer.step(DISCOVERY, "b")
    assert [s["message"] for s in tracer.steps(since=1)] == ["b"]


def test_buffer_is_bounded(tracer):
    for i in range(trace_mod.MAX_STEPS + 50):
        tracer.step(DISCOVERY, f"step {i}")
    assert len(tracer.steps()) == trace_mod.MAX_STEPS


def test_timed_records_duration_and_payloads(tracer):
    with tracer.timed(EXECUTION, "PATCH service") as step:
        step.add(request={"memory": "512Mi"}, response={"revision": "v2"})

    recorded = tracer.steps()[-1]
    assert recorded["status"] == "ok"
    assert recorded["duration_ms"] is not None
    assert recorded["detail"]["request"] == {"memory": "512Mi"}
    assert recorded["detail"]["response"] == {"revision": "v2"}


def test_timed_records_failures_without_swallowing_them(tracer):
    with pytest.raises(ValueError):
        with tracer.timed(EXECUTION, "PATCH service") as step:
            step.add(request={"memory": "512Mi"})
            raise ValueError("quota exceeded")

    recorded = tracer.steps()[-1]
    assert recorded["status"] == "error"
    assert "quota exceeded" in recorded["detail"]["error"]
    assert recorded["detail"]["request"] == {"memory": "512Mi"}, (
        "the attempted request must survive the failure"
    )


def test_dry_run_records_what_would_have_been_sent(monkeypatch):
    """Operators must be able to review the exact payload before enabling writes."""
    from app.core.config import settings
    from app.tools import gcp_actions

    monkeypatch.setattr(settings, "DRY_RUN", True)
    monkeypatch.setattr(trace_mod, "tracer", trace_mod.Tracer())
    monkeypatch.setattr(gcp_actions, "tracer", trace_mod.tracer)

    ok, message = gcp_actions.resize_service("svc", "512Mi")
    assert ok and message.startswith("DRY_RUN")

    step = trace_mod.tracer.steps()[-1]
    assert step["detail"]["dry_run"] is True
    sent = step["detail"]["would_send"]
    assert sent["method"] == "projects.locations.services.patch"
    assert sent["changes"]["memory"] == "512Mi"
    assert "svc" in sent["name"]


def test_subscriber_registration(tracer):
    queue = tracer.subscribe()
    assert tracer.subscriber_count == 1
    tracer.unsubscribe(queue)
    assert tracer.subscriber_count == 0


def test_publishing_without_a_loop_does_not_raise(tracer):
    tracer.subscribe()
    tracer.step(DISCOVERY, "no loop bound")  # must not explode


# --- Live state pushes ----------------------------------------------------
def test_state_notifications_are_distinguishable_from_steps(tracer):
    """The client must be able to tell a trace line from a refresh signal."""
    published = []
    tracer._publish = lambda msg, subs: published.append(msg)
    tracer.subscribe()

    tracer.step(DISCOVERY, "a step")
    tracer.notify_state_changed("approved:svc")

    assert published[0]["kind"] == "step"
    assert published[1] == {"kind": "state", "reason": "approved:svc"}


def test_a_state_notification_is_not_stored_as_a_step(tracer):
    tracer.subscribe()
    tracer.notify_state_changed("x")
    assert tracer.steps() == [], "a refresh signal is not part of the audit trail"


def test_notifying_with_no_subscribers_is_harmless(tracer):
    tracer.notify_state_changed("nobody listening")
