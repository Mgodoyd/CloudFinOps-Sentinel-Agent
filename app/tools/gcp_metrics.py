"""Infrastructure observation tools.

Reads Cloud Run services and their utilization. Every GCP call is wrapped in a
short-lived cache (the dashboard polls frequently) and degrades to a clearly
labelled simulated dataset when credentials or APIs are unavailable, so the
demo never hard-fails.
"""

import hashlib
import logging
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.tools import gcp_monitoring, rationale
from app.core.trace import ANALYSIS, INFO, OK, WARN, tracer

logger = logging.getLogger(__name__)

# Cloud Run Tier-1 on-demand pricing (approximate, USD).
CPU_PRICE_PER_VCPU_SECOND = 0.00002400
MEM_PRICE_PER_GIB_SECOND = 0.00000250
SECONDS_PER_MONTH = 730 * 3600

# A demo fleet that mirrors a realistic mix: some always-on (billed around the
# clock), some scale-to-zero (billed only while serving).
SIMULATED_SERVICES: List[Dict[str, Any]] = [
    {"resource_id": "checkout-api", "cpu_limit": "2", "memory_limit": "4Gi", "min_instances": 2},
    {"resource_id": "ml-inference", "cpu_limit": "4", "memory_limit": "8Gi", "min_instances": 1},
    {"resource_id": "billing-worker", "cpu_limit": "1", "memory_limit": "2Gi", "min_instances": 1},
    {"resource_id": "auth-gateway", "cpu_limit": "1", "memory_limit": "512Mi", "min_instances": 1},
    {"resource_id": "notifications", "cpu_limit": "500m", "memory_limit": "256Mi", "min_instances": 0},
    {"resource_id": "media-transcoder", "cpu_limit": "2", "memory_limit": "1Gi", "min_instances": 0},
]


class _TTLCache:
    """Thread-safe single-value cache with a separate "last known" slot.

    The TTL decides when data is stale enough to re-fetch. It must not decide
    whether we have data at all: once a scan has run, the dashboard keeps
    showing that result (with its age) instead of reverting to "never scanned".
    """

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._value: Any = None
        self._expires_at: float = 0.0
        self._last: Any = None
        self._last_at: float = 0.0

    def get(self) -> Any:
        """The value if still fresh, else None (callers may re-fetch)."""
        with self._lock:
            if self._value is not None and time.monotonic() < self._expires_at:
                return self._value
        return None

    def last(self) -> Any:
        """The most recent value ever stored, however old."""
        with self._lock:
            return self._last

    def last_age_seconds(self) -> Optional[float]:
        with self._lock:
            if self._last is None:
                return None
            return time.monotonic() - self._last_at

    def set(self, value: Any) -> Any:
        with self._lock:
            self._value = value
            self._expires_at = time.monotonic() + self.ttl
            self._last = value
            self._last_at = time.monotonic()
        return value

    def clear(self) -> None:
        """Expire the freshness window but keep the last known result."""
        with self._lock:
            self._value = None
            self._expires_at = 0.0

    def reset(self) -> None:
        """Forget everything — as if no scan had ever run."""
        with self._lock:
            self._value = self._last = None
            self._expires_at = self._last_at = 0.0


_services_cache = _TTLCache(settings.METRICS_CACHE_TTL)


# ----------------------------------------------------------------------
# Unit helpers
# ----------------------------------------------------------------------
def parse_cpu(cpu_limit: str) -> float:
    """'500m' -> 0.5, '2' -> 2.0."""
    try:
        value = str(cpu_limit).strip()
        if value.endswith("m"):
            return float(value[:-1]) / 1000
        return float(value)
    except (ValueError, AttributeError):
        return 1.0


def parse_memory_gib(memory_limit: str) -> float:
    """'512Mi' -> 0.5, '2Gi' -> 2.0."""
    try:
        value = str(memory_limit).strip()
        if value.endswith("Gi"):
            return float(value[:-2])
        if value.endswith("Mi"):
            return float(value[:-2]) / 1024
        if value.endswith("Ki"):
            return float(value[:-2]) / (1024 * 1024)
        return float(value) / (1024**3)
    except (ValueError, AttributeError):
        return 0.5


def calculate_monthly_cost(cpu_limit: str, memory_limit: str) -> float:
    """Estimated monthly cost of the allocation, assuming an always-on instance."""
    cpu = parse_cpu(cpu_limit)
    mem = parse_memory_gib(memory_limit)
    cost = (cpu * CPU_PRICE_PER_VCPU_SECOND + mem * MEM_PRICE_PER_GIB_SECOND) * SECONDS_PER_MONTH
    return round(cost, 2)


def _seed(resource_id: str) -> int:
    """Stable per-resource seed so simulated metrics don't jitter between polls."""
    return int(hashlib.sha256(resource_id.encode()).hexdigest()[:8], 16)


# ----------------------------------------------------------------------
# Cloud Run inventory
# ----------------------------------------------------------------------
# Problems reported by the last inventory scan, surfaced on the dashboard.
_last_problems: List[Dict[str, str]] = []


def last_problems() -> List[Dict[str, str]]:
    return list(_last_problems)


def seed_services(services: List[Dict[str, Any]]) -> None:
    """Populate the service cache from a scan someone else already did.

    `discover_all` lists Cloud Run as one of its four parallel sources. Without
    this the refresh path would list it a second time for the same data.
    """
    _services_cache.set((services, "gcp"))


def has_scanned() -> bool:
    """True once a scan has run, regardless of how long ago."""
    from app.tools import gcp_inventory

    return _services_cache.last() is not None or gcp_inventory.has_scanned()


def last_scan_age_seconds() -> Optional[float]:
    """Seconds since the last successful scan, or None if never scanned."""
    from app.tools import gcp_inventory

    ages = [a for a in (_services_cache.last_age_seconds(),
                        gcp_inventory.last_scan_age_seconds()) if a is not None]
    return min(ages) if ages else None


def fetch_services(
    force_refresh: bool = False, allow_discovery: bool = True
) -> Tuple[List[Dict[str, Any]], str]:
    """Return (services, data_source).

    data_source is 'gcp' for anything read from the project — *including an
    empty result*. A project with no Cloud Run services is a real answer, not a
    failure, so it is never replaced with invented data. Simulated data appears
    only when MOCK_MODE is on, or when a genuine API error occurs and
    ALLOW_SIMULATED_FALLBACK is explicitly enabled.
    """
    global _last_problems

    if force_refresh:
        _services_cache.clear()

    cached = _services_cache.get()
    if cached is not None:
        return cached

    if not allow_discovery:
        # The dashboard polls constantly; it must never start a scan by itself.
        # It should still see the last result, even after the TTL lapsed.
        previous = _services_cache.last()
        return previous if previous is not None else ([], "idle")

    if settings.MOCK_MODE:
        _last_problems = []
        return _services_cache.set(([dict(s) for s in SIMULATED_SERVICES], "simulated"))

    from app.tools import gcp_inventory

    services, problems = gcp_inventory.discover_cloud_run()
    _last_problems = problems

    if not problems:
        # Reached the API. Whatever it returned — even nothing — is the truth.
        logger.info(
            "Discovered %d Cloud Run service(s) across %d region(s)",
            len(services), len(settings.regions),
        )
        return _services_cache.set((services, "gcp"))

    logger.warning("Cloud Run inventory failed: %s", problems[0]["detail"])
    if settings.ALLOW_SIMULATED_FALLBACK:
        return _services_cache.set(([dict(s) for s in SIMULATED_SERVICES], "simulated"))
    return _services_cache.set(([], "error"))


# ----------------------------------------------------------------------
# Utilization
# ----------------------------------------------------------------------
def _simulated_utilization(resource_id: str, kind: str) -> float:
    """Deterministic 0-1 utilization derived from the resource name."""
    seed = _seed(resource_id + kind)
    # Oversized services intentionally land in the low band so the demo has
    # something meaningful for the agent to find.
    return round(0.04 + (seed % 780) / 1000, 3)


_utilization_cache = _TTLCache(settings.METRICS_CACHE_TTL)


def fleet_utilization(allow_discovery: bool = True) -> Tuple[Dict[str, Dict[str, float]], str]:
    """Real utilization per service, or an empty map when unavailable.

    Returns (map, source) where source is 'monitoring' or 'modelled'.
    """
    cached = _utilization_cache.get()
    if cached is not None:
        return cached

    if not allow_discovery:
        previous = _utilization_cache.last()
        return previous if previous is not None else ({}, "idle")

    if settings.MOCK_MODE or not settings.USE_REAL_METRICS:
        return _utilization_cache.set(({}, "modelled"))

    data = gcp_monitoring.fetch_fleet_utilization()
    if data:
        logger.info("Loaded real utilization for %d service(s) from Cloud Monitoring", len(data))
        return _utilization_cache.set((data, "monitoring"))
    return _utilization_cache.set(({}, "modelled"))


def get_utilization(resource_id: str) -> Dict[str, float]:
    """Observed utilization for one service.

    Prefers Cloud Monitoring; falls back to a deterministic model so the
    dashboard still renders when metrics are unavailable.
    """
    real, _ = fleet_utilization()
    if resource_id in real:
        return real[resource_id]
    return {
        "cpu": _simulated_utilization(resource_id, "cpu"),
        "memory": _simulated_utilization(resource_id, "mem"),
    }


def get_utilization_series(resource_id: str, points: int = 24, bucket_minutes: int = 10) -> List[Dict[str, Any]]:
    """A stable time series for the dashboard chart.

    Anchored to wall-clock buckets so the line advances over time instead of
    reshuffling on every poll.
    """
    base = get_utilization(resource_id)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now -= timedelta(minutes=now.minute % bucket_minutes)

    series = []
    for i in range(points - 1, -1, -1):
        ts = now - timedelta(minutes=bucket_minutes * i)
        phase = (ts.timestamp() / (bucket_minutes * 60)) + _seed(resource_id) % 97
        wave = (math.sin(phase / 3.1) + math.sin(phase / 7.7)) / 2
        cpu = max(0.01, min(1.0, base["cpu"] * (1 + 0.45 * wave)))
        mem = max(0.01, min(1.0, base["memory"] * (1 + 0.25 * wave)))
        series.append(
            {
                "t": ts.strftime("%H:%M"),
                "cpu": round(cpu * 100, 1),
                "memory": round(mem * 100, 1),
            }
        )
    return series


# ----------------------------------------------------------------------
# Enrichment
# ----------------------------------------------------------------------
def describe_resources(
    allow_discovery: bool = True, force_refresh: bool = False
) -> Tuple[List[Dict[str, Any]], str]:
    """Every service, enriched with cost and utilization."""
    if force_refresh:
        _utilization_cache.clear()
    services, source = fetch_services(
        allow_discovery=allow_discovery, force_refresh=force_refresh
    )
    if source == "idle":
        return [], "idle"
    real_util, util_source = fleet_utilization()
    enriched: List[Dict[str, Any]] = []

    for svc in services:
        rid = svc["resource_id"]
        cpu_limit = svc.get("cpu_limit", "1")
        mem_limit = svc.get("memory_limit", "512Mi")
        util = get_utilization(rid)
        # min_instances are billed around the clock whether or not traffic
        # arrives; a scale-to-zero service only bills while handling requests.
        min_instances = int(svc.get("min_instances") or 0)
        cost = calculate_monthly_cost(cpu_limit, mem_limit) * max(min_instances, 1)
        cost = round(cost if min_instances else cost * max(util["cpu"], 0.02), 2)

        # Waste = the share of the allocation that utilization never reaches.
        headroom = 1 - max(util["cpu"], util["memory"])
        wasted = round(cost * max(0.0, headroom - 0.2), 2)  # keep a 20% safety buffer

        mem_gib = parse_memory_gib(mem_limit)
        oversized = mem_gib >= settings.MEMORY_ANOMALY_THRESHOLD_GIB and headroom > 0.5
        # An always-on service with no load is the most expensive kind of idle.
        idle = (min_instances > 0 and util["cpu"] < 0.10) or (
            util["cpu"] < 0.10 and util["memory"] < 0.20
        )

        # Severity follows the money, not just the shape. An idle service
        # wasting $1/month is a note, not an incident.
        if wasted < settings.MIN_SAVINGS_THRESHOLD:
            severity = "LOW"
        elif wasted >= settings.HIGH_RISK_ROI_THRESHOLD:
            severity = "HIGH"
        else:
            severity = "MEDIUM"

        if idle:
            status = "Idle"
        elif oversized:
            status = "Oversized"
        else:
            status, severity = "Healthy", "LOW"

        # A resource whose recoverable waste is under the action threshold is
        # correctly sized *for practical purposes*. Reporting it as a problem
        # is misleading: there is nothing to do, and the operator should see
        # that it is fine, not that it is about to be changed.
        if status != "Healthy" and wasted < settings.MIN_SAVINGS_THRESHOLD:
            status, severity = "Tolerated", "LOW"

        enriched.append(
            {
                "resource_id": rid,
                "type": "Cloud Run",
                "region": svc.get("region", settings.REGION),
                "cpu_limit": cpu_limit,
                "memory_limit": mem_limit,
                "min_instances": min_instances,
                "uri": svc.get("uri", ""),
                "cpu_utilization": round(util["cpu"] * 100, 1),
                "memory_utilization": round(util["memory"] * 100, 1),
                "monthly_cost": cost,
                "wasted_cost": wasted,
                "status": status,
                "severity": severity,
                "metrics_source": "monitoring" if rid in real_util else "modelled",
                "metric": f"{cpu_limit} vCPU / {mem_limit} · ${cost:.2f}/mo",
                "url": (
                    f"https://console.cloud.google.com/run/detail/"
                    f"{svc.get('region', settings.REGION)}"
                    f"/{rid}/metrics?project={settings.PROJECT_ID}"
                ),
            }
        )

    enriched.sort(key=lambda r: r["monthly_cost"], reverse=True)
    return enriched, source


def get_active_resources() -> List[Dict[str, Any]]:
    """Healthy resources — kept for backwards compatibility with the old API."""
    resources, _ = describe_resources()
    return [r for r in resources if r["status"] in ("Healthy", "Tolerated")]


def get_infrastructure_anomalies(force_refresh: bool = False) -> Dict[str, Any]:
    """The payload handed to the LLM: everything that looks wasteful.

    `force_refresh` bypasses the cache. Pressing "Run Audit" must mean "go look
    again" — otherwise a re-scan inside the TTL just re-analyses the same
    snapshot and appears to produce identical results forever.
    """
    resources, source = describe_resources(force_refresh=force_refresh)
    anomalies = []

    tracer.step(
        ANALYSIS, f"Evaluating {len(resources)} resource(s) against detection rules",
        detail={"rules": ["IDLE_ALWAYS_ON", "IDLE_SERVICE", "OVERSIZED_ALLOCATION"],
                "min_savings_threshold": settings.MIN_SAVINGS_THRESHOLD,
                "approval_threshold": settings.HIGH_RISK_ROI_THRESHOLD},
    )
    negligible = []
    for res in resources:
        if res["status"] == "Healthy":
            continue
        # "Tolerated" is exactly the below-threshold state: worth reporting,
        # not worth anyone's time to action.
        if res["status"] == "Tolerated" or res["wasted_cost"] < settings.MIN_SAVINGS_THRESHOLD:
            # Worth knowing about, not worth anyone's time to action.
            negligible.append(
                {
                    "resource_id": res["resource_id"],
                    "status": res["status"],
                    "potential_savings": res["wasted_cost"],
                }
            )
            tracer.step(
                ANALYSIS,
                f"{res['resource_id']} below action threshold — reported, not actioned",
                resource_id=res["resource_id"],
                detail={"recoverable": res["wasted_cost"],
                        "threshold": settings.MIN_SAVINGS_THRESHOLD},
            )
            continue
        anomalies.append(
            {
                "resource_id": res["resource_id"],
                "resource_type": res["type"],
                "anomaly_type": "IDLE_SERVICE" if res["status"] == "Idle" else "OVERSIZED_ALLOCATION",
                "severity": res["severity"],
                "issue": (
                    f"{res['status']}: {res['cpu_limit']} vCPU / {res['memory_limit']} allocated, "
                    f"observed peak {res['cpu_utilization']}% CPU and "
                    f"{res['memory_utilization']}% memory."
                ),
                "current_cost": res["monthly_cost"],
                "potential_savings": res["wasted_cost"],
                "resource_url": res["url"],
                "rationale": rationale.explain(res),
            }
        )
        tracer.step(
            ANALYSIS,
            f"{res['resource_id']} → {res['status']} "
            f"(${res['wasted_cost']:.2f}/mo recoverable)",
            status=WARN if res["severity"] == "HIGH" else INFO,
            resource_id=res["resource_id"],
            detail={
                "rule": (rationale.rule_triggered(res) or {}).get("id"),
                "observed": {
                    "cpu_peak_pct": res["cpu_utilization"],
                    "memory_peak_pct": res["memory_utilization"],
                    "min_instances": res.get("min_instances"),
                    "source": res.get("metrics_source"),
                },
                "allocated": {"cpu": res["cpu_limit"], "memory": res["memory_limit"]},
                "monthly_cost": res["monthly_cost"],
                "recoverable": res["wasted_cost"],
                "severity": res["severity"],
            },
        )

    # Real inventory only. The untagged-image list used to be hardcoded, which
    # meant a "live" audit still reported a resource that did not exist.
    if settings.MOCK_MODE:
        extra = {
            "untagged_images": [
                {"resource_id": "demo/legacy-build", "short_id": "sha256:demo", "monthly_cost": 0.10}
            ],
            "orphan_disks": [],
            "unused_addresses": [],
            "problems": [],
        }
    else:
        from app.tools import gcp_inventory

        found = gcp_inventory.discover_all(force_refresh=force_refresh)
        extra = {
            "untagged_images": found["untagged_images"],
            "orphan_disks": found["orphan_disks"],
            "unused_addresses": found["unused_addresses"],
            "problems": found["problems"],
        }

    return {
        "data_source": source,
        "project_id": settings.PROJECT_ID,
        "regions_scanned": list(settings.regions),
        "idle_services": anomalies,
        "below_threshold": negligible,
        **extra,
    }


# ----------------------------------------------------------------------
# Chart aggregations for the dashboard
# ----------------------------------------------------------------------
def build_charts(
    resources: List[Dict[str, Any]],
    remediations: List[Dict[str, Any]],
    inventory: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Charts of the whole estate.

    `resources` is Cloud Run only — the utilization series come from it.
    `inventory` is every resource type and drives the cost views, so the charts
    agree with the KPIs instead of silently omitting disks and IPs.
    """
    estate = inventory if inventory is not None else resources

    ranking = [
        {
            "label": r["resource_id"],
            "allocated": r["monthly_cost"],
            "wasted": r["wasted_cost"],
        }
        for r in sorted(estate, key=lambda x: x["monthly_cost"], reverse=True)[:6]
    ]

    by_status: Dict[str, float] = {}
    for r in estate:
        by_status[r["status"]] = round(by_status.get(r["status"], 0.0) + r["monthly_cost"], 2)
    distribution = [{"label": k, "value": v} for k, v in sorted(by_status.items(), key=lambda kv: -kv[1])]

    # Prefer a real Cloud Monitoring history; model one only if unavailable.
    if settings.USE_REAL_METRICS and not settings.MOCK_MODE:
        real_history = gcp_monitoring.fetch_utilization_history()
        if real_history is not None:
            # Even a short history is the truth. The UI says "collecting" rather
            # than drawing a modelled line over real-but-sparse data.
            charts = _assemble_charts(
                ranking, distribution, real_history, resources, remediations, estate
            )
            charts["trend_source"] = "monitoring"
            return charts

    # Aggregate utilization across the fleet for the trend chart.
    series_by_time: Dict[str, Dict[str, float]] = {}
    for r in resources:
        for point in get_utilization_series(r["resource_id"]):
            slot = series_by_time.setdefault(point["t"], {"cpu": 0.0, "memory": 0.0, "n": 0})
            slot["cpu"] += point["cpu"]
            slot["memory"] += point["memory"]
            slot["n"] += 1
    trend = [
        {
            "t": t,
            "cpu": round(v["cpu"] / v["n"], 1),
            "memory": round(v["memory"] / v["n"], 1),
        }
        for t, v in series_by_time.items()
    ]

    charts = _assemble_charts(
        ranking, distribution, trend, resources, remediations, estate
    )
    charts["trend_source"] = "modelled"
    return charts


SETTLED_STATES = ("Healthy", "Tolerated")


def _efficiency_radar(estate, resources, remediations) -> List[Dict[str, Any]]:
    """Six dimensions of *efficiency*, not of raw utilization.

    The earlier version plotted average CPU and memory directly, which inverted
    the meaning: a service that correctly scales to zero sits at 1% CPU and
    dragged the score down, so a perfectly optimised fleet scored badly. These
    axes measure how close the estate is to the shape it should have.
    """
    count = max(len(estate), 1)
    total_cost = sum(r["monthly_cost"] for r in estate) or 1.0
    total_waste = sum(r["wasted_cost"] for r in estate)
    settled = [r for r in estate if r["status"] in SETTLED_STATES]

    # 1. How much of the bill is actually buying something.
    cost = 100 * (1 - min(1.0, total_waste / total_cost))

    # 2. How many resources have no recoverable waste worth acting on.
    right_sized = 100 * len(settled) / count

    # 3. Scale-to-zero where it is warranted: an always-on service that is busy
    #    is fine; an always-on service that is idle is not.
    scaling_ok = 0
    for r in resources:
        min_instances = int(r.get("min_instances") or 0)
        if min_instances == 0 or r["cpu_utilization"] >= 10:
            scaling_ok += 1
    scaling = 100 * scaling_ok / max(len(resources), 1) if resources else 100.0

    # 4. Judged on measurements rather than a model.
    measured = len([r for r in resources if r.get("metrics_source") == "monitoring"])
    observability = 100 * measured / max(len(resources), 1) if resources else 100.0

    # 5. Findings the agent resolved on its own, out of everything it found.
    #    Nothing to fix reads as fully automated, not as zero.
    open_findings = len([r for r in estate if r["status"] not in SETTLED_STATES])
    handled = len(remediations)
    automation = 100.0 if not (open_findings + handled) else (
        100 * handled / (open_findings + handled)
    )

    # 6. Nothing irreversible left lying around unattended.
    orphans = len([r for r in estate if r["status"] in ("Orphaned", "Unused")])
    governance = 100 * (1 - orphans / count)

    return [
        {"axis": "radar.cost", "value": round(cost, 1)},
        {"axis": "radar.rightsizing", "value": round(right_sized, 1)},
        {"axis": "radar.scaling", "value": round(scaling, 1)},
        {"axis": "radar.observability", "value": round(observability, 1)},
        {"axis": "radar.automation", "value": round(automation, 1)},
        {"axis": "radar.governance", "value": round(governance, 1)},
    ]


def _assemble_charts(
    ranking, distribution, trend, resources, remediations, estate=None
) -> Dict[str, Any]:
    estate = estate if estate is not None else resources
    total_cost = sum(r["monthly_cost"] for r in estate) or 1.0
    total_waste = sum(r["wasted_cost"] for r in estate)
    radar = _efficiency_radar(estate, resources, remediations)

    # Cumulative realized savings over the remediation history.
    cumulative, running = [], 0.0
    for rem in remediations[-12:]:
        running += rem.get("savings", 0.0)
        cumulative.append({"t": rem.get("timestamp", "")[11:16], "value": round(running, 2)})

    return {
        "ranking": ranking,
        "distribution": distribution,
        "trend": trend,
        "radar": radar,
        "savings_curve": cumulative,
    }
