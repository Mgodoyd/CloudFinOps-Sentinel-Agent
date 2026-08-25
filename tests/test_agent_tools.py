"""Guards on the tool list handed to Gemini.

google-genai deep-copies the GenerateContentConfig (and therefore the tools)
before every request. Anything in that list that drags a non-picklable object
along — a bound method whose instance holds a lock, for example — crashes the
whole audit with "cannot pickle '_thread.RLock' object".
"""

from copy import deepcopy

import pytest

from app.core.agent import CloudFinOpsAgent
from app.tools.memory_tools import check_remediation_history, memory_bank


def test_tool_list_survives_the_sdk_deep_copy():
    """Regression: the SDK deep-copies tools, so none may hold a lock."""
    agent = CloudFinOpsAgent()
    deepcopy(agent.tools)  # must not raise


def test_tools_are_plain_functions_not_bound_methods():
    """Bound methods deep-copy their instance; plain functions don't."""
    agent = CloudFinOpsAgent()
    for tool in agent.tools:
        assert not hasattr(tool, "__self__"), (
            f"{tool.__name__} is a bound method; deep-copying it would drag in "
            f"{type(getattr(tool, '__self__', None)).__name__} and its lock."
        )


def test_bound_method_would_still_fail():
    """Documents *why* the indirection exists, so nobody 'simplifies' it back."""
    with pytest.raises(TypeError, match="cannot pickle"):
        deepcopy(memory_bank.check_history)


def test_module_level_tool_delegates_to_the_memory_bank():
    assert check_remediation_history("svc-none")["found"] is False
    memory_bank.log_remediation("e_1", "resize", 10.0, resource_id="svc-known")
    result = check_remediation_history("svc-known")
    assert result["found"] is True
    assert result["last_action"] == "resize"


def test_every_tool_has_a_docstring():
    """Docstrings become the function declarations Gemini reads."""
    agent = CloudFinOpsAgent()
    for tool in agent.tools:
        assert tool.__doc__ and len(tool.__doc__.strip()) > 40, (
            f"{tool.__name__} needs a docstring — it is the tool's prompt."
        )


def test_the_exact_sdk_config_copy_that_crashed():
    """Reproduces google/genai/models.py:6540 without a network call.

    generate_content() calls `parsed_config.model_copy(deep=True)` on every
    request. That line raised "cannot pickle '_thread.RLock' object" and took
    down every audit.
    """
    types = pytest.importorskip("google.genai.types")

    from app.core.agent import MAX_TOOL_CALLS
    from app.core.config import settings
    from app.core.prompts import SYSTEM_INSTRUCTION

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=settings.GEMINI_TEMPERATURE,
        tools=CloudFinOpsAgent().tools,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            maximum_remote_calls=MAX_TOOL_CALLS
        ),
    )
    copied = config.model_copy(deep=True)
    assert len(copied.tools) == 5


# --- Failure handling ----------------------------------------------------
def test_quota_error_degrades_to_heuristic_instead_of_failing(monkeypatch):
    """A 429 must not abandon the audit — the fleet still needs auditing."""
    from app.core import agent as agent_mod

    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", object())  # pretend Gemini is configured
    monkeypatch.setattr(
        a, "_generate",
        lambda _p: (_ for _ in ()).throw(
            agent_mod.QuotaExceeded("429 RESOURCE_EXHAUSTED ... 'retryDelay': '54s'")
        ),
    )

    result = a.audit_infrastructure()
    assert result["status"] == "success"
    assert result["mode"] == "heuristic-fallback"
    assert "quota" in result["degraded"].lower()
    assert "54s" in result["degraded"]
    assert result["actions_taken"] > 0


def test_error_result_has_the_same_shape_as_success(monkeypatch):
    """Regression: the error path omitted keys, so callers hit KeyError.

    Triggered by a failure the agent genuinely cannot absorb — both the model
    and the deterministic fallback are down.
    """
    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", object())
    monkeypatch.setattr(
        a, "_generate", lambda _p: (_ for _ in ()).throw(RuntimeError("llm down"))
    )
    monkeypatch.setattr(
        a, "_heuristic_audit", lambda _d: (_ for _ in ()).throw(RuntimeError("boom"))
    )

    failure = a.audit_infrastructure()
    assert failure["status"] == "error"
    for key in ("run_id", "mode", "model", "anomalies_found", "actions_taken",
                "degraded", "response"):
        assert key in failure, f"error result is missing '{key}'"


def test_quota_detection_covers_the_real_message():
    from app.core.agent import _is_quota_error

    real = (
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded "
        "your current quota... generate_content_free_tier_requests, limit: 5'}}"
    )
    assert _is_quota_error(Exception(real)) is True
    assert _is_quota_error(Exception("404 NOT_FOUND model missing")) is False


def test_tool_call_budget_fits_a_small_quota():
    """Each AFC round-trip is an API request; the free tier allows 5/minute."""
    from app.core.agent import MAX_TOOL_CALLS

    assert MAX_TOOL_CALLS <= 5


def test_report_is_reconstructed_when_the_model_writes_no_text(monkeypatch):
    """AFC can end on a tool call, leaving response.text empty.

    The run still did real work, so the report must describe it rather than
    show a generic placeholder.
    """
    from app.tools import gcp_remediator

    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", object())

    def act_then_return_nothing(_prompt):
        gcp_remediator.resize_cloud_run("svc-ledger", "512Mi", estimated_savings=12.0)
        gcp_remediator.request_human_approval("svc-big", "Resize to 1Gi", 88.0)
        return ""  # model ended its turn on a function_call

    monkeypatch.setattr(a, "_generate", act_then_return_nothing)

    result = a.audit_infrastructure()
    report = result["response"]

    assert result["mode"] == "gemini"
    assert "svc-ledger" in report
    assert "svc-big" in report
    assert "Awaiting approval" in report
    assert "100.00" in report  # 12.00 applied + 88.00 escalated
    assert "dry run" in report  # DRY_RUN is on in tests


def test_reconstructed_report_handles_a_no_op_run(monkeypatch):
    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", object())
    monkeypatch.setattr(a, "_generate", lambda _p: "")

    report = a.audit_infrastructure()["response"]
    assert "No new action required" in report
    assert "$0.00" in report


@pytest.mark.parametrize(
    "error,expected_phrase",
    [
        (Exception("503 UNAVAILABLE: The model is overloaded"), "overloaded"),
        (Exception("504 deadline exceeded"), "timed out"),
        (Exception("ServerError: something broke"), "failed"),
    ],
)
def test_any_llm_outage_degrades_instead_of_losing_the_audit(
    monkeypatch, error, expected_phrase
):
    """Regression: a 5xx from Gemini used to return status=error, actions=0.

    This agent runs unattended on a schedule. A transient LLM outage must not
    cost a whole audit cycle.
    """
    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", object())
    monkeypatch.setattr(a, "_generate", lambda _p: (_ for _ in ()).throw(error))

    result = a.audit_infrastructure()
    assert result["status"] == "success"
    assert result["mode"] == "heuristic-fallback"
    assert result["actions_taken"] > 0
    assert expected_phrase in result["degraded"]


def test_fallback_report_states_the_real_reason(monkeypatch):
    """It must not claim 'no API key' when a key exists but hit its quota."""
    from app.core import agent as agent_mod

    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", object())
    monkeypatch.setattr(
        a, "_generate",
        lambda _p: (_ for _ in ()).throw(agent_mod.QuotaExceeded("429 RESOURCE_EXHAUSTED")),
    )
    report = a.audit_infrastructure()["response"]
    assert "quota exhausted" in report
    assert "no Gemini API key" not in report


def test_no_credentials_report_says_so():
    a = CloudFinOpsAgent()
    a.client = None
    report = a.audit_infrastructure()["response"]
    assert "no Gemini credentials configured" in report


# --- Model availability ---------------------------------------------------
def test_default_model_is_not_a_retired_one():
    """gemini-2.5-flash 404s for API keys created after Google's cutoff."""
    from app.core.agent import MODEL_FALLBACKS
    from app.core.config import settings

    retired = {"gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"}
    assert settings.GEMINI_MODEL not in retired
    assert not retired & set(MODEL_FALLBACKS)


def test_a_404_walks_the_fallback_chain(monkeypatch):
    from app.core import agent as agent_mod

    tried = []

    class FakeModels:
        def generate_content(self, model, contents, config):
            tried.append(model)
            if model != "gemini-3.6-flash":
                raise RuntimeError("404 NOT_FOUND: model is no longer available")
            return type("R", (), {"text": "done"})()

    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", type("C", (), {"models": FakeModels()})())
    monkeypatch.setattr(a, "model_name", "gemini-3.5-flash")

    assert a._generate("audit this") == "done"
    assert tried[0] == "gemini-3.5-flash", "the configured model is tried first"
    assert a.model_name == "gemini-3.6-flash", "the working model is adopted"


def test_exhausting_every_model_names_them_all(monkeypatch):
    a = CloudFinOpsAgent()

    class AlwaysMissing:
        def generate_content(self, **kwargs):
            raise RuntimeError("404 NOT_FOUND")

    monkeypatch.setattr(a, "client", type("C", (), {"models": AlwaysMissing()})())
    with pytest.raises(RuntimeError, match="available to this API key"):
        a._generate("audit")


def test_preflight_calls_a_retired_model_a_model_problem():
    """Regression: a 404 told the operator to check a perfectly good API key."""
    from app.tools.preflight import _gemini_failure

    result = _gemini_failure(
        Exception("404 NOT_FOUND: This model models/gemini-2.5-flash is no longer available")
    )
    assert result["status"] == "fail"
    assert "not available to this API key" in result["detail"]
    assert "key itself is valid" in result["detail"]
    assert "aistudio" not in result["fix"], "must not blame the credential"


def test_an_overloaded_model_falls_through_to_a_sibling(monkeypatch):
    """Capacity is per-model: a 503 on one is no reason to give up on all."""
    tried = []

    class Models:
        def generate_content(self, model, contents, config):
            tried.append(model)
            if model == "gemini-3.5-flash":
                raise RuntimeError(
                    "503 UNAVAILABLE. This model is currently experiencing high demand."
                )
            return type("R", (), {"text": "audited"})()

    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", type("C", (), {"models": Models()})())
    monkeypatch.setattr(a, "model_name", "gemini-3.5-flash")

    assert a._generate("audit") == "audited"
    assert len(tried) == 2, "it must try the next model, not give up"


def test_quota_does_not_walk_the_chain(monkeypatch):
    """Quota is shared across models, so retrying another one just burns it."""
    from app.core import agent as agent_mod

    tried = []

    class Models:
        def generate_content(self, model, contents, config):
            tried.append(model)
            raise RuntimeError("429 RESOURCE_EXHAUSTED quota exceeded")

    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", type("C", (), {"models": Models()})())
    with pytest.raises(agent_mod.QuotaExceeded):
        a._generate("audit")
    assert len(tried) == 1


def test_overload_walk_is_bounded_by_the_request_budget(monkeypatch):
    """Each attempt is a billed request; the free tier allows 5 per minute.
    Walking every candidate on a congested minute would spend the lot."""
    from app.core.agent import MAX_OVERLOAD_ATTEMPTS

    tried = []

    class AlwaysBusy:
        def generate_content(self, model, contents, config):
            tried.append(model)
            raise RuntimeError("503 UNAVAILABLE. high demand.")

    a = CloudFinOpsAgent()
    monkeypatch.setattr(a, "client", type("C", (), {"models": AlwaysBusy()})())
    with pytest.raises(RuntimeError):
        a._generate("audit")

    assert len(tried) == MAX_OVERLOAD_ATTEMPTS == 2


def test_preflight_rejects_a_live_only_model(monkeypatch):
    """A Live model exists but speaks bidiGenerateContent over a WebSocket.
    Configuring one must not read as a broken API key."""
    from app.core.config import settings
    from app.tools import preflight

    class FakeModels:
        def list(self):
            return [
                type("M", (), {"name": "models/gemini-3.1-flash-live-preview",
                               "supported_actions": ["bidiGenerateContent"]})(),
            ]

    monkeypatch.setattr(settings, "GEMINI_MODEL", "gemini-3.1-flash-live-preview")
    monkeypatch.setattr(
        "google.genai.Client", lambda **kw: type("C", (), {"models": FakeModels()})()
    )

    result = preflight._uses_a_different_api("gemini-3.1-flash-live-preview")
    assert result is not None
    assert result["status"] == "fail"
    assert "generateContent" in result["detail"]
    assert "flash-lite" in result["fix"]


def test_a_generate_content_model_passes_the_guard(monkeypatch):
    from app.tools import preflight

    class FakeModels:
        def list(self):
            return [type("M", (), {"name": "models/gemini-3.5-flash-lite",
                                   "supported_actions": ["generateContent"]})()]

    monkeypatch.setattr(
        "google.genai.Client", lambda **kw: type("C", (), {"models": FakeModels()})()
    )
    assert preflight._uses_a_different_api("gemini-3.5-flash-lite") is None


def test_fallbacks_prefer_quota_headroom():
    """flash-lite has the highest free-tier limit; try it before heavier models."""
    from app.core.agent import MODEL_FALLBACKS

    assert "lite" in MODEL_FALLBACKS[0]
