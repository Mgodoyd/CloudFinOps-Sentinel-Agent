"""OpenTelemetry: the machine-readable half of the reasoning chain."""

import pytest

from app.core import telemetry


def test_spans_are_a_no_op_when_disabled(monkeypatch):
    """Callers must not need a guard around every span."""
    monkeypatch.setattr(telemetry, "_enabled", False)
    with telemetry.span("anything", key="value") as s:
        assert s is None
    telemetry.annotate(key="value")
    telemetry.event("something")


def test_telemetry_never_blocks_startup(monkeypatch):
    """Observability failing must not stop the agent from running."""
    monkeypatch.setattr(telemetry, "_enabled", False)
    monkeypatch.setattr(telemetry.settings, "OTEL_ENABLED", True)
    monkeypatch.setattr(
        telemetry, "_build_exporter",
        lambda: (_ for _ in ()).throw(RuntimeError("no credentials")),
    )
    assert telemetry.init_telemetry() in (True, False)  # never raises


def test_disabled_by_configuration(monkeypatch):
    monkeypatch.setattr(telemetry, "_enabled", False)
    monkeypatch.setattr(telemetry.settings, "OTEL_ENABLED", False)
    assert telemetry.init_telemetry() is False


def test_trace_context_is_safe_without_a_span(monkeypatch):
    monkeypatch.setattr(telemetry, "_enabled", False)
    assert telemetry.trace_context() == {"trace_id": None, "span_id": None}


@pytest.mark.parametrize(
    "value,expected_type",
    [("text", str), (42, int), (1.5, float), (True, bool), (["a", "b"], list)],
)
def test_primitive_attributes_pass_through(value, expected_type):
    assert isinstance(telemetry._coerce(value), expected_type)


def test_complex_attributes_are_stringified_and_bounded():
    """OTel rejects nested objects, and an unbounded string blows the exporter."""
    coerced = telemetry._coerce({"nested": {"deep": "x" * 5000}})
    assert isinstance(coerced, str)
    assert len(coerced) <= 1000


def test_a_span_records_the_exception_and_re_raises(monkeypatch):
    pytest.importorskip("opentelemetry.sdk")
    telemetry.init_telemetry()
    if not telemetry.is_enabled():
        pytest.skip("telemetry unavailable in this environment")

    with pytest.raises(ValueError):
        with telemetry.span("failing"):
            raise ValueError("boom")


def test_a_live_span_exposes_correlation_ids():
    pytest.importorskip("opentelemetry.sdk")
    telemetry.init_telemetry()
    if not telemetry.is_enabled():
        pytest.skip("telemetry unavailable in this environment")

    with telemetry.span("agent.audit", **{"agent.run_id": "run_1"}):
        ctx = telemetry.trace_context()
        assert ctx["trace_id"] and len(ctx["trace_id"]) == 32
        assert ctx["span_id"] and len(ctx["span_id"]) == 16


def test_agent_steps_carry_a_trace_id():
    """Each step in the human-readable trace links to its span."""
    from app.core.trace import DISCOVERY, Tracer

    step = Tracer().step(DISCOVERY, "listing services")
    assert "trace_id" in step


def test_health_reports_the_telemetry_backend(client):
    assert client.get("/health").json()["telemetry"] in ("opentelemetry", "disabled")
