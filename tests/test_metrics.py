import pytest

from app.tools import gcp_metrics as m


@pytest.mark.parametrize(
    "value,expected", [("500m", 0.5), ("2", 2.0), ("1000m", 1.0), ("bogus", 1.0)]
)
def test_parse_cpu(value, expected):
    assert m.parse_cpu(value) == expected


@pytest.mark.parametrize(
    "value,expected", [("512Mi", 0.5), ("2Gi", 2.0), ("1024Mi", 1.0), ("nope", 0.5)]
)
def test_parse_memory(value, expected):
    assert m.parse_memory_gib(value) == expected


def test_cost_grows_with_allocation():
    small = m.calculate_monthly_cost("1", "512Mi")
    large = m.calculate_monthly_cost("4", "8Gi")
    assert 0 < small < large


def test_utilization_is_deterministic():
    """Charts must not jitter between 5-second polls."""
    assert m.get_utilization("checkout-api") == m.get_utilization("checkout-api")


def test_describe_resources_classifies_and_ranks():
    resources, source = m.describe_resources()
    assert source == "simulated"
    assert resources, "expected the simulated fleet"
    costs = [r["monthly_cost"] for r in resources]
    assert costs == sorted(costs, reverse=True)
    assert {r["status"] for r in resources} <= {"Healthy", "Oversized", "Idle"}
    for r in resources:
        assert 0 <= r["wasted_cost"] <= r["monthly_cost"]


def test_services_are_cached():
    m.fetch_services(force_refresh=True)
    first = m.fetch_services()
    second = m.fetch_services()
    assert first is second  # same cached tuple, no second API round-trip


def test_charts_have_every_series():
    resources, _ = m.describe_resources()
    charts = m.build_charts(resources, [])
    assert set(charts) == {
        "ranking", "distribution", "trend", "radar", "savings_curve", "trend_source",
    }
    assert charts["trend_source"] in ("monitoring", "modelled")
    assert len(charts["radar"]) == 6
    assert len(charts["trend"]) == 24
    assert all(0 <= a["value"] <= 100 for a in charts["radar"])


def test_charts_cover_the_whole_estate_not_just_cloud_run():
    """Regression: KPIs counted disks and IPs while the charts silently omitted
    them, so the dashboard contradicted itself."""
    cloud_run = [
        {"resource_id": "svc", "monthly_cost": 100.0, "wasted_cost": 50.0,
         "status": "Idle", "cpu_utilization": 5.0, "memory_utilization": 5.0},
    ]
    estate = cloud_run + [
        {"resource_id": "disk", "monthly_cost": 20.0, "wasted_cost": 20.0, "status": "Orphaned",
         "cpu_utilization": 0.0, "memory_utilization": 0.0},
        {"resource_id": "ip", "monthly_cost": 7.2, "wasted_cost": 7.2, "status": "Unused",
         "cpu_utilization": 0.0, "memory_utilization": 0.0},
    ]

    charts = m.build_charts(cloud_run, [], inventory=estate)
    labels = {r["label"] for r in charts["ranking"]}
    assert {"svc", "disk", "ip"} <= labels

    states = {d["label"] for d in charts["distribution"]}
    assert {"Idle", "Orphaned", "Unused"} == states
    assert round(sum(d["value"] for d in charts["distribution"]), 2) == 127.20


def test_ranking_is_ordered_by_cost():
    estate = [
        {"resource_id": "cheap", "monthly_cost": 1.0, "wasted_cost": 1.0, "status": "Idle",
         "cpu_utilization": 0.0, "memory_utilization": 0.0},
        {"resource_id": "pricey", "monthly_cost": 90.0, "wasted_cost": 10.0, "status": "Idle",
         "cpu_utilization": 0.0, "memory_utilization": 0.0},
    ]
    ranking = m.build_charts([], [], inventory=estate)["ranking"]
    assert [r["label"] for r in ranking] == ["pricey", "cheap"]
