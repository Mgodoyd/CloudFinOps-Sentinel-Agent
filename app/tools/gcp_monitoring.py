"""Real utilization metrics from Cloud Monitoring.

Cloud Run publishes CPU and memory utilization as DISTRIBUTION metrics. We
align each series to its 99th percentile over the lookback window and reduce
across revisions, which is the number that matters for right-sizing: the peak a
service actually reached, not its average.

Every function degrades to ``None`` when the API is unavailable so the caller
can fall back to the modelled dataset.
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings

logger = logging.getLogger(__name__)

CPU_METRIC = "run.googleapis.com/container/cpu/utilizations"
MEM_METRIC = "run.googleapis.com/container/memory/utilizations"
REQUEST_METRIC = "run.googleapis.com/request_count"
INSTANCE_METRIC = "run.googleapis.com/container/instance_count"

# Set once we learn the API is unreachable, so we stop retrying on every poll.
_unavailable_until: float = 0.0
_UNAVAILABLE_BACKOFF = 300  # seconds


def _mark_unavailable(reason: str) -> None:
    global _unavailable_until
    _unavailable_until = time.monotonic() + _UNAVAILABLE_BACKOFF
    logger.warning("Cloud Monitoring unavailable (%s); backing off %ss", reason, _UNAVAILABLE_BACKOFF)


def is_available() -> bool:
    return time.monotonic() >= _unavailable_until


def _interval(hours: int):
    from google.cloud import monitoring_v3

    now = time.time()
    return monitoring_v3.TimeInterval(
        end_time={"seconds": int(now)},
        start_time={"seconds": int(now - hours * 3600)},
    )


def _point_value(point) -> Optional[float]:
    """Extract a scalar from a typed Monitoring point."""
    v = point.value
    if v.double_value:
        return float(v.double_value)
    if v.int64_value:
        return float(v.int64_value)
    # An aligned distribution still arrives as a double; a raw one does not.
    if v.distribution_value and v.distribution_value.count:
        return float(v.distribution_value.mean)
    return None


def _query(metric_type: str, aligner: str, reducer: str) -> Optional[Dict[str, float]]:
    """Return {service_name: value} for one metric, or None if unavailable."""
    if not is_available():
        return None

    try:
        from google.cloud import monitoring_v3

        client = monitoring_v3.MetricServiceClient()
        aggregation = monitoring_v3.Aggregation(
            alignment_period={"seconds": 3600},
            per_series_aligner=getattr(monitoring_v3.Aggregation.Aligner, aligner),
            cross_series_reducer=getattr(monitoring_v3.Aggregation.Reducer, reducer),
            group_by_fields=["resource.labels.service_name"],
        )

        results: Dict[str, float] = {}
        pager = client.list_time_series(
            request=monitoring_v3.ListTimeSeriesRequest(
                name=f"projects/{settings.PROJECT_ID}",
                filter=f'metric.type = "{metric_type}"',
                interval=_interval(settings.METRICS_LOOKBACK_HOURS),
                view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                aggregation=aggregation,
            )
        )

        for series in pager:
            service = series.resource.labels.get("service_name")
            if not service:
                continue
            values = [v for v in (_point_value(p) for p in series.points) if v is not None]
            if values:
                results[service] = max(values)
        return results

    except Exception as exc:
        _mark_unavailable(f"{type(exc).__name__}: {str(exc)[:120]}")
        return None


def fetch_fleet_utilization() -> Optional[Dict[str, Dict[str, float]]]:
    """Peak CPU and memory utilization (0-1) per Cloud Run service.

    One batched call per metric for the whole project, not one per service.
    """
    cpu = _query(CPU_METRIC, "ALIGN_PERCENTILE_99", "REDUCE_MAX")
    if cpu is None:
        return None
    mem = _query(MEM_METRIC, "ALIGN_PERCENTILE_99", "REDUCE_MAX") or {}

    services = set(cpu) | set(mem)
    if not services:
        return {}

    return {
        name: {
            "cpu": min(1.0, max(0.0, cpu.get(name, 0.0))),
            "memory": min(1.0, max(0.0, mem.get(name, 0.0))),
        }
        for name in services
    }


def fetch_request_counts() -> Optional[Dict[str, float]]:
    """Total requests per service over the lookback window.

    A service with zero requests is genuinely idle, which is stronger evidence
    than low CPU alone.
    """
    return _query(REQUEST_METRIC, "ALIGN_SUM", "REDUCE_SUM")


_history_lock = threading.Lock()
_history_cache: Dict[int, Any] = {}


def fetch_utilization_history(hours: int = 4) -> Optional[List[Dict[str, float]]]:
    """Fleet-wide CPU/memory utilization over time, for the trend chart.

    Cached per lookback window: this is called once per chart *and* once per
    resource for the confidence assessment, but it is the same query each time.
    """
    with _history_lock:
        entry = _history_cache.get(hours)
        if entry and time.monotonic() < entry["expires"]:
            return entry["value"]

    value = _fetch_utilization_history_uncached(hours)
    with _history_lock:
        _history_cache[hours] = {
            "value": value,
            "expires": time.monotonic() + settings.METRICS_CACHE_TTL,
        }
    return value


def _fetch_utilization_history_uncached(hours: int) -> Optional[List[Dict[str, float]]]:
    if not is_available():
        return None

    try:
        from google.cloud import monitoring_v3

        client = monitoring_v3.MetricServiceClient()
        buckets: Dict[int, Dict[str, List[float]]] = {}

        ALIGNMENT = 600  # seconds per bucket

        for metric, key in ((CPU_METRIC, "cpu"), (MEM_METRIC, "memory")):
            aggregation = monitoring_v3.Aggregation(
                alignment_period={"seconds": ALIGNMENT},
                per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_PERCENTILE_99,
                cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_MEAN,
            )
            pager = client.list_time_series(
                request=monitoring_v3.ListTimeSeriesRequest(
                    name=f"projects/{settings.PROJECT_ID}",
                    filter=f'metric.type = "{metric}"',
                    interval=_interval(hours),
                    view=monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
                    aggregation=aggregation,
                )
            )
            for series in pager:
                for point in series.points:
                    value = _point_value(point)
                    if value is None:
                        continue
                    # Quantize to the alignment period: CPU at 18:50:00 and
                    # memory at 18:50:37 belong in the same bucket, otherwise
                    # they render as two points with an identical label.
                    epoch = int(point.interval.end_time.timestamp())
                    slot = (epoch // ALIGNMENT) * ALIGNMENT
                    buckets.setdefault(slot, {"cpu": [], "memory": []})[key].append(value)

        if not buckets:
            return []

        from datetime import datetime, timezone

        history = []
        for slot in sorted(buckets):
            b = buckets[slot]
            history.append(
                {
                    "t": datetime.fromtimestamp(slot, timezone.utc).strftime("%H:%M"),
                    "cpu": round(100 * sum(b["cpu"]) / len(b["cpu"]), 1) if b["cpu"] else 0.0,
                    "memory": round(100 * sum(b["memory"]) / len(b["memory"]), 1) if b["memory"] else 0.0,
                }
            )
        return history[-24:]

    except Exception as exc:
        _mark_unavailable(f"{type(exc).__name__}: {str(exc)[:120]}")
        return None
