"""The agent must be able to justify every recommendation it makes."""

import pytest

from app.core.config import settings
from app.tools import rationale


def make(**over):
    base = {
        "resource_id": "svc", "region": "us-central1", "type": "Cloud Run",
        "cpu_limit": "2", "memory_limit": "4Gi", "min_instances": 2,
        "cpu_utilization": 3.0, "memory_utilization": 8.0,
        "monthly_cost": 300.0, "wasted_cost": 200.0,
        "status": "Idle", "severity": "HIGH", "metrics_source": "monitoring",
    }
    base.update(over)
    return base


def test_explanation_is_complete():
    why = rationale.explain(make())
    for key in ("evidence", "rule", "diagnosis", "sizing", "solution",
                "command", "expected_result", "autonomy", "confidence"):
        assert why.get(key) is not None, f"missing {key}"


def test_every_evidence_row_names_its_source():
    """A number without provenance is an assertion, not evidence."""
    for row in rationale.explain(make())["evidence"]:
        assert row["source"], f"{row['label']} has no source"
        assert row["value"]


def test_always_on_idle_is_its_own_rule():
    why = rationale.explain(make(min_instances=3, cpu_utilization=1.0))
    assert why["rule"]["id"] == "IDLE_ALWAYS_ON"
    assert "24/7" in str(why["evidence"])


def test_recommendation_never_grows_a_resource():
    why = rationale.explain(make(cpu_utilization=99.0, memory_utilization=99.0))
    sizing = why["sizing"]
    assert sizing["target"]["memory"] == sizing["current"]["memory"]
    assert sizing["target"]["cpu"] == sizing["current"]["cpu"]


def test_reduction_is_capped_per_audit():
    """A 16x cut on thin evidence is a guess. Converge in steps instead."""
    why = rationale.explain(make(memory_limit="8Gi", memory_utilization=0.5))
    assert why["sizing"]["target"]["memory"] == "2Gi"  # 8Gi / 4, not 128Mi
    assert why["capped"]


def test_never_recommends_below_the_floor():
    why = rationale.explain(make(memory_limit="512Mi", memory_utilization=0.1))
    assert why["sizing"]["target"]["memory"] == "256Mi"


def test_a_busy_service_is_left_alone():
    """90% memory use leaves no safe headroom to reclaim."""
    why = rationale.explain(make(memory_limit="32Gi", memory_utilization=90.0))
    assert why["sizing"]["target"]["memory"] == "32Gi"


def test_cpu_respects_cloud_run_minimum_for_large_memory():
    """Cloud Run requires >= 2 vCPU at 8Gi; a recommendation must stay valid."""
    why = rationale.explain(
        make(cpu_limit="8", memory_limit="32Gi", cpu_utilization=0.1, memory_utilization=1.0)
    )
    target = why["sizing"]["target"]
    assert target["memory"] == "8Gi", "capped at a 4x reduction"
    assert float(target["cpu"]) >= 2.0, "8Gi requires at least 2 vCPU"


def test_idle_service_gets_min_instances_zeroed():
    why = rationale.explain(make(status="Idle", min_instances=4))
    assert why["sizing"]["target"]["min_instances"] == 0


def test_modelled_metrics_lower_the_confidence():
    why = rationale.explain(make(metrics_source="modelled"))
    assert why["confidence"]["level"] == "low"
    assert "modelled" in why["confidence"]["reason"].lower()


def test_healthy_resource_recommends_nothing():
    why = rationale.explain(make(status="Healthy", wasted_cost=0.0))
    assert why["verdict"] == "Healthy"
    assert why["rule"] is None
    assert why["sizing"] is None
    assert why["savings"] == 0.0


@pytest.mark.parametrize(
    "waste,expected_level",
    [(1.0, "None"), (20.0, "Level 1"), (200.0, "Level 2")],
)
def test_autonomy_level_follows_the_savings(waste, expected_level):
    assert rationale.explain(make(wasted_cost=waste))["autonomy"]["level"] == expected_level


def test_command_is_runnable_and_targets_the_right_project():
    why = rationale.explain(make())
    cmd = why["command"]
    assert "gcloud run services update svc" in cmd
    assert f"--project={settings.PROJECT_ID}" in cmd
    assert "--region=us-central1" in cmd
    assert why["sizing"]["target"]["memory"] in cmd


def test_expected_result_quantifies_the_yearly_impact():
    why = rationale.explain(make(wasted_cost=100.0))
    assert "1200.00/year" in why["expected_result"]


# --- Non-Cloud-Run resources ---------------------------------------------
def test_orphan_disk_is_fully_explained():
    disk = {
        "resource_id": "orphan-1", "type": "Persistent Disk", "zone": "us-central1-a",
        "size_gb": 100.0, "disk_type": "pd-ssd", "monthly_cost": 17.0,
    }
    why = rationale.explain(disk)
    assert why["verdict"] == "Orphaned"
    assert why["rule"]["id"] == "ORPHANED_DISK"
    assert "snapshot" in why["command"].lower(), "must offer a safety net before deleting"
    assert "gcloud compute disks delete orphan-1" in why["command"]
    assert why["autonomy"]["level"] == "Level 2"


def test_disk_deletion_always_needs_a_human_even_when_cheap():
    """Irreversibility, not savings, drives this one."""
    disk = {
        "resource_id": "tiny", "type": "Persistent Disk", "zone": "us-central1-a",
        "size_gb": 1.0, "disk_type": "pd-standard", "monthly_cost": 0.04,
    }
    assert rationale.explain(disk)["autonomy"]["level"] == "Level 2"


def test_unused_ip_explains_the_inverted_pricing():
    addr = {
        "resource_id": "ip-1", "type": "Static IP", "region": "us-central1",
        "address": "34.1.2.3", "monthly_cost": 7.20,
    }
    why = rationale.explain(addr)
    assert why["rule"]["id"] == "UNUSED_STATIC_IP"
    assert "idle" in why["rule"]["why_it_matters"]
    assert "DNS" in why["autonomy"]["reason"], "must warn about dangling references"


def test_untagged_image_is_level_1():
    image = {
        "resource_id": "projects/p/versions/sha256:abc", "short_id": "sha256:abc",
        "type": "Container Image", "repository": "builds", "monthly_cost": 0.10,
        "created": "2026-01-01T00:00:00Z",
    }
    why = rationale.explain(image)
    assert why["autonomy"]["level"] == "Level 1"
    assert why["verdict"] == "Untagged"


def test_every_resource_type_produces_a_complete_explanation():
    """No type may return a rationale the drawer cannot render."""
    samples = [
        make(),
        {"resource_id": "d", "type": "Persistent Disk", "zone": "z", "size_gb": 10.0,
         "disk_type": "pd-standard", "monthly_cost": 0.4},
        {"resource_id": "a", "type": "Static IP", "region": "r", "address": "1.2.3.4",
         "monthly_cost": 7.2},
        {"resource_id": "i", "type": "Container Image", "short_id": "sha256:x",
         "repository": "repo", "monthly_cost": 0.1, "created": "2026-01-01T00:00:00Z"},
    ]
    for sample in samples:
        why = rationale.explain(sample)
        for key in ("evidence", "rule", "solution", "command", "expected_result",
                    "autonomy", "confidence", "savings"):
            assert why.get(key) is not None, f"{sample['type']} missing {key}"
        assert all(e["source"] for e in why["evidence"])
