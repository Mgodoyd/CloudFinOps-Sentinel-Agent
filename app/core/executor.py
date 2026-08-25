"""Carries out a plan, step by step, enforcing the autonomy matrix.

The planner says what should happen. This decides whether each step may happen
without a human, dispatches it to the right tool, and records the outcome so the
planner can adapt if something fails.

Enforcement lives here, not in the prompt: a model that is convinced a disk
should be deleted still cannot delete one on its own.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.trace import DECISION, EXECUTION, INFO, OK, WARN, tracer
from app.tools.gcp_remediator import (
    delete_orphan_disk,
    purge_untagged_image,
    request_human_approval,
    resize_cloud_run,
)
from app.tools.memory_tools import memory_bank

logger = logging.getLogger(__name__)

# Tools whose effect cannot be undone always need a person, whatever they save.
IRREVERSIBLE = {"delete_disk", "release_address", "delete_image"}


class PlanExecutor:
    """Executes one plan and reports what happened to each step."""

    def __init__(self, analysis: Optional[Dict[str, Any]] = None):
        self.analysis = (analysis or {}).get("by_resource", {})
        self.results: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------
    def run(self, plan: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
        """Execute every step. Returns (results, failures)."""
        failures: List[Dict[str, str]] = []

        tracer.step(
            DECISION, f"Plan: {plan.get('goal', 'optimise the estate')}",
            detail={"steps": len(plan.get("steps", [])),
                    "rejected": plan.get("rejected_steps") or None,
                    "plan": plan.get("steps")},
        )

        for step in plan.get("steps", []):
            outcome = self._run_step(step)
            self.results.append(outcome)
            if outcome["status"] == "failed":
                failures.append({
                    "resource_id": step["resource_id"],
                    "tool": step["tool"],
                    "error": outcome["message"],
                })

        return self.results, failures

    # ------------------------------------------------------------------
    def _run_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        rid = step["resource_id"]
        tool = step["tool"]
        args = step.get("args") or {}
        saving = float(step.get("estimated_saving") or 0.0)

        def result(status: str, message: str) -> Dict[str, Any]:
            tracer.step(
                EXECUTION if status != "skipped" else DECISION,
                f"Step {step.get('order', '?')} · {tool} on {rid} → {status}",
                status={"done": OK, "awaiting_approval": WARN,
                        "skipped": INFO, "failed": "error"}.get(status, INFO),
                resource_id=rid,
                detail={"intent": step.get("intent"),
                        "expected_outcome": step.get("expected_outcome"),
                        "tool": tool, "args": args or None,
                        "estimated_saving_monthly": saving,
                        "outcome": message},
            )
            return {**step, "status": status, "message": message}

        if tool == "skip":
            return result("skipped", args.get("reason") or "No action warranted.")

        # --- autonomy matrix, enforced in code ---------------------------
        # Order matters. The value threshold is checked first, deliberately:
        # escalating a $0.50/month cleanup costs more human attention than it
        # saves, irreversible or not. Only above the threshold does the question
        # "may this run unattended?" arise.
        if saving and saving < settings.MIN_SAVINGS_THRESHOLD:
            return result(
                "skipped",
                f"${saving:.2f}/mo is below the ${settings.MIN_SAVINGS_THRESHOLD:.2f} "
                "action threshold.",
            )

        needs_human = (
            tool in IRREVERSIBLE
            or saving >= settings.HIGH_RISK_ROI_THRESHOLD
        )

        verdict = self.analysis.get(rid, {})
        recommendation = verdict.get("recommendation") or step.get("intent", "")
        reason = verdict.get("diagnosis") or step.get("intent", "")

        if needs_human:
            message = self._escalate(rid, tool, args, saving, recommendation, reason)
            status = "awaiting_approval" if message.startswith("PENDING") else "skipped"
            return result(status, message)

        return self._execute_now(step, result, rid, tool, args, saving)

    # ------------------------------------------------------------------
    def _escalate(self, rid, tool, args, saving, recommendation, reason) -> str:
        if tool == "delete_disk":
            return delete_orphan_disk(rid, estimated_savings=saving, zone=args.get("zone", ""))

        action_key = {
            "release_address": "act.release_ip",
            "delete_image": "act.purge_image",
        }.get(tool, "act.right_size")

        shape = {k: v for k, v in args.items()
                 if k in ("cpu", "min_instances", "zone", "region", "full_name")}

        return request_human_approval(
            resource_id=rid,
            proposed_action=recommendation or f"{tool} on {rid}",
            estimated_roi=saving,
            detailed_reason=reason,
            severity="HIGH" if saving >= settings.HIGH_RISK_ROI_THRESHOLD else "MEDIUM",
            target_memory=args.get("memory", "512Mi"),
            action_key=action_key,
            action_type={"release_address": "release_address",
                         "delete_image": "delete_image"}.get(tool, "resize_service"),
            action_params=shape,
            model_recommendation=recommendation,
            reason_key="reason.irreversible" if tool in IRREVERSIBLE else "",
        )

    def _execute_now(self, step, result, rid, tool, args, saving) -> Dict[str, Any]:
        """Level 1: the agent applies it directly."""
        if tool == "resize_service":
            message = resize_cloud_run(
                rid, args.get("memory", "512Mi"), estimated_savings=saving,
                new_cpu=args.get("cpu", ""), new_min_instances=args.get("min_instances"),
            )
        elif tool == "delete_image":
            message = purge_untagged_image(args.get("full_name", rid), estimated_savings=saving)
        else:
            return result("failed", f"No Level 1 handler for '{tool}'.")

        if message.startswith("FAILED"):
            return result("failed", message)
        if message.startswith("SKIPPED"):
            return result("skipped", message)
        return result("done", message)

    # ------------------------------------------------------------------
    def report(self, plan: Dict[str, Any], replans: int = 0) -> str:
        """A human-readable account of the plan and what became of it."""
        lines = [plan.get("goal", "").strip(), ""]
        buckets = {"done": [], "awaiting_approval": [], "skipped": [], "failed": []}
        for r in self.results:
            buckets[r["status"]].append(r)

        headings = {
            "done": "**Applied**",
            "awaiting_approval": "**Awaiting approval**",
            "skipped": "**Skipped**",
            "failed": "**Failed**",
        }
        for status, heading in headings.items():
            if not buckets[status]:
                continue
            lines.append(heading)
            for r in buckets[status]:
                saving = f" (${r['estimated_saving']:.2f}/mo)" if r.get("estimated_saving") else ""
                lines.append(f"- `{r['resource_id']}` — {r['intent']}{saving}")
                if status in ("failed", "skipped"):
                    lines.append(f"  _{r['message']}_")
            lines.append("")

        total = sum(r.get("estimated_saving", 0.0) for r in self.results
                    if r["status"] in ("done", "awaiting_approval"))
        lines.append(f"**Estimated monthly savings:** ${total:.2f}")
        if replans:
            lines.append("")
            lines.append(f"_The plan was revised {replans} time(s) after a step failed._")
        return "\n".join(lines)
