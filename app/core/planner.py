"""Planning: the model decides what to do, in what order, and why.

The analyst decides *what is wrong*. The planner decides *what to do about it* —
which tool, on which resource, with which arguments, in which order, and what it
expects to happen. The agent then carries the plan out and, when a step fails,
asks the planner to adapt rather than abandoning the run.

Cost is bounded on purpose: one planning call per audit, plus one re-plan per
failed round up to MAX_REPLANS. A tool-calling loop would spend one request per
tool invocation, which exhausts a free-tier minute on a single audit.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Set

from app.core.config import settings
from app.core import guardrails

logger = logging.getLogger(__name__)

MAX_REPLANS = 2

# What the agent can actually do. The planner may only choose from these.
TOOLBOX = [
    {
        "tool": "resize_service",
        "applies_to": "Cloud Run",
        "args": {"memory": "e.g. 512Mi", "cpu": "e.g. 250m",
                 "min_instances": "integer, 0 to scale to zero"},
        "notes": "Read-modify-write; a new revision is deployed. Reversible.",
    },
    {
        "tool": "delete_disk",
        "applies_to": "Persistent Disk",
        "args": {"zone": "e.g. us-central1-a"},
        "notes": "IRREVERSIBLE. Always requires human approval.",
    },
    {
        "tool": "release_address",
        "applies_to": "Static IP",
        "args": {"region": "e.g. us-central1"},
        "notes": "IRREVERSIBLE — the same address cannot be reclaimed.",
    },
    {
        "tool": "delete_image",
        "applies_to": "Container Image",
        "args": {"full_name": "the version resource path"},
        "notes": "Safe: an untagged version cannot be deployed by name, and "
                 "discovery excludes any digest a live revision still uses.",
    },
    {
        "tool": "skip",
        "applies_to": "any",
        "args": {"reason": "why no action is warranted"},
        "notes": "Use when the resource is fine, already handled, or too risky.",
    },
]

PLANNER_INSTRUCTION = """
You are the planning stage of an autonomous FinOps agent. You receive an
analysis of a cloud estate and a fixed toolbox. Produce an ordered plan the
agent will execute.

Rules:
- Use only the tools listed. Never invent one, and never invent arguments.
- One step per resource. Order by impact: the largest recoverable saving first.
- `skip` anything the analysis judged acceptable, anything already handled, and
  anything whose recoverable saving is below the stated action threshold.
- State `intent` in one sentence: what this step is for.
- State `expected_outcome` concretely, so the agent can tell whether it worked.
- You do NOT decide whether a human must approve. The agent enforces that from
  its autonomy matrix. Plan the action; approval is applied on top.
- If you are asked to re-plan after a failure, do not repeat the step that
  failed in the same form. Either adapt the arguments, or skip the resource and
  explain why the failure makes it unsafe to retry.

Be concise. This plan is read by an engineer reviewing what the agent did.
"""

PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "order": {"type": "integer"},
                    "resource_id": {"type": "string"},
                    "tool": {
                        "type": "string",
                        "enum": ["resize_service", "delete_disk", "release_address",
                                 "delete_image", "skip"],
                    },
                    "args": {
                        "type": "object",
                        "properties": {
                            "memory": {"type": "string"},
                            "cpu": {"type": "string"},
                            "min_instances": {"type": "integer"},
                            "zone": {"type": "string"},
                            "region": {"type": "string"},
                            "full_name": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                    },
                    "intent": {"type": "string"},
                    "expected_outcome": {"type": "string"},
                    "estimated_saving": {"type": "number"},
                },
                "required": ["order", "resource_id", "tool", "intent",
                             "expected_outcome", "estimated_saving"],
            },
        },
    },
    "required": ["goal", "steps"],
}

VALID_TOOLS = {t["tool"] for t in TOOLBOX}


def _unwrap(value: Any) -> str:
    """A model that echoes the delimiters back still names a real resource."""
    text = str(value or "").strip()
    if text.startswith(guardrails.UNTRUSTED_OPEN):
        text = text[len(guardrails.UNTRUSTED_OPEN):]
    if text.endswith(guardrails.UNTRUSTED_CLOSE):
        text = text[: -len(guardrails.UNTRUSTED_CLOSE)]
    return text.strip()


class Planner:
    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    # ------------------------------------------------------------------
    def _call(self, prompt: str) -> Optional[Dict[str, Any]]:
        from google.genai import types

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=PLANNER_INSTRUCTION,
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=PLAN_SCHEMA,
                    http_options=types.HttpOptions(timeout=90_000),
                ),
            )
            return json.loads(response.text or "{}")
        except Exception as exc:
            logger.warning("Planning unavailable: %s: %s", type(exc).__name__, str(exc)[:160])
            return None

    # ------------------------------------------------------------------
    def plan(
        self,
        analysis: Dict[str, Any],
        resources: List[Dict[str, Any]],
        already_handled: List[str],
    ) -> Optional[Dict[str, Any]]:
        """Turn an analysis into an ordered, executable plan."""
        if not self.client:
            return None

        payload = {
            "project": settings.PROJECT_ID,
            "action_threshold_usd_month": settings.MIN_SAVINGS_THRESHOLD,
            "fleet_summary": analysis.get("summary", ""),
            "analysis": list(analysis.get("by_resource", {}).values()),
            "resources": [
                {"resource_id": r["resource_id"], "type": r.get("type", "Cloud Run"),
                 "zone": r.get("zone"), "region": r.get("region") or r.get("location"),
                 "status": r.get("status")}
                for r in resources
            ],
            "already_handled_do_not_repeat": already_handled,
            "toolbox": TOOLBOX,
        }
        plan = self._call("Produce an execution plan for this estate:\n"
                          + json.dumps(payload, indent=2))
        return self._validate(plan, {r["resource_id"] for r in resources})

    def replan(
        self,
        plan: Dict[str, Any],
        failures: List[Dict[str, str]],
        completed: List[str],
        known: Optional[Set[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Adapt the plan after one or more steps failed."""
        if not self.client or not failures:
            return None

        payload = {
            "original_goal": plan.get("goal", ""),
            "completed_steps": completed,
            "failed_steps": failures,
            "remaining_steps": [
                s for s in plan.get("steps", [])
                if s["resource_id"] not in completed
                and s["resource_id"] not in {f["resource_id"] for f in failures}
            ],
            "toolbox": TOOLBOX,
        }
        revised = self._call(
            "These steps failed. Produce a revised plan for what remains, "
            "adapting or skipping what failed:\n" + json.dumps(payload, indent=2)
        )
        return self._validate(revised, known)

    # ------------------------------------------------------------------
    @staticmethod
    def _validate(
        plan: Optional[Dict[str, Any]], known: Optional[Set[str]] = None
    ) -> Optional[Dict[str, Any]]:
        """Drop steps this agent cannot or must not carry out.

        Two checks, and the second matters more than it looks. The tool enum
        makes an unknown tool unlikely, but the executor must never be handed a
        name it cannot dispatch — that is how an agent silently does nothing
        while reporting success.

        The target was never constrained at all. A resource name reaches the
        model from the estate, and a step aimed at something that was never
        measured is a step against a resource nobody looked at. Constraining the
        verb and leaving the object free is half a guardrail.
        """
        if not plan or not isinstance(plan.get("steps"), list):
            return None

        valid, rejected, unmeasured = [], [], []
        for step in plan["steps"]:
            rid = _unwrap(step.get("resource_id"))
            if step.get("tool") not in VALID_TOOLS or not rid:
                rejected.append(step.get("tool"))
                continue
            if known is not None and rid not in known:
                unmeasured.append(rid)
                continue
            step["resource_id"] = rid
            step.setdefault("args", {})
            valid.append(step)

        if rejected:
            logger.warning("Planner proposed unknown tool(s): %s", rejected)
        if unmeasured:
            logger.warning(
                "Planner named %d resource(s) that were never measured: %s",
                len(unmeasured), unmeasured[:5],
            )

        plan["steps"] = sorted(valid, key=lambda s: s.get("order", 0))
        plan["rejected_steps"] = rejected
        plan["unmeasured_steps"] = unmeasured
        return plan
