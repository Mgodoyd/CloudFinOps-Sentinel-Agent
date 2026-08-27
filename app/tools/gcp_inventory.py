"""Real resource discovery across the GCP project.

Cloud Run is the primary target, but a FinOps audit that only looks at one
service misses most waste. This module discovers what actually exists —
services, orphaned disks, untagged images, unused static IPs — and reports
honestly when an API is disabled or a permission is missing, instead of
substituting invented data.

Every function returns ``(items, problems)`` where ``problems`` is a list of
human-readable reasons a source could not be read.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.trace import DISCOVERY, ERROR, INFO as INFO_, OK, WARN, tracer

logger = logging.getLogger(__name__)

Problem = Dict[str, str]


def _problem(source: str, exc: Exception) -> Problem:
    text = str(exc)
    if "has not been used in project" in text or "is disabled" in text:
        api = text.split("project")[0].strip() or source
        return {
            "source": source,
            "reason": "api_disabled",
            "detail": f"The API for {source} is not enabled on this project.",
        }
    if "403" in text or "PermissionDenied" in type(exc).__name__:
        return {
            "source": source,
            "reason": "permission_denied",
            "detail": f"Missing permission to read {source}.",
        }
    return {"source": source, "reason": "error", "detail": f"{type(exc).__name__}: {text[:120]}"}


# ----------------------------------------------------------------------
# Cloud Run — scanned across every configured region in parallel
# ----------------------------------------------------------------------
def _services_in_region(region: str) -> List[Dict[str, Any]]:
    from google.cloud import run_v2

    client = run_v2.ServicesClient()
    out = []
    for service in client.list_services(
        request=run_v2.ListServicesRequest(
            parent=f"projects/{settings.PROJECT_ID}/locations/{region}"
        )
    ):
        containers = service.template.containers
        limits = containers[0].resources.limits if containers else {}
        out.append(
            {
                "resource_id": service.name.split("/")[-1],
                "region": region,
                "cpu_limit": limits.get("cpu", "1"),
                "memory_limit": limits.get("memory", "512Mi"),
                "min_instances": service.template.scaling.min_instance_count,
                "max_instances": service.template.scaling.max_instance_count,
                "uri": service.uri,
            }
        )
    return out


def discover_cloud_run() -> Tuple[List[Dict[str, Any]], List[Problem]]:
    """Find Cloud Run services in every configured region."""
    if settings.MOCK_MODE:
        # Reached directly by fetch_services as well as through discover_all,
        # so the mode is checked on both doors rather than only the front one.
        return [], []

    regions = settings.regions
    services: List[Dict[str, Any]] = []
    problems: List[Problem] = []
    per_region: Dict[str, Any] = {}

    tracer.step(
        DISCOVERY,
        f"run.googleapis.com · ListServices across {len(regions)} region(s)",
        detail={
            "api": "run.googleapis.com/v2",
            "method": "projects.locations.services.list",
            "project": settings.PROJECT_ID,
            "regions": list(regions),
        },
    )

    with ThreadPoolExecutor(max_workers=min(10, len(regions))) as pool:
        futures = {pool.submit(_services_in_region, r): r for r in regions}
        for future, region in futures.items():
            try:
                found = future.result(timeout=30)
                services.extend(found)
                per_region[region] = len(found)
            except Exception as exc:
                per_region[region] = f"{type(exc).__name__}"
                if not problems:
                    problems.append(_problem("Cloud Run", exc))

    if services:
        problems = []  # at least one region answered, so the API is fine

    tracer.step(
        DISCOVERY,
        f"Cloud Run · {len(services)} service(s) found",
        status=OK if services else WARN,
        detail={
            "response": {
                "count": len(services),
                "per_region": {k: v for k, v in per_region.items() if v},
                "services": [
                    {
                        "name": s["resource_id"], "region": s["region"],
                        "cpu": s["cpu_limit"], "memory": s["memory_limit"],
                        "min_instances": s["min_instances"],
                    }
                    for s in services
                ],
            }
        },
    )
    return services, problems


# ----------------------------------------------------------------------
# Compute Engine — orphaned disks and unused static IPs
# ----------------------------------------------------------------------
def discover_orphan_disks() -> Tuple[List[Dict[str, Any]], List[Problem]]:
    """Persistent disks not attached to any instance — pure waste."""
    try:
        from google.cloud import compute_v1

        tracer.step(
            DISCOVERY, "compute.googleapis.com · disks.aggregatedList",
            detail={"api": "compute/v1", "method": "disks.aggregatedList",
                    "project": settings.PROJECT_ID},
        )
        client = compute_v1.DisksClient()
        disks = []
        for zone, scoped in client.aggregated_list(project=settings.PROJECT_ID):
            for disk in scoped.disks or []:
                if disk.users:
                    continue
                size = float(disk.size_gb)
                disks.append(
                    {
                        "resource_id": disk.name,
                        "type": "Persistent Disk",
                        "zone": zone.split("/")[-1],
                        "size_gb": size,
                        "disk_type": disk.type_.split("/")[-1] if disk.type_ else "pd-standard",
                        # Standard PD is $0.04/GB/month, SSD $0.17.
                        "monthly_cost": round(size * (0.17 if "ssd" in str(disk.type_) else 0.04), 2),
                    }
                )
        tracer.step(
            DISCOVERY, f"Persistent disks · {len(disks)} orphaned",
            status=OK if disks else INFO_,
            detail={"response": {"orphaned": len(disks), "disks": disks}},
        )
        return disks, []
    except Exception as exc:
        problem = _problem("Compute Engine disks", exc)
        tracer.step(DISCOVERY, f"Persistent disks · {problem['detail']}",
                    status=WARN, detail={"error": str(exc)[:220]})
        return [], [problem]


def discover_unused_addresses() -> Tuple[List[Dict[str, Any]], List[Problem]]:
    """Reserved static IPs that are not attached to anything (~$7.20/mo each)."""
    try:
        from google.cloud import compute_v1

        tracer.step(
            DISCOVERY, "compute.googleapis.com · addresses.aggregatedList",
            detail={"api": "compute/v1", "method": "addresses.aggregatedList",
                    "project": settings.PROJECT_ID},
        )
        client = compute_v1.AddressesClient()
        addresses = []
        for scope, scoped in client.aggregated_list(project=settings.PROJECT_ID):
            for addr in scoped.addresses or []:
                if str(addr.status) == "IN_USE" or addr.users:
                    continue
                addresses.append(
                    {
                        "resource_id": addr.name,
                        "type": "Static IP",
                        "region": scope.split("/")[-1],
                        "address": addr.address,
                        "monthly_cost": 7.20,
                    }
                )
        tracer.step(
            DISCOVERY, f"Static IPs · {len(addresses)} unused",
            status=OK if addresses else INFO_,
            detail={"response": {"unused": len(addresses), "addresses": addresses}},
        )
        return addresses, []
    except Exception as exc:
        problem = _problem("Compute Engine addresses", exc)
        tracer.step(DISCOVERY, f"Static IPs · {problem['detail']}",
                    status=WARN, detail={"error": str(exc)[:220]})
        return [], [problem]


# ----------------------------------------------------------------------
# Artifact Registry — untagged image versions
# ----------------------------------------------------------------------
def _digests_in_use() -> set:
    """Image digests a Cloud Run revision still points at.

    A revision references its image by digest, not by tag, so "untagged" does
    not mean "unreferenced". Deleting a digest an existing revision uses leaves
    that revision unable to start — it will not fail until Cloud Run next needs
    to pull, which is the worst time to find out.

    Best effort: if the revisions cannot be listed, nothing is excluded and the
    caller still has the tag check. Failing open here would be the wrong default
    only if the tag check did not exist.
    """
    digests: set = set()
    try:
        from google.cloud import run_v2

        client = run_v2.RevisionsClient()
        for region in settings.regions:
            parent = f"projects/{settings.PROJECT_ID}/locations/{region}"
            try:
                services = run_v2.ServicesClient().list_services(
                    request=run_v2.ListServicesRequest(parent=parent)
                )
            except Exception:
                continue
            for service in services:
                for revision in client.list_revisions(
                    request=run_v2.ListRevisionsRequest(parent=service.name)
                ):
                    for container in revision.containers:
                        if "@sha256:" in container.image:
                            digests.add(container.image.split("@")[-1])
    except Exception as exc:
        logger.warning("Could not list revision images (%s); relying on tags alone", exc)
    return digests


def discover_untagged_images(max_per_repo: int = 25) -> Tuple[List[Dict[str, Any]], List[Problem]]:
    """Image versions with no tags — usually orphaned build layers."""
    try:
        from google.cloud import artifactregistry_v1

        tracer.step(
            DISCOVERY, "artifactregistry.googleapis.com · listing untagged versions",
            detail={"api": "artifactregistry/v1", "project": settings.PROJECT_ID},
        )
        client = artifactregistry_v1.ArtifactRegistryClient()
        images: List[Dict[str, Any]] = []
        # Untagged is not the same as unused: a revision pins its image by
        # digest. These are the digests something still runs on.
        in_use = _digests_in_use()

        for region in settings.regions:
            try:
                repos = list(
                    client.list_repositories(
                        parent=f"projects/{settings.PROJECT_ID}/locations/{region}"
                    )
                )
            except Exception:
                continue  # region has no registry

            for repo in repos:
                for package in client.list_packages(parent=repo.name):
                    count = 0
                    for version in client.list_versions(
                        request=artifactregistry_v1.ListVersionsRequest(
                            parent=package.name,
                            view=artifactregistry_v1.VersionView.FULL,
                        )
                    ):
                        if version.related_tags:
                            continue
                        if version.name.split("/")[-1] in in_use:
                            # Untagged, but a revision still points at it.
                            continue
                        images.append(
                            {
                                "resource_id": version.name,
                                "short_id": version.name.split("/")[-1][:19],
                                "type": "Container Image",
                                "repository": repo.name.split("/")[-1],
                                "created": version.create_time.rfc3339()
                                if version.create_time
                                else "",
                                # Untagged layers are typically a few hundred MB.
                                "monthly_cost": 0.10,
                            }
                        )
                        count += 1
                        if count >= max_per_repo:
                            break
        tracer.step(
            DISCOVERY, f"Artifact Registry · {len(images)} untagged image(s)",
            status=OK if images else INFO_,
            detail={"response": {"untagged": len(images)}},
        )
        return images, []
    except Exception as exc:
        problem = _problem("Artifact Registry", exc)
        tracer.step(DISCOVERY, f"Artifact Registry · {problem['detail']}",
                    status=WARN, detail={"error": str(exc)[:220]})
        return [], [problem]


# ----------------------------------------------------------------------
# Aggregate
# ----------------------------------------------------------------------
class _Cache:
    """TTL cache that also remembers the last result indefinitely.

    Freshness governs when to re-scan; it must not govern whether the dashboard
    has anything to show.
    """

    def __init__(self, ttl: int):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._value: Any = None
        self._expires: float = 0.0
        self._last: Any = None
        self._last_at: float = 0.0

    def get(self) -> Any:
        with self._lock:
            if self._value is not None and time.monotonic() < self._expires:
                return self._value
        return None

    def last(self) -> Any:
        with self._lock:
            return self._last

    def last_age_seconds(self) -> Optional[float]:
        with self._lock:
            return None if self._last is None else time.monotonic() - self._last_at

    def set(self, value: Any) -> Any:
        with self._lock:
            self._value = value
            self._expires = time.monotonic() + self.ttl
            self._last = value
            self._last_at = time.monotonic()
        return value

    def clear(self) -> None:
        with self._lock:
            self._value, self._expires = None, 0.0

    def reset(self) -> None:
        with self._lock:
            self._value = self._last = None
            self._expires = self._last_at = 0.0


_discovery_cache = _Cache(settings.METRICS_CACHE_TTL)


def has_scanned() -> bool:
    return _discovery_cache.last() is not None


def last_scan_age_seconds() -> Optional[float]:
    return _discovery_cache.last_age_seconds()


def discover_all(force_refresh: bool = False, allow_discovery: bool = True) -> Dict[str, Any]:
    """Run every discovery source concurrently and merge the results.

    Cached: a full scan touches four APIs across every configured region and
    takes seconds, while the dashboard polls every few seconds.
    """
    # Guarded here rather than at each call site. Every caller that reaches GCP
    # has to remember the mode, and one that forgets turns a simulated demo into
    # a live scan of somebody's project — which is how the post-action refresh
    # quietly replaced the demo fleet with real services mid-run.
    if settings.MOCK_MODE:
        return {"cloud_run": [], "orphan_disks": [], "unused_addresses": [],
                "untagged_images": [], "problems": [], "scanned_regions": [],
                "simulated": True}

    if force_refresh:
        _discovery_cache.clear()
        tracer.step(DISCOVERY, "Cache bypassed — re-querying GCP",
                    detail={"reason": "explicit audit"})

    cached = _discovery_cache.get()
    if cached is not None:
        return cached
    if not allow_discovery:
        # Serve the last scan if there is one; only report idle if there is not.
        previous = _discovery_cache.last()
        if previous is not None:
            return previous
        return {"cloud_run": [], "orphan_disks": [], "unused_addresses": [],
                "untagged_images": [], "problems": [], "scanned_regions": [],
                "idle": True}
    sources = {
        "cloud_run": discover_cloud_run,
        "orphan_disks": discover_orphan_disks,
        "unused_addresses": discover_unused_addresses,
        "untagged_images": discover_untagged_images,
    }

    results: Dict[str, Any] = {}
    problems: List[Problem] = []

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        futures = {name: pool.submit(fn) for name, fn in sources.items()}
        for name, future in futures.items():
            try:
                items, issues = future.result(timeout=60)
            except Exception as exc:
                items, issues = [], [_problem(name, exc)]
            results[name] = items
            problems.extend(issues)

    results["problems"] = problems
    results["scanned_regions"] = list(settings.regions)
    return _discovery_cache.set(results)
