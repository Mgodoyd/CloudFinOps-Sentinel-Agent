"""The model produces the judgement; code enforces the autonomy matrix."""

import json

import pytest

from app.core import analyst as analyst_mod
from app.core.agent import CloudFinOpsAgent
from app.core.analyst import FleetAnalyst, clear_analysis, last_analysis, store_analysis
from app.core.config import settings
from app.tools.memory_tools import memory_bank


class FakeModels:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, 0

    def generate_content(self, model, contents, config):
        self.calls += 1
        if self.error:
            raise self.error
        return type("R", (), {"text": json.dumps(self.payload)})()


def client_with(payload=None, error=None):
    models = FakeModels(payload, error)
    return type("C", (), {"models": models})(), models


def test_analysis_is_keyed_by_resource():
    client, _ = client_with({
        "analyses": [
            {"resource_id": "svc", "verdict": "wasteful", "diagnosis": "idle",
             "recommendation": "shrink", "risk": "cold starts",
             "confidence": "medium", "monthly_saving": 12.0},
        ],
        "fleet_summary": "one wasteful service",
    })
    result = FleetAnalyst(client, "m").analyse([{"resource_id": "svc"}])
    assert result["by_resource"]["svc"]["verdict"] == "wasteful"
    assert result["summary"] == "one wasteful service"


def test_one_call_covers_the_whole_fleet():
    """Per-resource calls would exhaust a free-tier minute on one audit."""
    client, models = client_with({"analyses": [], "fleet_summary": ""})
    FleetAnalyst(client, "m").analyse([{"resource_id": f"r{i}"} for i in range(10)])
    assert models.calls == 1


def test_an_unreachable_model_returns_none_not_a_guess():
    client, _ = client_with(error=RuntimeError("503 UNAVAILABLE"))
    assert FleetAnalyst(client, "m").analyse([{"resource_id": "svc"}]) is None


def test_no_client_means_no_analysis():
    assert FleetAnalyst(None, "m").analyse([{"resource_id": "svc"}]) is None


def test_only_measured_facts_are_sent(monkeypatch):
    """The model must judge measurements, not be handed a conclusion to echo."""
    captured = {}

    class Capturing:
        def generate_content(self, model, contents, config):
            captured["contents"] = contents
            return type("R", (), {"text": '{"analyses": [], "fleet_summary": ""}'})()

    resource = {
        "resource_id": "svc", "cpu_limit": "2", "memory_limit": "4Gi",
        "min_instances": 2, "cpu_utilization": 3.0, "memory_utilization": 8.0,
        "monthly_cost": 300.0, "status": "Idle", "metrics_source": "monitoring",
        "rationale": {"solution": "PRECOOKED ANSWER"},
    }
    FleetAnalyst(type("C", (), {"models": Capturing()})(), "m").analyse([resource])
    assert "PRECOOKED ANSWER" not in captured["contents"]
    assert "observed_cpu_peak_pct" in captured["contents"]


# --- Enforcement stays in code -------------------------------------------
def test_a_high_value_recommendation_still_needs_a_human(monkeypatch):
    """However confident the model is, Level 2 is decided by code."""
    a = CloudFinOpsAgent()
    analysis = {
        "model": "m", "summary": "s",
        "by_resource": {
            "svc-big": {
                "resource_id": "svc-big", "verdict": "wasteful",
                "diagnosis": "idle", "recommendation": "Set min-instances to 0",
                "target_memory": "256Mi", "risk": "cold start",
                "confidence": "high", "monthly_saving": 500.0,
            }
        },
    }
    data = {
        "idle_services": [{"resource_id": "svc-big", "severity": "HIGH",
                           "potential_savings": 500.0, "issue": "idle"}],
    }

    a._act_on_analysis(data, analysis)
    assert memory_bank.has_pending_approval("svc-big") is True
    assert memory_bank.check_history("svc-big")["found"] is False, (
        "a Level 2 action must not execute without a human"
    )


def test_the_ticket_headline_states_the_shape_that_will_be_applied(monkeypatch):
    """The model narrates changes it does not encode.

    Here it says "set min-instances to 0" in prose but returns no
    `target_min_instances`, so nothing would set it. The headline must describe
    the change that will actually run; the model's wording is kept alongside and
    shown in the reasoning drawer, where it explains rather than promises.
    """
    a = CloudFinOpsAgent()
    analysis = {
        "model": "m", "summary": "s",
        "by_resource": {
            "svc-x": {
                "resource_id": "svc-x", "verdict": "wasteful",
                "diagnosis": "min_instances=2 bills 24/7 at 1% CPU",
                "recommendation": "Set min-instances to 0 and memory to 512Mi",
                "target_memory": "512Mi", "risk": "cold starts",
                "confidence": "medium", "monthly_saving": 99.0,
            }
        },
    }
    a._act_on_analysis(
        {"idle_services": [{"resource_id": "svc-x", "severity": "HIGH",
                            "potential_savings": 99.0, "issue": "idle"}]},
        analysis,
    )
    ticket = memory_bank.pending_approvals()[0]
    assert ticket["proposed_action"] == "Resize to 512Mi", (
        "the headline is the contract; the prose promised a min-instances change "
        "the model never encoded"
    )
    assert ticket["model_recommendation"] == "Set min-instances to 0 and memory to 512Mi"
    assert "bills 24/7" in ticket["detailed_reason"]
    assert ticket["target_memory"] == "512Mi"
    assert ticket["target_shape"]["memory"] == "512Mi"


def test_the_model_can_veto_an_action():
    """If the model judges an allocation acceptable, nothing is proposed."""
    a = CloudFinOpsAgent()
    analysis = {
        "model": "m", "summary": "s",
        "by_resource": {
            "svc-ok": {"resource_id": "svc-ok", "verdict": "acceptable",
                       "diagnosis": "sized correctly", "recommendation": "leave it",
                       "risk": "none", "confidence": "high", "monthly_saving": 0.0},
        },
    }
    a._act_on_analysis(
        {"idle_services": [{"resource_id": "svc-ok", "severity": "HIGH",
                            "potential_savings": 99.0, "issue": "idle"}]},
        analysis,
    )
    assert memory_bank.has_pending_approval("svc-ok") is False
    assert memory_bank.check_history("svc-ok")["found"] is False


def test_the_report_attributes_the_analysis():
    a = CloudFinOpsAgent()
    report = a._act_on_analysis(
        {"idle_services": []},
        {"model": "gemini-3.5-flash", "summary": "Fleet is mostly idle.", "by_resource": {}},
    )
    assert "Fleet is mostly idle." in report
    assert "gemini-3.5-flash" in report


def test_analysis_store_round_trip():
    clear_analysis()
    assert last_analysis()["by_resource"] == {}
    store_analysis({"by_resource": {"a": {}}, "summary": "s", "model": "m"})
    assert last_analysis()["model"] == "m"
    assert last_analysis()["at"] is not None
    clear_analysis()
    assert last_analysis()["model"] is None


def test_the_ticket_shows_the_models_recommendation_not_a_label():
    """Regression: render_approval rebuilt the text from a catalogue key and
    discarded the model's analysis, so every ticket read identically."""
    from app.tools.memory_tools import render_approval

    ticket = {
        "resource_id": "svc",
        "action_key": "act.right_size",
        "model_recommendation": "Set min_instances to 0; memory stays at 512Mi.",
        "change_specs": [{"kind": "memory", "from": "2Gi", "to": "512Mi"}],
    }
    for lang in ("en", "es"):
        assert render_approval(ticket, lang)["proposed_action"] == (
            "Set min_instances to 0; memory stays at 512Mi."
        )


def test_tickets_without_model_text_still_use_the_catalogue():
    from app.tools.memory_tools import render_approval

    ticket = {
        "resource_id": "svc", "action_key": "act.delete_disk",
        "model_recommendation": "", "change_specs": [],
    }
    assert render_approval(ticket, "es")["proposed_action"] == "Eliminar disco persistente huérfano"
