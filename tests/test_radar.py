"""The efficiency radar must reward an optimised estate.

Regression: the axes plotted raw CPU and memory utilization, which inverted the
meaning — a service correctly scaling to zero sits at ~1% CPU and dragged the
score down, so a perfectly optimised fleet scored 25%.
"""

from app.tools.gcp_metrics import _efficiency_radar


def resource(**over):
    base = {
        "resource_id": "svc", "type": "Cloud Run", "status": "Healthy",
        "monthly_cost": 10.0, "wasted_cost": 0.0,
        "cpu_utilization": 1.0, "memory_utilization": 2.0,
        "min_instances": 0, "metrics_source": "monitoring",
    }
    base.update(over)
    return base


def scores(estate, resources=None, remediations=None):
    return {
        a["axis"].replace("radar.", ""): a["value"]
        for a in _efficiency_radar(estate, resources or estate, remediations or [])
    }


def test_an_optimised_fleet_scores_high():
    """Everything right-sized, scaling to zero, measured, nothing orphaned."""
    fleet = [resource(resource_id=f"svc-{i}") for i in range(3)]
    s = scores(fleet)

    assert s["cost"] == 100.0
    assert s["rightsizing"] == 100.0
    assert s["scaling"] == 100.0
    assert s["observability"] == 100.0
    assert s["governance"] == 100.0
    assert sum(s.values()) / len(s) >= 95, "an optimised fleet must read as optimised"


def test_low_cpu_alone_no_longer_penalises():
    """A scale-to-zero service is idle by design; that is success, not waste."""
    idle_but_correct = [resource(cpu_utilization=0.5, memory_utilization=1.0)]
    assert scores(idle_but_correct)["scaling"] == 100.0


def test_an_always_on_idle_service_is_penalised():
    """min_instances > 0 with no load is the expensive kind of idle."""
    s = scores([resource(min_instances=2, cpu_utilization=1.0, status="Idle",
                         wasted_cost=90.0, monthly_cost=100.0)])
    assert s["scaling"] == 0.0
    assert s["rightsizing"] == 0.0
    assert s["cost"] == 10.0


def test_a_busy_always_on_service_is_not_penalised():
    """Always-on is fine when the traffic justifies it."""
    assert scores([resource(min_instances=2, cpu_utilization=70.0)])["scaling"] == 100.0


def test_modelled_metrics_lower_observability():
    s = scores([resource(metrics_source="modelled")])
    assert s["observability"] == 0.0


def test_orphans_lower_governance():
    estate = [resource(), resource(resource_id="disk", status="Orphaned")]
    assert scores(estate)["governance"] == 50.0


def test_nothing_to_fix_reads_as_fully_automated():
    """Zero findings must not read as zero automation."""
    assert scores([resource()])["automation"] == 100.0


def test_automation_reflects_what_was_actually_handled():
    estate = [resource(status="Idle", wasted_cost=50.0)]
    s = scores(estate, remediations=[{"savings": 1.0}, {"savings": 2.0}, {"savings": 3.0}])
    assert s["automation"] == 75.0  # 3 handled of 4 total


def test_every_axis_stays_within_bounds():
    """A radar polygon cannot render a value outside 0-100."""
    extreme = [
        resource(status="Idle", monthly_cost=1.0, wasted_cost=999.0, min_instances=5),
        resource(resource_id="d", status="Orphaned", monthly_cost=0.0, wasted_cost=0.0),
    ]
    for axis, value in scores(extreme).items():
        assert 0.0 <= value <= 100.0, f"{axis} out of range: {value}"


def test_an_empty_estate_does_not_divide_by_zero():
    for axis, value in scores([], resources=[]).items():
        assert 0.0 <= value <= 100.0
