"""Planning: the model chooses the actions; the executor enforces the limits."""

import json

import pytest

from app.core.config import settings
from app.core.executor import PlanExecutor
from app.core.planner import TOOLBOX, VALID_TOOLS, Planner
from app.tools.memory_tools import memory_bank


def planner_returning(payload):
    class Models:
        calls = 0

        def generate_content(self, model, contents, config):
            Models.calls += 1
            return type("R", (), {"text": json.dumps(payload)})()

    return Planner(type("C", (), {"models": Models()})(), "m"), Models


def test_a_plan_is_ordered_by_the_model():
    p, _ = planner_returning({
        "goal": "cut idle spend",
        "steps": [
            {"order": 2, "resource_id": "b", "tool": "skip", "intent": "fine",
             "expected_outcome": "nothing", "estimated_saving": 0.0},
            {"order": 1, "resource_id": "a", "tool": "resize_service", "intent": "shrink",
             "expected_outcome": "cheaper", "estimated_saving": 50.0},
        ],
    })
    plan = p.plan({"by_resource": {}}, [], [])
    assert [s["order"] for s in plan["steps"]] == [1, 2]
    assert plan["goal"] == "cut idle spend"


def test_a_hallucinated_tool_is_dropped_not_dispatched():
    """An unknown tool must never reach the executor: that is how an agent
    silently does nothing while reporting success."""
    p, _ = planner_returning({
        "goal": "g",
        "steps": [
            {"order": 1, "resource_id": "a", "tool": "reboot_universe", "intent": "x",
             "expected_outcome": "y", "estimated_saving": 5.0},
            {"order": 2, "resource_id": "b", "tool": "skip", "intent": "x",
             "expected_outcome": "y", "estimated_saving": 0.0},
        ],
    })
    plan = p.plan({"by_resource": {}}, [], [])
    assert [s["tool"] for s in plan["steps"]] == ["skip"]
    assert plan["rejected_steps"] == ["reboot_universe"]


def test_planning_needs_one_call():
    p, models = planner_returning({"goal": "g", "steps": []})
    p.plan({"by_resource": {}}, [{"resource_id": f"r{i}"} for i in range(20)], [])
    assert models.calls == 1


def test_an_unavailable_planner_returns_none():
    class Broken:
        def generate_content(self, **kw):
            raise RuntimeError("503")

    assert Planner(type("C", (), {"models": Broken()})(), "m").plan(
        {"by_resource": {}}, [], []
    ) is None


def test_the_toolbox_and_schema_agree():
    """The model may only pick a tool the executor can dispatch."""
    assert VALID_TOOLS == {t["tool"] for t in TOOLBOX}


# --- Execution enforces the matrix ---------------------------------------
def step(resource_id, tool, saving, **args):
    return {"order": 1, "resource_id": resource_id, "tool": tool, "args": args,
            "intent": "do the thing", "expected_outcome": "cheaper",
            "estimated_saving": saving}


def test_an_irreversible_step_always_needs_a_human(monkeypatch):
    """Above the action threshold, an irreversible step is never automatic —
    however small the saving and however confident the plan."""
    executor = PlanExecutor()
    barely_worth_it = settings.MIN_SAVINGS_THRESHOLD + 0.01
    results, failures = executor.run(
        {"goal": "g",
         "steps": [step("disk-1", "delete_disk", barely_worth_it, zone="us-central1-a")]}
    )
    assert results[0]["status"] == "awaiting_approval"
    assert memory_bank.has_pending_approval("disk-1") is True
    assert not failures


def test_a_trivially_cheap_irreversible_step_is_not_escalated():
    """Waking a human for $0.50/month costs more than it saves."""
    results, _ = PlanExecutor().run(
        {"goal": "g", "steps": [step("disk-tiny", "delete_disk", 0.50, zone="z")]}
    )
    assert results[0]["status"] == "skipped"
    assert memory_bank.has_pending_approval("disk-tiny") is False


def test_a_high_value_step_needs_a_human(monkeypatch):
    executor = PlanExecutor()
    saving = settings.HIGH_RISK_ROI_THRESHOLD + 10
    results, _ = executor.run(
        {"goal": "g", "steps": [step("svc-big", "resize_service", saving, memory="512Mi")]}
    )
    assert results[0]["status"] == "awaiting_approval"
    assert memory_bank.check_history("svc-big")["found"] is False


def test_a_low_value_step_runs_directly(monkeypatch):
    from app.tools import gcp_actions

    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: (True, "APPLIED"))
    executor = PlanExecutor()
    results, _ = executor.run(
        {"goal": "g", "steps": [step("svc-small", "resize_service", 10.0,
                                     memory="512Mi", min_instances=0)]}
    )
    assert results[0]["status"] == "done"
    assert memory_bank.check_history("svc-small")["found"] is True


def test_a_step_below_the_threshold_is_skipped():
    executor = PlanExecutor()
    results, _ = executor.run(
        {"goal": "g", "steps": [step("svc-tiny", "resize_service", 1.0, memory="512Mi")]}
    )
    assert results[0]["status"] == "skipped"
    assert "threshold" in results[0]["message"]


def test_the_plan_carries_the_full_shape_to_the_tool(monkeypatch):
    captured = {}
    from app.tools import gcp_actions

    monkeypatch.setattr(
        gcp_actions, "resize_service",
        lambda rid, mem, new_cpu="", new_min_instances=None: (
            captured.update(id=rid, mem=mem, cpu=new_cpu, min_i=new_min_instances),
            (True, "APPLIED"),
        )[1],
    )
    PlanExecutor().run({"goal": "g", "steps": [
        step("svc-shape", "resize_service", 10.0, memory="256Mi", cpu="250m", min_instances=0)
    ]})
    assert captured == {"id": "svc-shape", "mem": "256Mi", "cpu": "250m", "min_i": 0}


def test_a_failure_is_reported_for_replanning(monkeypatch):
    from app.tools import gcp_actions

    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: (False, "FAILED to resize: PermissionDenied"))
    results, failures = PlanExecutor().run(
        {"goal": "g", "steps": [step("svc-bad", "resize_service", 10.0, memory="512Mi")]}
    )
    assert results[0]["status"] == "failed"
    assert failures[0]["resource_id"] == "svc-bad"
    assert "PermissionDenied" in failures[0]["error"]


def test_the_report_groups_by_outcome(monkeypatch):
    from app.tools import gcp_actions

    monkeypatch.setattr(gcp_actions, "resize_service", lambda *a, **k: (True, "APPLIED"))
    executor = PlanExecutor()
    plan = {"goal": "Reduce idle spend.", "steps": [
        step("svc-a", "resize_service", 10.0, memory="512Mi"),
        step("disk-a", "delete_disk", 20.0, zone="z"),
        step("svc-b", "skip", 0.0, reason="already right-sized"),
    ]}
    executor.run(plan)
    report = executor.report(plan)

    assert "Reduce idle spend." in report
    assert "**Applied**" in report and "svc-a" in report
    assert "**Awaiting approval**" in report and "disk-a" in report
    assert "**Skipped**" in report and "already right-sized" in report
