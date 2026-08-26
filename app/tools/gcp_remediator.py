"""Remediation tools exposed to the agent as callable functions.

The docstrings here are part of the prompt: `google-genai` turns them into the
function declarations Gemini sees, so they describe both *what* each tool does
and *when* the autonomy matrix allows it.

Safety rules enforced in code (not just in the prompt):
  * every action checks the Memory Bank first, so a resource is never
    remediated twice;
  * anything above HIGH_RISK_ROI_THRESHOLD is downgraded to an approval ticket
    even if the model tries to execute it directly.
"""

import logging
from typing import Any, Dict, Optional

from app.core.config import settings
from app.core.trace import DECISION, EXECUTION, INFO, OK, WARN, tracer
from app.tools import gcp_actions
from app.tools.memory_tools import memory_bank

logger = logging.getLogger(__name__)


def _console_url(resource_id: str) -> str:
    return (
        f"https://console.cloud.google.com/run/detail/{settings.REGION}"
        f"/{resource_id}/metrics?project={settings.PROJECT_ID}"
    )


def _current_shape(resource_id: str) -> Dict[str, Any]:
    """The resource's shape right now, for comparison against past actions."""
    try:
        from app.tools.gcp_metrics import describe_resources

        for r in describe_resources(allow_discovery=False)[0]:
            if r["resource_id"] == resource_id:
                return {
                    "cpu": r.get("cpu_limit"),
                    "memory": r.get("memory_limit"),
                    "min_instances": r.get("min_instances"),
                }
    except Exception:
        pass
    return {}


def _already_handled(resource_id: str) -> str:
    """Return a message if this resource needs no further action right now."""
    history = memory_bank.check_history(resource_id)
    if history.get("found") and not history.get("applied", True):
        # A dry run changed nothing, so the resource is still exactly as
        # wasteful as it was. Blocking it would leave an anomaly no one can act
        # on — and the dashboard would show it red with no ticket.
        return ""
    if history.get("found"):
        acted_on = history.get("last_state") or {}
        now = _current_shape(resource_id)

        # Only a resource that is *unchanged* since we acted is a duplicate.
        # A partial remediation that left waste behind must remain actionable,
        # otherwise the dashboard shows an anomaly nobody can ever resolve.
        if acted_on and now and acted_on != now:
            logger.info(
                "%s changed since the last action (%s -> %s); eligible again",
                resource_id, acted_on, now,
            )
            return ""

        return (
            f"SKIPPED: {resource_id} was already remediated "
            f"({history['last_action']} at {history['last_timestamp']}) "
            "and has not changed since. Do not act on it again."
        )
    if memory_bank.has_pending_approval(resource_id):
        return f"SKIPPED: {resource_id} already has an approval ticket awaiting a human decision."

    rejected = memory_bank.last_rejection(resource_id)
    if rejected:
        # A human already declined this. Re-raising it every scan is the alert
        # fatigue the savings threshold exists to prevent.
        return (
            f"SKIPPED: a human rejected '{rejected['proposed_action']}' for "
            f"{resource_id} on {rejected.get('resolved_at', 'a previous run')}. "
            "Do not propose it again."
        )
    return ""


def resize_cloud_run(
    service_id: str,
    new_memory: str,
    estimated_savings: float = 0.0,
    new_cpu: str = "",
    new_min_instances: Optional[int] = None,
) -> str:
    """Reduce the memory allocation of an idle or oversized Cloud Run service.

    Args:
        service_id: Name of the Cloud Run service, e.g. "billing-worker".
        new_memory: Target memory limit, e.g. "512Mi" or "1Gi".
        estimated_savings: Expected monthly USD saved by the resize.

    Returns a status string. High-value resizes are converted into an approval
    ticket instead of being applied.
    """
    guard = _already_handled(service_id)
    if guard:
        logger.info(guard)
        tracer.step(DECISION, guard, status=INFO, resource_id=service_id)
        return guard

    if estimated_savings >= settings.HIGH_RISK_ROI_THRESHOLD:
        return request_human_approval(
            resource_id=service_id,
            proposed_action=f"Resize memory to {new_memory}",
            estimated_roi=estimated_savings,
            detailed_reason=(
                f"Autonomy Level 2: a ${estimated_savings:.2f}/mo change on a production "
                "service requires human validation before execution."
            ),
            target_memory=new_memory,
            action_key="act.right_size",
            change_specs=[{"kind": "memory", "from": "current", "to": new_memory}],
            reason_key="reason.autonomy2",
            reason_params={"savings": f"{estimated_savings:.2f}"},
        )

    tracer.step(
        DECISION, f"Autonomy Level 1 → applying resize on {service_id} directly",
        resource_id=service_id,
        detail={"new_memory": new_memory,
                "estimated_savings_monthly": round(float(estimated_savings), 2),
                "threshold": settings.HIGH_RISK_ROI_THRESHOLD},
    )
    ok, message = gcp_actions.resize_service(
        service_id, new_memory, new_cpu=new_cpu, new_min_instances=new_min_instances
    )
    if not ok:
        memory_bank.log_event(message, level="WARN", resource_id=service_id)
        return message

    memory_bank.log_remediation(
        event_id=f"resize_{service_id}",
        resource_id=service_id,
        action=f"resize_cloud_run -> {new_memory}",
        savings=estimated_savings or 15.0,
        applied=settings.writes_enabled,
        resource_state=_current_shape(service_id),
    )
    return message


def delete_orphan_disk(disk_id: str, estimated_savings: float = 25.0, zone: str = "") -> str:
    """Delete a persistent disk that is not attached to any running instance.

    Args:
        disk_id: Name of the orphaned disk.
        estimated_savings: Expected monthly USD saved.

    Disk deletion is destructive, so this always routes through human approval.
    """
    guard = _already_handled(disk_id)
    if guard:
        return guard

    return request_human_approval(
        resource_id=disk_id,
        proposed_action="Delete orphaned persistent disk",
        estimated_roi=estimated_savings,
        detailed_reason=(
            "Autonomy Level 2: disk deletion is irreversible and always requires "
            "human validation, regardless of estimated savings."
        ),
        action_key="act.delete_disk",
        reason_key="reason.irreversible",
        action_type="delete_disk",
        action_params={"zone": zone},
    )


def purge_untagged_image(image_id: str, estimated_savings: float = 5.0) -> str:
    """Delete an untagged container image from Artifact Registry.

    Args:
        image_id: Identifier of the untagged image.
        estimated_savings: Expected monthly USD saved on storage.

    This is an Autonomy Level 1 (safe) action and executes directly.
    """
    guard = _already_handled(image_id)
    if guard:
        return guard

    ok, message = gcp_actions.delete_untagged_image(image_id)
    if not ok:
        memory_bank.log_event(message, level="WARN", resource_id=image_id)
        return message

    memory_bank.log_remediation(
        event_id=f"purge_{image_id}",
        resource_id=image_id,
        action="purge_untagged_image",
        savings=estimated_savings,
        applied=settings.writes_enabled,
    )
    return message


def request_human_approval(
    resource_id: str,
    proposed_action: str,
    estimated_roi: float,
    resource_url: str = "",
    detailed_reason: str = "",
    severity: str = "HIGH",
    target_memory: str = "512Mi",
    rationale: Optional[Dict[str, Any]] = None,
    action_key: str = "",
    change_specs: Optional[list] = None,
    reason_key: str = "",
    reason_params: Optional[Dict[str, Any]] = None,
    action_type: str = "resize_service",
    action_params: Optional[Dict[str, Any]] = None,
    model_recommendation: str = "",
) -> str:
    """Open a human-in-the-loop approval ticket for a high-risk action.

    Args:
        resource_id: The resource the action targets.
        proposed_action: Short description, e.g. "Resize memory to 1Gi".
        estimated_roi: Estimated monthly USD saved if approved.
        resource_url: Optional deep link to the GCP console.
        detailed_reason: Why the change is being proposed, in one or two sentences.
        severity: LOW, MEDIUM or HIGH.
        target_memory: The concrete memory limit to apply on approval, e.g. "512Mi".

    The ticket appears in the dashboard; nothing is executed until a human approves.
    """
    guard = _already_handled(resource_id)
    if guard:
        tracer.step(DECISION, guard, status=INFO, resource_id=resource_id)
        return guard

    tracer.step(
        DECISION,
        f"Autonomy Level 2 → escalating {resource_id} for human approval",
        status=WARN, resource_id=resource_id,
        detail={"proposed_action": proposed_action,
                "estimated_savings_monthly": round(float(estimated_roi), 2),
                "severity": severity,
                "threshold": settings.HIGH_RISK_ROI_THRESHOLD,
                "reason": detailed_reason},
    )

    ticket = memory_bank.add_approval(
        {
            "resource_id": resource_id,
            "proposed_action": proposed_action,
            "estimated_roi": round(float(estimated_roi), 2),
            "resource_url": resource_url or _console_url(resource_id),
            "detailed_reason": detailed_reason or "Automated anomaly detection requested this change.",
            "severity": severity,
            "target_memory": target_memory,
            "rationale": rationale,
            # Stored structurally so the ticket can be re-rendered in any
            # language, not frozen in the one the agent happened to run in.
            "action_key": action_key,
            "change_specs": change_specs or [],
            "reason_key": reason_key,
            "reason_params": reason_params or {},
            # Which handler executes this on approval. Without it every ticket
            # was resized as if it were a Cloud Run service.
            "action_type": action_type,
            "action_params": action_params or {},
            # The model's own words. Kept verbatim: it is analysis, not a label,
            # and paraphrasing it into a catalogue string loses the reasoning.
            "model_recommendation": model_recommendation,
        }
    )
    logger.info("Approval ticket %s created for %s", ticket["ticket_id"], resource_id)
    return f"PENDING_APPROVAL: ticket {ticket['ticket_id']} created for {resource_id}."


def execute_approved_action(resource_id: str) -> str:
    """Apply an action a human approved, using the handler that action needs."""
    approval = next(
        (a for a in memory_bank.snapshot()["approvals"]
         if a["resource_id"] == resource_id and a["status"] == "APPROVED"),
        None,
    )
    if not approval:
        return f"No approved action found for {resource_id}."

    action_type = approval.get("action_type") or "resize_service"
    params = approval.get("action_params") or {}

    tracer.step(
        EXECUTION,
        f"Human approval granted — executing '{approval['proposed_action']}' on {resource_id}",
        resource_id=resource_id,
        detail={"ticket_id": approval.get("ticket_id"),
                "approved_at": approval.get("resolved_at"),
                "action_type": action_type,
                "action_params": params or None,
                "estimated_savings_monthly": approval["estimated_roi"]},
    )

    # Dispatch by what the ticket actually proposes. Resizing a disk as though
    # it were a Cloud Run service would target a resource that does not exist.
    if action_type == "delete_disk":
        ok, message = gcp_actions.delete_disk(resource_id, zone=params.get("zone", ""))
    elif action_type == "release_address":
        ok, message = gcp_actions.release_address(resource_id, region=params.get("region", ""))
    elif action_type == "delete_image":
        ok, message = gcp_actions.delete_untagged_image(params.get("full_name", resource_id))
    elif action_type == "resize_service":
        ok, message = gcp_actions.resize_service(
            resource_id,
            approval.get("target_memory") or "512Mi",
            new_cpu=params.get("cpu", ""),
            new_min_instances=params.get("min_instances"),
        )
    else:
        message = f"REFUSED: unknown action type '{action_type}' for {resource_id}."
        tracer.step(EXECUTION, message, status=WARN, resource_id=resource_id)
        return message

    if not ok:
        memory_bank.log_event(message, level="WARN", resource_id=resource_id)
        tracer.step(EXECUTION, message, status=WARN, resource_id=resource_id,
                    detail={"action_type": action_type})
        return message

    memory_bank.log_remediation(
        run_id=approval.get("run_id"),
        event_id=f"approved_{approval.get('ticket_id', resource_id)}",
        resource_id=resource_id,
        action=approval["proposed_action"],
        action_key=approval.get("action_key") or "",
        savings=approval["estimated_roi"],
        source="human-approved",
        applied=settings.writes_enabled,
        resource_state=_current_shape(resource_id),
    )
    tracer.step(
        EXECUTION, f"Action complete on {resource_id}", status=OK, resource_id=resource_id,
        detail={"action_type": action_type, "outcome": message,
                "booked_savings_monthly": approval["estimated_roi"],
                "really_applied": settings.writes_enabled},
    )
    return message

    memory_bank.log_remediation(
        run_id=approval.get("run_id"),
        event_id=f"approved_{approval.get('ticket_id', resource_id)}",
        resource_id=resource_id,
        action=approval["proposed_action"],
        action_key=approval.get("action_key") or "",
        savings=approval["estimated_roi"],
        source="human-approved",
        applied=settings.writes_enabled,
        resource_state=_current_shape(resource_id),
    )
    tracer.step(
        EXECUTION, f"Action complete on {resource_id}", status=OK, resource_id=resource_id,
        detail={"outcome": message, "booked_savings_monthly": approval["estimated_roi"],
                "really_applied": settings.writes_enabled},
    )
    return message
