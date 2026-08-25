"""LLM analysis of the fleet.

The deterministic layer measures — allocations, utilization, cost. Measurement
is fact, and facts should not be invented by a model. What the model does here
is the *judgement*: why a resource is wasteful, what shape it should have, and
what could go wrong if you change it.

One structured call covers the whole fleet. Per-resource calls would multiply
requests and exhaust a free-tier minute on a single audit.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

# The most recent fleet analysis, so the dashboard can attach it to resources
# without re-asking the model on every poll.
_last: Dict[str, Any] = {"by_resource": {}, "summary": "", "model": None, "at": None}


def last_analysis() -> Dict[str, Any]:
    return dict(_last)


def store_analysis(result: Optional[Dict[str, Any]]) -> None:
    global _last
    if result:
        from datetime import datetime, timezone

        _last = {**result, "at": datetime.now(timezone.utc).isoformat()}


def clear_analysis() -> None:
    global _last
    _last = {"by_resource": {}, "summary": "", "model": None, "at": None}

ANALYST_INSTRUCTION = """
You are a Google Cloud FinOps analyst. You are given measured facts about
resources — allocation, observed utilization, billing model and estimated cost.
The measurements are authoritative: never contradict or invent them.

For each resource, decide:
  * whether its allocation is justified by its observed usage,
  * the concrete shape it should have instead,
  * what could break if that change is applied.

Rules:
- Size from the observed peak plus headroom, never from the current limit.
- Never propose growing a resource.
- Cloud Run requires >= 1 vCPU for >= 4Gi memory, and >= 2 vCPU for >= 8Gi.
- Valid memory values: 128Mi, 256Mi, 512Mi, 1Gi, 2Gi, 4Gi, 8Gi, 16Gi, 32Gi.
- A service with min_instances > 0 bills 24/7 regardless of traffic; setting it
  to 0 is usually the single biggest saving available.
- Short observation windows mean low confidence. Say so rather than sounding
  certain, and prefer a conservative step you can repeat next audit.
- An unattached disk or an unused static IP is 100% waste; the only question is
  whether the data or address is still needed.
- Write for an engineer who must justify the change to their team: specific,
  quantified, no filler.
"""

# The shape the model must return. Enforced by the SDK, so no parsing guesswork.
ANALYSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "analyses": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "resource_id": {"type": "string"},
                    "verdict": {
                        "type": "string",
                        "enum": ["wasteful", "acceptable", "needs_investigation"],
                    },
                    "diagnosis": {"type": "string"},
                    "recommendation": {"type": "string"},
                    "target_cpu": {"type": "string"},
                    "target_memory": {"type": "string"},
                    "target_min_instances": {"type": "integer"},
                    "risk": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "monthly_saving": {"type": "number"},
                },
                "required": [
                    "resource_id", "verdict", "diagnosis", "recommendation",
                    "risk", "confidence", "monthly_saving",
                ],
            },
        },
        "fleet_summary": {"type": "string"},
    },
    "required": ["analyses", "fleet_summary"],
}


def _facts(resource: Dict[str, Any]) -> Dict[str, Any]:
    """The measured facts handed to the model — no interpretation."""
    return {
        "resource_id": resource["resource_id"],
        "type": resource.get("type", "Cloud Run"),
        "region": resource.get("location") or resource.get("region"),
        "allocated_cpu": resource.get("cpu_limit"),
        "allocated_memory": resource.get("memory_limit"),
        "min_instances": resource.get("min_instances"),
        "observed_cpu_peak_pct": resource.get("cpu_utilization"),
        "observed_memory_peak_pct": resource.get("memory_utilization"),
        "metrics_source": resource.get("metrics_source", "n/a"),
        "spec": resource.get("spec"),
        "estimated_monthly_cost_usd": resource.get("monthly_cost"),
        "detected_state": resource.get("status"),
    }


class FleetAnalyst:
    """Wraps one structured Gemini call over the whole fleet."""

    def __init__(self, client: Any, model: str):
        self.client = client
        self.model = model

    def analyse(self, resources: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Return {resource_id: analysis} plus a fleet summary, or None.

        Returns None when the model is unreachable so the caller can fall back
        and say so, rather than presenting a deterministic guess as an analysis.
        """
        if not self.client or not resources:
            return None

        from google.genai import types

        payload = {
            "project": settings.PROJECT_ID,
            "observation_window_hours": settings.METRICS_LOOKBACK_HOURS,
            "resources": [_facts(r) for r in resources],
        }

        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=(
                    "Analyse these measured resources and return one analysis per "
                    "resource_id:\n" + json.dumps(payload, indent=2)
                ),
                config=types.GenerateContentConfig(
                    system_instruction=ANALYST_INSTRUCTION,
                    temperature=0.1,
                    response_mime_type="application/json",
                    response_schema=ANALYSIS_SCHEMA,
                    http_options=types.HttpOptions(timeout=90_000),
                ),
            )
            data = json.loads(response.text or "{}")
        except Exception as exc:
            logger.warning("Fleet analysis unavailable: %s: %s", type(exc).__name__, str(exc)[:160])
            return None

        by_id = {a["resource_id"]: a for a in data.get("analyses", []) if a.get("resource_id")}
        logger.info("LLM analysed %d/%d resource(s)", len(by_id), len(resources))
        return {"by_resource": by_id, "summary": data.get("fleet_summary", ""), "model": self.model}
