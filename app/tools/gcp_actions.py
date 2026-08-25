"""Real mutations against Google Cloud.

This is the layer that actually changes infrastructure. Everything here is
gated by ``settings.DRY_RUN``: while it is true (the default) each function
computes and reports the change it *would* make without calling the mutating
API. Set ``DRY_RUN=false`` to let the agent act for real.

Each function returns ``(ok, message)``.
"""

import logging
from typing import Any, Dict, Optional, Tuple

from app.core.config import settings
from app.core.trace import EXECUTION, OK, WARN, tracer

logger = logging.getLogger(__name__)


class ActionBlocked(Exception):
    """Raised when a mutation is refused for a reason the agent should hear."""


def _dry(message: str, request: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    logger.info("[DRY_RUN] %s", message)
    tracer.step(
        EXECUTION, f"DRY_RUN · not sent to GCP: {message}", status=WARN,
        detail={"dry_run": True, "would_send": request,
                "hint": "Set DRY_RUN=false to apply changes for real."},
    )
    return True, f"DRY_RUN: would have {message}. Set DRY_RUN=false to apply."


def resize_service(
    service_id: str,
    new_memory: str,
    new_cpu: str = "",
    new_min_instances: Optional[int] = None,
) -> Tuple[bool, str]:
    """Apply a new shape to a Cloud Run service.

    Covers memory, CPU *and* min-instances. Scaling is the usual biggest saving,
    and a ticket that says "set min-instances to 0" must actually do that —
    silently applying only the memory change would book savings never realised.

    Read-modify-write, so image, env vars and concurrency are preserved.
    """
    bits = []
    if new_cpu:
        bits.append(f"{new_cpu} vCPU")
    bits.append(new_memory)
    if new_min_instances is not None:
        bits.append(f"min-instances {new_min_instances}")
    target = " / ".join(bits)
    name = f"projects/{settings.PROJECT_ID}/locations/{settings.REGION}/services/{service_id}"
    intent = {
        "api": "run.googleapis.com/v2",
        "method": "projects.locations.services.patch",
        "name": name,
        "changes": {
            "memory": new_memory,
            **({"cpu": new_cpu} if new_cpu else {}),
            **({"min_instances": new_min_instances} if new_min_instances is not None else {}),
        },
    }

    if not settings.writes_enabled:
        return _dry(f"resized {service_id} to {target}", request=intent)

    try:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()

        # 1. Read the current shape, so the trace shows what we are changing from.
        with tracer.timed(
            EXECUTION, f"GET service {service_id}", resource_id=service_id
        ) as step:
            step.add(request={"method": "services.get", "name": name})
            service = client.get_service(request=run_v2.GetServiceRequest(name=name))
            if not service.template.containers:
                step.add(response={"containers": 0})
                return False, f"{service_id} has no containers to resize."
            container = service.template.containers[0]
            previous = dict(container.resources.limits)
            previous_min = service.template.scaling.min_instance_count
            step.add(response={
                "current_limits": previous,
                "min_instances": service.template.scaling.min_instance_count,
                "revision": service.template.revision or "(auto)",
            })

        # 2. Send the mutation.
        container.resources.limits["memory"] = new_memory
        if new_cpu:
            container.resources.limits["cpu"] = new_cpu
        if new_min_instances is not None:
            service.template.scaling.min_instance_count = new_min_instances
        # Let Cloud Run mint a new revision name rather than reusing the old one.
        service.template.revision = ""

        with tracer.timed(
            EXECUTION, f"PATCH service {service_id} → {target}", resource_id=service_id
        ) as step:
            step.add(request={
                "method": "services.patch",
                "name": name,
                "from": {**previous, "min_instances": previous_min},
                "to": {**dict(container.resources.limits),
                       **({"min_instances": new_min_instances}
                          if new_min_instances is not None else {})},
            })
            operation = client.update_service(
                request=run_v2.UpdateServiceRequest(service=service)
            )
            step.add(operation={"name": getattr(operation, "operation", None) and
                                str(operation.operation.name), "done": False})
            result = operation.result(timeout=300)

            revision = result.latest_created_revision.split("/")[-1]
            conditions = [
                {"type": c.type_, "state": c.state.name, "message": c.message}
                for c in result.conditions
            ]
            ready = next(
                (c for c in conditions if c["type"] == "Ready"),
                {"state": "UNKNOWN", "message": ""},
            )
            step.add(response={
                "gcp_confirmed": ready["state"] == "CONDITION_SUCCEEDED",
                "applied_min_instances": result.template.scaling.min_instance_count,
                "ready_state": ready["state"],
                "ready_message": ready["message"],
                "new_revision": revision,
                "applied_limits": dict(result.template.containers[0].resources.limits)
                if result.template.containers else {},
                "uri": result.uri,
                "conditions": conditions,
            })

        logger.info("Resized %s: %s -> %s (revision %s)", service_id, previous, target, revision)
        return True, (
            f"APPLIED: {service_id} resized from "
            f"{previous.get('cpu', '?')} vCPU / {previous.get('memory', '?')} to {target}. "
            f"New revision: {revision}"
        )

    except Exception as exc:
        logger.error("Failed to resize %s: %s", service_id, exc)
        return False, f"FAILED to resize {service_id}: {type(exc).__name__}: {str(exc)[:200]}"


def set_min_instances(service_id: str, min_instances: int = 0) -> Tuple[bool, str]:
    """Scale a service's floor down so it stops billing while idle."""
    if not settings.writes_enabled:
        return _dry(f"set min-instances of {service_id} to {min_instances}")

    try:
        from google.cloud import run_v2

        client = run_v2.ServicesClient()
        name = f"projects/{settings.PROJECT_ID}/locations/{settings.REGION}/services/{service_id}"

        service = client.get_service(request=run_v2.GetServiceRequest(name=name))
        previous = service.template.scaling.min_instance_count
        service.template.scaling.min_instance_count = min_instances
        service.template.revision = ""

        client.update_service(request=run_v2.UpdateServiceRequest(service=service)).result(timeout=300)
        logger.info("min-instances of %s: %s -> %s", service_id, previous, min_instances)
        return True, f"APPLIED: min-instances of {service_id} changed from {previous} to {min_instances}."

    except Exception as exc:
        logger.error("Failed to set min-instances on %s: %s", service_id, exc)
        return False, f"FAILED on {service_id}: {type(exc).__name__}: {str(exc)[:200]}"


def delete_disk(disk_id: str, zone: str = "") -> Tuple[bool, str]:
    """Delete an unattached persistent disk. Irreversible."""
    zone = zone or f"{settings.REGION}-a"
    if not settings.writes_enabled:
        return _dry(
            f"deleted disk {disk_id}",
            request={"api": "compute/v1", "method": "disks.delete",
                     "project": settings.PROJECT_ID, "zone": zone, "disk": disk_id},
        )

    try:
        from google.cloud import compute_v1

        disks = compute_v1.DisksClient()

        with tracer.timed(EXECUTION, f"GET disk {disk_id}", resource_id=disk_id) as step:
            step.add(request={"method": "disks.get", "zone": zone, "disk": disk_id})
            disk = disks.get(project=settings.PROJECT_ID, zone=zone, disk=disk_id)
            attached = [u.split("/")[-1] for u in disk.users]
            step.add(response={"size_gb": disk.size_gb, "type": disk.type_.split("/")[-1],
                               "attached_to": attached or None})

        if disk.users:
            tracer.step(
                EXECUTION, f"REFUSED to delete {disk_id} — still attached",
                status=WARN, resource_id=disk_id,
                detail={"attached_to": attached,
                        "reason": "Deleting an attached disk would break a running instance."},
            )
            return False, f"REFUSED: disk {disk_id} is still attached to {', '.join(attached)}."

        with tracer.timed(EXECUTION, f"DELETE disk {disk_id}", resource_id=disk_id) as step:
            step.add(request={"method": "disks.delete", "zone": zone, "disk": disk_id})
            op = disks.delete(project=settings.PROJECT_ID, zone=zone, disk=disk_id)
            result = op.result(timeout=300)
            step.add(response={
                "gcp_confirmed": True,
                "operation_status": getattr(result, "status", "DONE") and str(getattr(result, "status", "DONE")),
                "freed_gb": disk.size_gb,
            })

        logger.info("Deleted disk %s in %s (%sGB)", disk_id, zone, disk.size_gb)
        return True, f"APPLIED: disk {disk_id} ({disk.size_gb}GB) deleted from {zone}."

    except Exception as exc:
        logger.error("Failed to delete disk %s: %s", disk_id, exc)
        return False, f"FAILED to delete {disk_id}: {type(exc).__name__}: {str(exc)[:200]}"


def release_address(address_id: str, region: str = "") -> Tuple[bool, str]:
    """Release a reserved static IP that is not attached to anything."""
    region = region or settings.REGION
    if not settings.writes_enabled:
        return _dry(
            f"released static IP {address_id}",
            request={"api": "compute/v1", "method": "addresses.delete",
                     "project": settings.PROJECT_ID, "region": region, "address": address_id},
        )

    try:
        from google.cloud import compute_v1

        client = compute_v1.AddressesClient()

        with tracer.timed(EXECUTION, f"GET address {address_id}", resource_id=address_id) as step:
            step.add(request={"method": "addresses.get", "region": region, "address": address_id})
            addr = client.get(project=settings.PROJECT_ID, region=region, address=address_id)
            step.add(response={"address": addr.address, "status": str(addr.status),
                               "users": list(addr.users) or None})

        if str(addr.status) == "IN_USE" or addr.users:
            tracer.step(
                EXECUTION, f"REFUSED to release {address_id} — now in use",
                status=WARN, resource_id=address_id,
                detail={"status": str(addr.status), "users": list(addr.users)},
            )
            return False, f"REFUSED: {address_id} is in use."

        with tracer.timed(EXECUTION, f"DELETE address {address_id}", resource_id=address_id) as step:
            step.add(request={"method": "addresses.delete", "region": region,
                              "address": address_id})
            client.delete(project=settings.PROJECT_ID, region=region,
                          address=address_id).result(timeout=300)
            step.add(response={"gcp_confirmed": True, "released": addr.address})

        return True, f"APPLIED: static IP {address_id} ({addr.address}) released."

    except Exception as exc:
        logger.error("Failed to release %s: %s", address_id, exc)
        return False, f"FAILED to release {address_id}: {type(exc).__name__}: {str(exc)[:200]}"


def delete_untagged_image(image_id: str) -> Tuple[bool, str]:
    """Delete an untagged Artifact Registry image version.

    ``image_id`` must be the full version path:
    ``projects/P/locations/L/repositories/R/packages/PKG/versions/sha256:...``
    """
    if not settings.writes_enabled:
        return _dry(f"deleted untagged image {image_id}")

    try:
        from google.cloud import artifactregistry_v1

        client = artifactregistry_v1.ArtifactRegistryClient()
        client.delete_version(
            request=artifactregistry_v1.DeleteVersionRequest(name=image_id)
        ).result(timeout=300)
        logger.info("Deleted untagged image %s", image_id)
        return True, f"APPLIED: untagged image {image_id.split('/')[-1]} deleted."

    except Exception as exc:
        logger.error("Failed to delete image %s: %s", image_id, exc)
        return False, f"FAILED to delete image: {type(exc).__name__}: {str(exc)[:200]}"
