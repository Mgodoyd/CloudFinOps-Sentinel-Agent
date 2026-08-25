"""Execution trace: what the agent did, in order, with the real payloads.

The dashboard needs to answer "what actually happened?" — which API was called,
what came back, which rule fired, what was sent to GCP when a human approved,
and what GCP replied. Every step is recorded here and streamed live to any
connected client.

A step is deliberately structured rather than a log line: `detail` carries the
request and response bodies so an operator can verify an action really executed
instead of trusting a message that says it did.
"""

import asyncio
import itertools
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

MAX_STEPS = 400

# Phases, in the order they occur during a run.
DISCOVERY = "DISCOVERY"
METRICS = "METRICS"
ANALYSIS = "ANALYSIS"
PLANNING = "PLANNING"
DECISION = "DECISION"
EXECUTION = "EXECUTION"
APPROVAL = "APPROVAL"
SYSTEM = "SYSTEM"

OK = "ok"
INFO = "info"
WARN = "warn"
ERROR = "error"


class Tracer:
    """Ring buffer of steps plus live fan-out to SSE subscribers."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._steps: List[Dict[str, Any]] = []
        self._counter = itertools.count(1)
        self._subscribers: Set[asyncio.Queue] = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._run_id: Optional[str] = None

    # ------------------------------------------------------------------
    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop so worker threads can publish into it."""
        self._loop = loop

    def set_run(self, run_id: Optional[str]) -> None:
        with self._lock:
            self._run_id = run_id

    # ------------------------------------------------------------------
    def notify_state_changed(self, reason: str = "") -> None:
        """Tell every connected client the dashboard state is stale.

        Approvals and executions change what is on screen; waiting for the next
        poll makes a click feel unacknowledged.
        """
        with self._lock:
            subscribers = list(self._subscribers)
        self._publish({"kind": "state", "reason": reason}, subscribers)

    def step(
        self,
        phase: str,
        message: str,
        status: str = INFO,
        detail: Optional[Dict[str, Any]] = None,
        duration_ms: Optional[float] = None,
        resource_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record one step and push it to every live subscriber."""
        step = {
            "kind": "step",
            "seq": next(self._counter),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "phase": phase,
            "status": status,
            "message": message,
            "detail": detail or None,
            "duration_ms": round(duration_ms, 1) if duration_ms is not None else None,
            "resource_id": resource_id,
            "run_id": self._run_id,
        }

        with self._lock:
            self._steps.append(step)
            del self._steps[:-MAX_STEPS]
            subscribers = list(self._subscribers)

        logger.info("[%s] %s", phase, message)

        # Mirror onto the current OTel span: the same run, machine-readable.
        try:
            from app.core import telemetry

            telemetry.event(
                f"{phase.lower()}.{status}",
                message=message,
                resource_id=resource_id,
                duration_ms=duration_ms,
            )
            step["trace_id"] = telemetry.trace_context()["trace_id"]
        except Exception:
            pass  # observability must never break the run

        self._publish(step, subscribers)
        return step

    def _publish(self, step: Dict[str, Any], subscribers: List[asyncio.Queue]) -> None:
        if not subscribers or self._loop is None:
            return
        for queue in subscribers:
            try:
                self._loop.call_soon_threadsafe(queue.put_nowait, step)
            except RuntimeError:
                pass  # loop closed during shutdown

    # ------------------------------------------------------------------
    def timed(self, phase: str, message: str, **fields: Any) -> "_Timer":
        """Context manager that records how long an operation took.

        Used around every GCP call so the trace shows real latency, and so a
        failure is recorded with its exception instead of vanishing.
        """
        return _Timer(self, phase, message, fields)

    # ------------------------------------------------------------------
    def steps(self, since: int = 0, limit: int = MAX_STEPS) -> List[Dict[str, Any]]:
        with self._lock:
            return [s for s in self._steps if s["seq"] > since][-limit:]

    def clear(self) -> None:
        with self._lock:
            self._steps.clear()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


class _Timer:
    def __init__(self, tracer: Tracer, phase: str, message: str, fields: Dict[str, Any]):
        self.tracer = tracer
        self.phase = phase
        self.message = message
        self.fields = fields
        self.detail: Dict[str, Any] = dict(fields.pop("detail", {}) or {})
        self.start = 0.0

    def __enter__(self) -> "_Timer":
        self.start = time.perf_counter()
        return self

    def add(self, **detail: Any) -> "_Timer":
        """Attach request/response data discovered while the block ran."""
        self.detail.update(detail)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed = (time.perf_counter() - self.start) * 1000
        if exc is None:
            self.tracer.step(
                self.phase, self.message, status=self.fields.pop("status", OK),
                detail=self.detail or None, duration_ms=elapsed, **self.fields,
            )
        else:
            self.detail["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.tracer.step(
                self.phase, f"{self.message} — failed", status=ERROR,
                detail=self.detail, duration_ms=elapsed, **self.fields,
            )
        return False  # never swallow the exception


tracer = Tracer()
