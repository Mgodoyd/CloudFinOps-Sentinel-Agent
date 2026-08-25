"""OpenTelemetry wiring.

The agent's own trace (`app/core/trace.py`) is what an operator reads: a
reasoning chain in plain language. This is the machine-readable half — the same
run expressed as OTel spans, exported to Cloud Trace, so an audit can be
correlated with everything else running in the project.

Both views describe one run. Emitting only the friendly one makes the agent
un-auditable by anything but a human; emitting only spans makes it unreadable
by the human who has to approve a disk deletion.
"""

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

SERVICE_NAME = "cloudfinops-sentinel"

_tracer = None
_enabled = False


def init_telemetry(app: Any = None) -> bool:
    """Set up the exporter once, at startup. Never fatal."""
    global _tracer, _enabled

    if _enabled or not settings.OTEL_ENABLED:
        return _enabled

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({
            "service.name": SERVICE_NAME,
            "service.version": settings.VERSION,
            "cloud.provider": "gcp",
            "cloud.account.id": settings.PROJECT_ID,
            "cloud.region": settings.REGION,
            "deployment.environment": "cloud-run" if os.environ.get("K_SERVICE") else "local",
        })
        provider = TracerProvider(resource=resource)

        exporter = _build_exporter()
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))

        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer(SERVICE_NAME)
        _enabled = True

        if app is not None:
            try:
                from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

                # Health checks would otherwise dominate the trace volume.
                FastAPIInstrumentor.instrument_app(app, excluded_urls="health,api/auth")
            except Exception as exc:
                logger.debug("FastAPI instrumentation unavailable: %s", exc)

        logger.info("OpenTelemetry active (exporter=%s)",
                    type(exporter).__name__ if exporter else "none")
        return True

    except Exception as exc:
        # Observability must never be the reason the agent will not start.
        logger.warning("OpenTelemetry unavailable (%s); continuing without spans", exc)
        return False


class _QuietExporter:
    """Wraps an exporter so a missing permission is reported once, not per span.

    Without this a service account lacking `cloudtrace.agent` floods the log
    with an identical error for every span, drowning the agent's own output.
    """

    def __init__(self, inner: Any):
        self._inner = inner
        self._failed = False

    def export(self, spans):
        if self._failed:
            return self._success()
        try:
            return self._inner.export(spans)
        except Exception as exc:
            self._failed = True
            logger.warning(
                "Cloud Trace export disabled after: %s: %s. "
                "Grant roles/cloudtrace.agent to export spans; the agent runs regardless.",
                type(exc).__name__, str(exc)[:160],
            )
            return self._success()

    @staticmethod
    def _success():
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self):
        try:
            self._inner.shutdown()
        except Exception:
            pass

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception:
            return True


def _build_exporter():
    """Cloud Trace when credentials allow it, OTLP if configured, else none."""
    if settings.OTEL_EXPORTER_OTLP_ENDPOINT:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        return _QuietExporter(OTLPSpanExporter(endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT))
    try:
        from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter

        return _QuietExporter(CloudTraceSpanExporter(project_id=settings.PROJECT_ID))
    except Exception as exc:
        logger.info("Cloud Trace exporter unavailable (%s); spans stay local", exc)
        return None


def is_enabled() -> bool:
    return _enabled


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a span. A no-op when telemetry is off, so callers need no guard."""
    if not _enabled or _tracer is None:
        yield None
        return

    with _tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, _coerce(value))
        try:
            yield current
        except Exception as exc:
            from opentelemetry.trace import Status, StatusCode

            current.set_status(Status(StatusCode.ERROR, str(exc)[:200]))
            current.record_exception(exc)
            raise


def annotate(**attributes: Any) -> None:
    """Add attributes to whichever span is currently open."""
    if not _enabled:
        return
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        if current is None:
            return
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, _coerce(value))
    except Exception:
        pass


def event(name: str, **attributes: Any) -> None:
    """Record a point-in-time event on the current span."""
    if not _enabled:
        return
    try:
        from opentelemetry import trace

        current = trace.get_current_span()
        if current is not None:
            current.add_event(name, {k: _coerce(v) for k, v in attributes.items()
                                     if v is not None})
    except Exception:
        pass


def trace_context() -> Dict[str, Optional[str]]:
    """The current trace/span ids, for correlating logs with spans."""
    if not _enabled:
        return {"trace_id": None, "span_id": None}
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if not ctx.is_valid:
            return {"trace_id": None, "span_id": None}
        return {"trace_id": format(ctx.trace_id, "032x"),
                "span_id": format(ctx.span_id, "016x")}
    except Exception:
        return {"trace_id": None, "span_id": None}


def _coerce(value: Any) -> Any:
    """OTel attributes must be primitives or homogeneous sequences."""
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return str(value)[:1000]
