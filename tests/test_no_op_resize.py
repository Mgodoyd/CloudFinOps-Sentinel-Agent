"""A resize that changes nothing must not be applied, or counted.

Seen in production. The agent reported:

    resize_cloud_run -> 1Gi applied to legacy-payment-service (+$8.55/mo)
    resize_cloud_run -> 1Gi applied to cloudfinops-sentinel   (+$8.25/mo)

Both services were already at 1 vCPU / 1Gi. Cloud Run accepted the patch and
deployed a new revision with identical limits, the memory bank recorded a
remediation, and $16.80/month went onto "savings realized" — money that will
never appear on a bill. For an agent whose entire output is a savings figure,
that is the most damaging thing it can get wrong.

Two causes, both covered here: the plan proposing the shape the resource
already has, and nothing checking the result against the current shape before
sending it.
"""

import pytest

from app.core.executor import PlanExecutor
from app.tools import gcp_actions, rationale
from app.tools.memory_tools import memory_bank

# 1 vCPU / 1Gi at 36% CPU and 45.9% memory: oversized on the memory rule, but
# the sizing cap means there is no smaller valid memory step to move to.
NOTHING_TO_DO = {
    "resource_id": "legacy-payment-service", "type": "Cloud Run",
    "cpu_limit": "1", "memory_limit": "1Gi", "min_instances": 0,
    "cpu_utilization": 36.0, "memory_utilization": 45.9,
    "monthly_cost": 25.07, "wasted_cost": 8.55,
    "status": "Oversized", "severity": "MEDIUM",
}

# Same shape, lower CPU use: the sizing does find a real cut, 1 vCPU -> 500m.
REAL_CUT = {**NOTHING_TO_DO, "resource_id": "cloudfinops-sentinel",
            "cpu_utilization": 26.8, "memory_utilization": 35.8,
            "monthly_cost": 18.64, "wasted_cost": 8.24}


@pytest.fixture
def sent(monkeypatch):
    calls = []
    # cpu and min_instances arrive as keywords, so both halves are recorded.
    monkeypatch.setattr(gcp_actions, "resize_service",
                        lambda *a, **k: calls.append((a, k)) or (True, "APPLIED"))
    return calls


def step(rid, **args):
    return {"order": 1, "resource_id": rid, "tool": "resize_service",
            "args": args, "intent": "right-size", "expected_outcome": "cheaper",
            "estimated_saving": 8.55}


# --- 1. the comparison itself ----------------------------------------------
def test_shapes_are_compared_by_value_not_by_spelling():
    """"1" and "1000m" are the same CPU; "1Gi" and "1024Mi" the same memory.
    Comparing the strings would call a no-op a change."""
    a = {"cpu": "1", "memory": "1Gi", "min_instances": 0}
    assert rationale.same_shape(a, {"cpu": "1000m", "memory": "1024Mi", "min_instances": 0})
    assert not rationale.same_shape(a, {"cpu": "500m", "memory": "1Gi", "min_instances": 0})


# --- 2. a proposal that changes nothing is not a proposal -------------------
def test_a_no_op_argument_does_not_override_the_sizing():
    """The model answered "cpu: 1" for a service already at 1 vCPU. Letting
    that win silently discarded the reduction the sizing had found."""
    shape = rationale.merge_target_shape(
        {"cpu": "1"},
        {"cpu": "500m", "memory": "1Gi", "min_instances": 0},
        {"cpu": "1", "memory": "1Gi", "min_instances": 0},
    )
    assert shape["cpu"] == "500m", "the deterministic cut must survive a no-op argument"


def test_a_real_argument_still_wins():
    shape = rationale.merge_target_shape(
        {"cpu": "250m"},
        {"cpu": "500m", "memory": "1Gi", "min_instances": 0},
        {"cpu": "1", "memory": "1Gi", "min_instances": 0},
    )
    assert shape["cpu"] == "250m"


# --- 3. nothing is sent, and nothing is booked ------------------------------
def test_a_resize_to_the_current_shape_is_skipped(sent):
    results, _ = PlanExecutor(fleet=[NOTHING_TO_DO]).run(
        {"steps": [step("legacy-payment-service", cpu="1", memory="1Gi")]}
    )

    assert results[0]["status"] == "skipped"
    assert "already" in results[0]["message"]
    assert not sent, "Cloud Run must not be asked to deploy an identical revision"


def test_a_skipped_no_op_books_no_savings(sent):
    PlanExecutor(fleet=[NOTHING_TO_DO]).run(
        {"steps": [step("legacy-payment-service", cpu="1", memory="1Gi")]}
    )
    assert memory_bank.total_savings() == 0.0, (
        "this is the number the whole agent exists to produce; a change that "
        "changed nothing must not appear in it"
    )
    assert not memory_bank.snapshot()["remediations"]


def test_a_real_reduction_is_still_applied(sent):
    """The guard must not swallow genuine work: same starting shape, but here
    the sizing finds 1 vCPU -> 500m."""
    results, _ = PlanExecutor(fleet=[REAL_CUT]).run(
        {"steps": [step("cloudfinops-sentinel", cpu="1")]}
    )

    assert results[0]["status"] == "done"
    assert sent, "a real cut must reach Cloud Run"
    args, kwargs = sent[0]
    assert args == ("cloudfinops-sentinel", "1Gi")
    assert kwargs["new_cpu"] == "500m", "the reduction the sizing found"
    assert kwargs["new_min_instances"] == 0


# --- 4. shapes the API will actually accept ---------------------------------
ALWAYS_ALLOCATED = {
    "resource_id": "always-on", "type": "Cloud Run",
    "cpu_limit": "1", "memory_limit": "512Mi", "min_instances": 2,
    "cpu_utilization": 1.0, "memory_utilization": 5.0,
    "monthly_cost": 132.72, "wasted_cost": 99.57,
    "status": "Idle", "severity": "HIGH",
    "cpu_always_allocated": True,
}


def test_memory_stays_above_the_floor_when_cpu_is_always_allocated():
    """Cloud Run refuses under 512Mi with --no-cpu-throttling:

        400 Total memory < 512 Mi is not supported with cpu always allocated

    The sizing did not know the setting existed, so it proposed 256Mi from the
    observed peak and the API rejected it — on every audit, forever, because a
    failed remediation is retried rather than remembered as done.
    """
    target = rationale.recommend_sizing(ALWAYS_ALLOCATED)["target"]
    assert target["memory"] == "512Mi"


def test_the_floor_does_not_apply_to_a_throttled_service():
    """The limit is the service's setting, not a blanket rule. Applying it
    everywhere would block valid 256Mi recommendations."""
    throttled = {**ALWAYS_ALLOCATED, "cpu_always_allocated": False}
    assert rationale.recommend_sizing(throttled)["target"]["memory"] == "256Mi"


def test_an_unknown_setting_is_treated_as_throttled():
    """Only simulated data lacks the field; assuming the stricter limit there
    would change the demo for no reason."""
    unknown = {k: v for k, v in ALWAYS_ALLOCATED.items() if k != "cpu_always_allocated"}
    assert rationale.recommend_sizing(unknown)["target"]["memory"] == "256Mi"


def test_a_plan_naming_an_invalid_memory_is_raised_to_the_floor(sent):
    """The model can name a memory the API will refuse. Raising it keeps a
    valid change instead of failing the call."""
    executor = PlanExecutor(fleet=[ALWAYS_ALLOCATED])
    shape = executor._target_shape("always-on", {"memory": "256Mi", "min_instances": 0})

    assert shape["memory"] == "512Mi"


def test_the_real_saving_survives_the_clamp(sent):
    """Raising the memory must not swallow the change that mattered: this
    service bills 24/7 on two warm instances, and min-instances 0 is the win."""
    results, _ = PlanExecutor(fleet=[ALWAYS_ALLOCATED]).run(
        {"steps": [step("always-on", cpu="1", min_instances=0)]}
    )

    assert results[0]["status"] == "awaiting_approval", "$99.57 is over the threshold"
    ticket = memory_bank.snapshot()["approvals"][0]
    assert ticket["target_shape"] == {"memory": "512Mi", "cpu": "1", "min_instances": 0}
