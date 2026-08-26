"""What runs must be what was proposed, sized from measurements.

Two regressions found by replaying a real audit against the simulated fleet:

  1. The planner returns only the dimensions it cared about. A step meaning
     "scale to zero" carries `min_instances` and no `memory`, and the executor
     filled the gap with a hardcoded "512Mi". So a ticket reading "downsize to
     1 vCPU and 2Gi" applied 512Mi instead — a 4x smaller allocation than the
     human approved, and on `ml-inference` (5 GB observed peak) an OOM.

  2. The autonomy thresholds and the booked savings were tested against the
     model's `estimated_saving` rather than the measured `wasted_cost`, so the
     dashboard reported a saving the cost model never computed.
"""

import pytest

from app.core.config import settings
from app.core.executor import PlanExecutor
from app.tools import gcp_actions, gcp_remediator as r
from app.tools.memory_tools import memory_bank

# A measured Cloud Run service: 2 vCPU / 4Gi, 2 warm instances, low peaks.
# Deterministic sizing puts this at 1 vCPU / 2Gi with $148.15/mo recoverable.
CHECKOUT = {
    "resource_id": "checkout-api",
    "type": "Cloud Run",
    "cpu_limit": "2",
    "memory_limit": "4Gi",
    "min_instances": 2,
    "cpu_utilization": 20.4,
    "memory_utilization": 31.4,
    "monthly_cost": 304.84,
    "wasted_cost": 148.15,
    "status": "Oversized",
    "severity": "HIGH",
}

# The shape of a real plan step: the model asked to scale down and named the
# CPU and min-instances, but never mentioned memory.
STEP_WITHOUT_MEMORY = {
    "order": 1,
    "resource_id": "checkout-api",
    "tool": "resize_service",
    "args": {"cpu": "1", "min_instances": 0, "region": "us-central1"},
    "intent": "Reduce allocation and scale to zero.",
    "expected_outcome": "checkout-api runs on 1 vCPU and 2Gi with min_instances 0.",
    "estimated_saving": 250.0,
}


@pytest.fixture
def captured_resize(monkeypatch):
    """Record what would actually be sent to Cloud Run."""
    seen = {}

    def fake(rid, memory, new_cpu="", new_min_instances=None):
        seen.update(id=rid, memory=memory, cpu=new_cpu, min_instances=new_min_instances)
        return True, "APPLIED"

    monkeypatch.setattr(gcp_actions, "resize_service", fake)
    return seen


# --- 1. the missing dimension comes from the sizing, never from a constant ---
def test_memory_absent_from_the_plan_is_sized_not_defaulted():
    shape = PlanExecutor(fleet=[CHECKOUT])._target_shape(
        "checkout-api", STEP_WITHOUT_MEMORY["args"]
    )

    assert shape["memory"] == "2Gi", (
        "memory the plan omitted must come from the deterministic sizing "
        f"for this resource, not from a hardcoded default (got {shape['memory']})"
    )
    assert shape["cpu"] == "1", "an explicit plan argument still wins"
    assert shape["min_instances"] == 0, "min_instances 0 is a value, not an absence"


def test_an_unknown_resource_yields_no_shape_at_all():
    """Better to skip than to invent a shape for something we never measured."""
    assert PlanExecutor(fleet=[])._target_shape("ghost", {"cpu": "1"}) is None


def test_a_step_with_no_resolvable_shape_is_skipped_not_applied(captured_resize):
    results, failures = PlanExecutor(fleet=[]).run({"steps": [STEP_WITHOUT_MEMORY]})

    assert results[0]["status"] == "skipped"
    assert not captured_resize, "nothing may be sent to GCP without a resolved shape"


# --- 2. the ticket a human reads is the call that runs -----------------------
def test_the_ticket_carries_the_shape_that_will_be_applied(captured_resize):
    PlanExecutor(fleet=[CHECKOUT]).run({"steps": [STEP_WITHOUT_MEMORY]})

    ticket = memory_bank.snapshot()["approvals"][0]
    assert ticket["target_shape"] == {"memory": "2Gi", "cpu": "1", "min_instances": 0}
    assert "2Gi" in ticket["proposed_action"], (
        "the human must be able to read the target shape off the ticket: "
        f"{ticket['proposed_action']!r}"
    )
    assert not captured_resize, "a Level 2 ticket must not execute on its own"


def test_approving_applies_the_whole_shape(captured_resize):
    PlanExecutor(fleet=[CHECKOUT]).run({"steps": [STEP_WITHOUT_MEMORY]})
    memory_bank.resolve_approval("checkout-api", "APPROVED")
    r.execute_approved_action("checkout-api")

    assert captured_resize["memory"] == "2Gi", (
        "the approved memory must be applied — this is the bug: the ticket said "
        "2Gi and 512Mi was sent"
    )
    assert captured_resize["cpu"] == "1"
    assert captured_resize["min_instances"] == 0


def test_a_ticket_with_no_target_memory_refuses_to_execute(captured_resize):
    r.request_human_approval(
        "orphan-ticket", "Resize something", 99.0,
        target_memory="", action_type="resize_service",
    )
    memory_bank.resolve_approval("orphan-ticket", "APPROVED")
    message = r.execute_approved_action("orphan-ticket")

    assert message.startswith("REFUSED")
    assert not captured_resize, "a default must never stand in for an approved value"


# --- 3. thresholds and booked savings follow the measurement -----------------
def test_the_measured_waste_decides_the_autonomy_level():
    executor = PlanExecutor(fleet=[CHECKOUT])
    assert executor._saving("checkout-api", STEP_WITHOUT_MEMORY) == 148.15, (
        "the model guessed $250; the cost model measured $148.15, and the "
        "thresholds are defined against the measurement"
    )


def test_the_model_estimate_is_only_a_fallback():
    executor = PlanExecutor(fleet=[])
    assert executor._saving("checkout-api", STEP_WITHOUT_MEMORY) == 250.0


def test_the_booked_saving_is_the_measured_one(captured_resize):
    PlanExecutor(fleet=[CHECKOUT]).run({"steps": [STEP_WITHOUT_MEMORY]})

    ticket = memory_bank.snapshot()["approvals"][0]
    assert ticket["estimated_roi"] == 148.15, (
        "the dashboard books this figure as realised savings, so it must be the "
        "measured one rather than the model's estimate"
    )


def test_a_cheap_step_is_skipped_on_the_measured_figure(captured_resize):
    """The model may overstate a saving; the threshold still uses the measurement."""
    cheap = {**CHECKOUT, "resource_id": "tiny", "wasted_cost": 0.40}
    step = {**STEP_WITHOUT_MEMORY, "resource_id": "tiny", "estimated_saving": 900.0}

    results, _ = PlanExecutor(fleet=[cheap]).run({"steps": [step]})

    assert results[0]["status"] == "skipped"
    assert f"{settings.MIN_SAVINGS_THRESHOLD:.2f}" in results[0]["message"]
    assert not captured_resize
