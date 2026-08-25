"""Real-data guarantees.

The product must never present invented infrastructure as real. These tests
pin that contract.
"""

import pytest

from app.core.config import settings
from app.tools import gcp_metrics


@pytest.fixture(autouse=True)
def clear_cache():
    gcp_metrics._services_cache.clear()
    gcp_metrics._utilization_cache.clear()
    yield
    gcp_metrics._services_cache.clear()
    gcp_metrics._utilization_cache.clear()


def test_an_empty_project_is_a_real_answer_not_a_fallback(monkeypatch):
    """Regression: a successful API call returning zero services used to be
    treated as failure and replaced with simulated data."""
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr("app.tools.gcp_inventory.discover_cloud_run", lambda: ([], []))

    services, source = gcp_metrics.fetch_services(force_refresh=True)
    assert services == []
    assert source == "gcp", "an empty project must report as real GCP data"


def test_api_failure_surfaces_as_error_not_fake_data(monkeypatch):
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(settings, "ALLOW_SIMULATED_FALLBACK", False)
    monkeypatch.setattr(
        "app.tools.gcp_inventory.discover_cloud_run",
        lambda: ([], [{"source": "Cloud Run", "reason": "permission_denied", "detail": "nope"}]),
    )

    services, source = gcp_metrics.fetch_services(force_refresh=True)
    assert source == "error"
    assert services == []
    assert gcp_metrics.last_problems()[0]["reason"] == "permission_denied"


def test_simulated_fallback_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(settings, "ALLOW_SIMULATED_FALLBACK", True)
    monkeypatch.setattr(
        "app.tools.gcp_inventory.discover_cloud_run",
        lambda: ([], [{"source": "Cloud Run", "reason": "error", "detail": "boom"}]),
    )

    services, source = gcp_metrics.fetch_services(force_refresh=True)
    assert source == "simulated"
    assert services


def test_live_audit_never_reports_hardcoded_images(monkeypatch):
    """Regression: untagged_images was a hardcoded literal, so a 'real' audit
    reported a resource that did not exist."""
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr("app.tools.gcp_inventory.discover_cloud_run", lambda: ([], []))
    monkeypatch.setattr(
        "app.tools.gcp_inventory.discover_all",
        lambda **_: {
            "cloud_run": [], "orphan_disks": [], "unused_addresses": [],
            "untagged_images": [], "problems": [], "scanned_regions": ["us-central1"],
        },
    )

    data = gcp_metrics.get_infrastructure_anomalies()
    assert data["untagged_images"] == []
    assert "legacy-build" not in str(data)


def test_min_instances_drive_cost_and_idle_detection(monkeypatch):
    """A scale-to-zero service is cheap; an always-on one with no load is not."""
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(
        "app.tools.gcp_inventory.discover_cloud_run",
        lambda: (
            [
                {"resource_id": "always-on", "region": "us-central1", "cpu_limit": "1",
                 "memory_limit": "2Gi", "min_instances": 2, "max_instances": 10},
                {"resource_id": "scale-to-zero", "region": "us-central1", "cpu_limit": "1",
                 "memory_limit": "2Gi", "min_instances": 0, "max_instances": 10},
            ],
            [],
        ),
    )
    monkeypatch.setattr(gcp_metrics, "get_utilization", lambda _r: {"cpu": 0.01, "memory": 0.01})

    resources, source = gcp_metrics.describe_resources()
    assert source == "gcp"
    by_id = {r["resource_id"]: r for r in resources}

    assert by_id["always-on"]["monthly_cost"] > by_id["scale-to-zero"]["monthly_cost"]
    assert by_id["always-on"]["status"] == "Idle"


def test_regions_always_include_the_primary():
    assert settings.regions[0] == settings.REGION
    assert len(set(settings.regions)) == len(settings.regions), "no duplicate regions"


def test_trivial_savings_are_reported_not_escalated(monkeypatch):
    """A $1/month idle service must not consume a human approval slot."""
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(settings, "MIN_SAVINGS_THRESHOLD", 5.0)
    monkeypatch.setattr(
        "app.tools.gcp_inventory.discover_cloud_run",
        lambda: (
            [{"resource_id": "tiny", "region": "us-central1", "cpu_limit": "100m",
              "memory_limit": "128Mi", "min_instances": 0, "max_instances": 1}],
            [],
        ),
    )
    monkeypatch.setattr(
        "app.tools.gcp_inventory.discover_all",
        lambda **_: {"cloud_run": [], "orphan_disks": [], "unused_addresses": [],
                     "untagged_images": [], "problems": [], "scanned_regions": ["us-central1"]},
    )
    monkeypatch.setattr(gcp_metrics, "get_utilization", lambda _r: {"cpu": 0.01, "memory": 0.01})

    data = gcp_metrics.get_infrastructure_anomalies()
    assert data["idle_services"] == [], "trivial waste must not become an action item"
    assert len(data["below_threshold"]) == 1
    assert data["below_threshold"][0]["resource_id"] == "tiny"


def test_severity_follows_the_money(monkeypatch):
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(
        "app.tools.gcp_inventory.discover_cloud_run",
        lambda: (
            [{"resource_id": "expensive", "region": "us-central1", "cpu_limit": "4",
              "memory_limit": "8Gi", "min_instances": 3, "max_instances": 10},
             {"resource_id": "cheap", "region": "us-central1", "cpu_limit": "100m",
              "memory_limit": "128Mi", "min_instances": 0, "max_instances": 1}],
            [],
        ),
    )
    monkeypatch.setattr(gcp_metrics, "get_utilization", lambda _r: {"cpu": 0.01, "memory": 0.01})

    by_id = {r["resource_id"]: r for r in gcp_metrics.describe_resources()[0]}
    assert by_id["expensive"]["severity"] == "HIGH"
    assert by_id["cheap"]["severity"] == "LOW"


def test_an_explicit_audit_re_queries_gcp(monkeypatch):
    """Regression: within the TTL a re-scan served the cache, so every audit
    produced identical findings and looked frozen."""
    from app.tools import gcp_inventory

    calls = []

    def counted():
        calls.append(1)
        return ([{"resource_id": f"svc-{len(calls)}", "region": "us-central1",
                  "cpu_limit": "1", "memory_limit": "512Mi", "min_instances": 0,
                  "max_instances": 1}], [])

    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(gcp_inventory, "discover_cloud_run", counted)
    monkeypatch.setattr(
        gcp_inventory, "discover_all",
        lambda **_: {"cloud_run": [], "orphan_disks": [], "unused_addresses": [],
                     "untagged_images": [], "problems": [], "scanned_regions": []},
    )

    gcp_metrics.get_infrastructure_anomalies(force_refresh=True)
    gcp_metrics.get_infrastructure_anomalies(force_refresh=True)
    assert len(calls) == 2, "each explicit audit must hit GCP again"

    gcp_metrics.get_infrastructure_anomalies(force_refresh=False)
    assert len(calls) == 2, "a non-forced read may still use the cache"


def test_below_threshold_resources_read_as_settled_not_as_problems(monkeypatch):
    """A resource with $1/mo recoverable exists and is fine. Showing it as a
    problem implies an action that will never be taken."""
    monkeypatch.setattr(settings, "MOCK_MODE", False)
    monkeypatch.setattr(settings, "MIN_SAVINGS_THRESHOLD", 5.0)
    monkeypatch.setattr(
        "app.tools.gcp_inventory.discover_cloud_run",
        lambda: ([{"resource_id": "tiny", "region": "us-central1", "cpu_limit": "100m",
                   "memory_limit": "128Mi", "min_instances": 0, "max_instances": 1}], []),
    )
    monkeypatch.setattr(gcp_metrics, "get_utilization", lambda _r: {"cpu": 0.01, "memory": 0.01})

    resource = gcp_metrics.describe_resources()[0][0]
    assert resource["status"] == "Tolerated"
    assert resource["severity"] == "LOW"
    assert resource in gcp_metrics.get_active_resources(), (
        "a tolerated resource is an active, healthy part of the fleet"
    )


def test_a_tolerated_resource_gets_a_clean_bill_of_health(monkeypatch):
    from app.tools.rationale import explain

    monkeypatch.setattr(settings, "MIN_SAVINGS_THRESHOLD", 5.0)
    why = explain({
        "resource_id": "tiny", "type": "Cloud Run", "region": "us-central1",
        "cpu_limit": "100m", "memory_limit": "128Mi", "min_instances": 0,
        "cpu_utilization": 1.0, "memory_utilization": 1.0,
        "monthly_cost": 1.33, "wasted_cost": 1.0,
        "status": "Tolerated", "severity": "LOW", "metrics_source": "monitoring",
    })
    assert why["status"] == "Tolerated"
    assert why["rule"] is None, "nothing was flagged"
    assert why["sizing"] is None, "no change is proposed"
    assert why["savings"] == 0.0
    assert "threshold" in why["diagnosis"]
