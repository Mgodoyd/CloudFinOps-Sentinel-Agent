"""Gemma is the second tier, and it is given a narrower job than Gemini.

Degradation has three steps, not two:

    Gemini  → per-resource judgement + fleet summary
    Gemma   → deterministic findings + a real fleet summary
    neither → deterministic findings, no narrative

The narrowness is measured, not stylistic. Asked for the analyst's full
per-resource schema, Gemma does not answer inside a usable deadline — a single
resource exceeded 100 s, and the whole fleet returned 504. The same fleet
summarised in one paragraph comes back in roughly twenty seconds. So the second
tier buys back the narrative the report would otherwise lose entirely, and
leaves the per-resource judgement to the rules that were always going to run.
"""

from typing import Any, Dict, List, Optional

import pytest

from app.core import analyst as analyst_module
from app.core.agent import CloudFinOpsAgent
from app.core.analyst import last_analysis, summarise_fleet
from app.core.config import settings
from app.tools.gcp_metrics import describe_resources

SUMMARY = "Spending is concentrated in ml-inference and checkout-api."


class StubModels:
    def __init__(self, text: Optional[str], error: Exception = None):
        self.text = text
        self.error = error
        self.calls: List[Dict[str, Any]] = []

    def generate_content(self, model=None, contents=None, config=None):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.error:
            raise self.error
        return type("Response", (), {"text": self.text})()


class StubClient:
    def __init__(self, text: Optional[str] = SUMMARY, error: Exception = None):
        self.models = StubModels(text, error)


@pytest.fixture
def fleet():
    return describe_resources()[0]


@pytest.fixture
def gemini_is_down(monkeypatch):
    """The primary tier returns nothing — quota, capacity or a timeout."""
    monkeypatch.setattr(analyst_module.FleetAnalyst, "analyse", lambda self, resources: None)


# --- 1. the summary call itself -------------------------------------------
def test_it_asks_the_configured_model_for_one_paragraph(fleet):
    client = StubClient()
    assert summarise_fleet(client, "gemma-4-31b-it", fleet) == SUMMARY

    call = client.models.calls[0]
    assert call["model"] == "gemma-4-31b-it"
    assert "60 words" in call["config"].system_instruction, (
        "the length bound is the whole reason this call fits in a deadline"
    )


def test_it_sends_the_measured_facts_not_a_conclusion(fleet):
    client = StubClient()
    summarise_fleet(client, "gemma-4-31b-it", fleet)

    sent = client.models.calls[0]["contents"]
    assert "observed_cpu_peak_pct" in sent
    assert "detected_state" in sent


def test_no_client_and_no_resources_are_both_a_quiet_none(fleet):
    assert summarise_fleet(None, "gemma-4-31b-it", fleet) is None
    assert summarise_fleet(StubClient(), "gemma-4-31b-it", []) is None


def test_an_outage_returns_none_rather_than_raising(fleet):
    client = StubClient(error=RuntimeError("504 DEADLINE_EXCEEDED"))
    assert summarise_fleet(client, "gemma-4-31b-it", fleet) is None


def test_an_empty_answer_is_treated_as_no_answer(fleet):
    assert summarise_fleet(StubClient(text="   "), "gemma-4-31b-it", fleet) is None


# --- 2. the tier inside the agent -----------------------------------------
def test_gemma_is_asked_only_when_gemini_returned_nothing(monkeypatch, fleet):
    agent = CloudFinOpsAgent()
    agent.client = StubClient()
    assert agent._summarise_with_gemma(fleet) == SUMMARY


def test_the_tier_is_off_when_no_model_is_configured(monkeypatch, fleet):
    monkeypatch.setattr(settings, "GEMMA_MODEL", "")
    agent = CloudFinOpsAgent()
    agent.client = StubClient()

    assert agent._summarise_with_gemma(fleet) is None
    assert not agent.client.models.calls, "an empty model name must not call anything"


def test_both_tiers_down_degrades_without_raising(fleet):
    agent = CloudFinOpsAgent()
    agent.client = StubClient(error=RuntimeError("503"))
    assert agent._summarise_with_gemma(fleet) is None


# --- 3. the header must not claim Gemini wrote it -------------------------
def test_the_stored_analysis_names_gemma(monkeypatch, fleet):
    """The ENGINE pill reads this. Claiming Gemini while Gemma wrote the
    summary is the kind of small lie that makes an operator stop trusting the
    rest of the panel."""
    from app.core.analyst import clear_analysis, store_analysis

    clear_analysis()
    store_analysis({"by_resource": {}, "summary": SUMMARY, "model": settings.GEMMA_MODEL})

    stored = last_analysis()
    assert stored["model"] == settings.GEMMA_MODEL
    assert stored["summary"] == SUMMARY


def test_a_gemma_summary_carries_no_per_resource_verdicts(monkeypatch, fleet):
    """Empty `by_resource` is the signal that keeps the run off the planning
    path — planning from Gemma would spend a second timeout discovering there
    is nothing to plan from."""
    from app.core.analyst import clear_analysis, store_analysis

    clear_analysis()
    store_analysis({"by_resource": {}, "summary": SUMMARY, "model": settings.GEMMA_MODEL})
    assert last_analysis()["by_resource"] == {}
