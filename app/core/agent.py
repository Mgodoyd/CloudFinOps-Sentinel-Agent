"""The CloudFinOps Sentinel agent.

Wraps Gemini with the remediation toolset and drives one audit pass. The agent
is deliberately defensive: a missing API key, an unavailable model, or a tool
error degrades into a readable result instead of a 500.
"""

import json
import logging
import re
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core import telemetry
from app.core.trace import ANALYSIS, APPROVAL, DECISION, PLANNING, tracer
from app.core.analyst import (
    FleetAnalyst,
    clear_analysis,
    last_analysis,
    store_analysis,
    summarise_fleet,
)
from app.core.executor import PlanExecutor
from app.core.planner import MAX_REPLANS, Planner
from app.core.prompts import AUDIT_PROMPT_TEMPLATE, SYSTEM_INSTRUCTION
from app.tools import rationale
from app.tools.gcp_metrics import describe_resources, get_infrastructure_anomalies
from app.tools.gcp_remediator import (
    delete_orphan_disk,
    purge_untagged_image,
    request_human_approval,
    resize_cloud_run,
)
from app.tools.memory_tools import check_remediation_history, memory_bank

logger = logging.getLogger(__name__)

# Tried in order when the configured model is not available to this API key.
# Tried in order when the configured model 404s. Newer first, then the one
# verified to work, so a key with broader access gets the better model while
# everyone else still lands somewhere real.
MODEL_FALLBACKS = [
    "gemini-3.5-flash-lite",   # highest free-tier headroom, ~2s responses
    "gemini-3.1-flash-lite",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
]

# Each automatic function call is a separate API request. The Gemini free tier
# allows only 5 requests/minute, so a chatty audit exhausts it mid-run. Keep
# this low unless you are on a paid tier (override with MAX_TOOL_CALLS).
MAX_TOOL_CALLS = settings.MAX_TOOL_CALLS

# Give up rather than let the SDK retry a dead request for minutes.
REQUEST_TIMEOUT_MS = 90_000

# How many models to try when one is at capacity. Each attempt is a billed
# request, and the free tier allows only 5 per minute — walking the whole chain
# would spend a minute's budget on a single congested audit.
MAX_OVERLOAD_ATTEMPTS = 2


class QuotaExceeded(RuntimeError):
    """The Gemini quota is exhausted; the caller should degrade, not fail."""


def _is_quota_error(exc: Exception) -> bool:
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text or "quota" in text.lower()


def _is_overloaded(exc: Exception) -> bool:
    """Google is at capacity for this model right now — a sibling may not be."""
    text = str(exc)
    return "503" in text or "UNAVAILABLE" in text or "high demand" in text.lower()


def _quota_hint(error_text: str) -> str:
    """Turn a raw 429 into something the operator can act on."""
    retry = ""
    match = re.search(r"retryDelay['\"]?[:\s]+['\"]?(\d+)s", error_text)
    if match:
        retry = f" Retry in ~{match.group(1)}s."
    return (
        "Gemini quota exhausted; the audit completed using the deterministic "
        f"heuristic instead.{retry} The free tier allows 5 requests/minute and "
        "each tool call is one request - lower MAX_TOOL_CALLS or move to a paid tier."
    )


def _outage_hint(exc: Exception) -> str:
    """Explain a non-quota LLM failure without leaking a stack trace."""
    kind = type(exc).__name__
    detail = str(exc)[:160]
    if "503" in detail or "UNAVAILABLE" in detail or "overloaded" in detail.lower():
        cause = "Gemini is temporarily overloaded"
    elif "timeout" in detail.lower() or "deadline" in detail.lower():
        cause = "the Gemini request timed out"
    else:
        cause = f"the Gemini call failed ({kind})"
    return (
        f"{cause}; the audit completed using the deterministic heuristic instead. "
        "Findings and the autonomy matrix are unaffected — only the natural-language "
        "reasoning was skipped. Re-run to get the model's analysis."
    )


def _shape_for(resource: Dict[str, Any], verdict: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The shape a ticket for this resource should carry.

    The model's targets first, then the deterministic sizing, then the shape
    the resource already has — see `rationale.merge_target_shape`.
    """
    recommended: Dict[str, Any] = {}
    try:
        recommended = rationale.recommend_sizing(resource)["target"]
    except (KeyError, TypeError, ValueError):
        pass
    return rationale.merge_target_shape(
        {
            "memory": (verdict or {}).get("target_memory"),
            "cpu": (verdict or {}).get("target_cpu"),
            "min_instances": (verdict or {}).get("target_min_instances"),
        },
        recommended,
        {
            "memory": resource.get("memory_limit"),
            "cpu": resource.get("cpu_limit"),
            "min_instances": resource.get("min_instances"),
        },
    )


class CloudFinOpsAgent:
    def __init__(self) -> None:
        self.client = None
        self.backend = "none"
        self.model_name = settings.GEMINI_MODEL
        self.tools = [
            resize_cloud_run,
            delete_orphan_disk,
            request_human_approval,
            purge_untagged_image,
            check_remediation_history,
        ]
        self._init_client()

    def _init_client(self) -> None:
        """Reach Gemini via Vertex AI (service account) or an AI Studio key."""
        try:
            from google import genai

            if settings.USE_VERTEX:
                # Uses Application Default Credentials - no API key needed.
                self.client = genai.Client(
                    vertexai=True, project=settings.PROJECT_ID, location=settings.REGION
                )
                self.backend = "vertex"
                logger.info(
                    "Gemini client initialised via Vertex AI (%s/%s) with model %s",
                    settings.PROJECT_ID, settings.REGION, self.model_name,
                )
            elif settings.GEMINI_API_KEY:
                self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
                self.backend = "api_key"
                logger.info("Gemini client initialised with model %s", self.model_name)
            else:
                logger.warning(
                    "No Gemini credentials (set GEMINI_API_KEY or USE_VERTEX=true) - "
                    "running in heuristic mode."
                )
        except Exception as exc:
            logger.warning("Failed to initialise Gemini client: %s", exc)
            self.client = None

    @property
    def is_live(self) -> bool:
        return self.client is not None

    # ------------------------------------------------------------------
    def _build_prompt(self, data: Dict[str, Any]) -> str:
        resources, _ = describe_resources()
        remediated = [r["resource_id"] for r in memory_bank.snapshot()["remediations"]]
        return AUDIT_PROMPT_TEMPLATE.format(
            project_id=data.get("project_id", settings.PROJECT_ID),
            regions=", ".join(data.get("regions_scanned", [settings.REGION])),
            data_source=data.get("data_source", "simulated"),
            resource_count=len(resources),
            remediated=", ".join(sorted(set(remediated))) or "none",
            anomalies=json.dumps(data.get("idle_services", []), indent=2),
            images=json.dumps(data.get("untagged_images", []), indent=2),
            disks=json.dumps(data.get("orphan_disks", []), indent=2),
            addresses=json.dumps(data.get("unused_addresses", []), indent=2),
            problems=json.dumps(data.get("problems", []), indent=2) or "none",
        )

    def _generate(self, prompt: str) -> str:
        from google import genai
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=settings.GEMINI_TEMPERATURE,
            tools=self.tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                maximum_remote_calls=MAX_TOOL_CALLS
            ),
            http_options=types.HttpOptions(timeout=REQUEST_TIMEOUT_MS),
        )

        candidates = [self.model_name] + [m for m in MODEL_FALLBACKS if m != self.model_name]
        last_error: Optional[Exception] = None
        overload_attempts = 0

        for model in candidates:
            try:
                response = self.client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                if model != self.model_name:
                    logger.info("Model %s unavailable; succeeded with %s", self.model_name, model)
                    self.model_name = model
                # response.text is empty when the turn ended on a function_call,
                # e.g. AFC hit its call budget before writing the final report.
                return (response.text or "").strip()
            except Exception as exc:
                last_error = exc
                if _is_quota_error(exc):
                    # Retrying another model burns the same shared quota.
                    raise QuotaExceeded(str(exc)) from exc
                if "NOT_FOUND" in str(exc) or "404" in str(exc):
                    logger.info("Model %s unavailable to this key, trying the next", model)
                    continue
                if _is_overloaded(exc):
                    # Capacity is per-model, so one sibling is worth trying —
                    # but not the whole chain, or a congested minute costs the
                    # entire free-tier request budget.
                    overload_attempts += 1
                    if overload_attempts >= MAX_OVERLOAD_ATTEMPTS:
                        logger.info("%d model(s) at capacity; degrading", overload_attempts)
                        raise
                    logger.info("Model %s is at capacity, trying one alternative", model)
                    continue
                raise
        raise RuntimeError(
            f"None of {candidates} is available to this API key. "
            f"Last error: {last_error}"
        )

    # ------------------------------------------------------------------
    def _reconcile_open_anomalies(self, analysis: Optional[Dict[str, Any]]) -> int:
        """Guarantee that every open anomaly has something a human can act on.

        The plan decides what to do; it may reasonably choose to skip. But an
        anomaly the dashboard paints red with no ticket behind it is a dead end
        for the operator, so anything still wasteful and still unhandled is
        escalated here, worded from the model's own analysis.
        """
        from app.main import build_full_inventory

        by_resource = (analysis or {}).get("by_resource", {})
        raised = 0

        try:
            inventory = build_full_inventory(allow_discovery=False)
        except Exception:
            return 0

        for res in inventory:
            if res["status"] in ("Healthy", "Tolerated"):
                continue
            rid = res["resource_id"]
            if memory_bank.has_pending_approval(rid):
                continue
            if memory_bank.last_rejection(rid):
                continue  # a human already said no
            history = memory_bank.check_history(rid)
            if history.get("found") and history.get("applied", True):
                continue  # really remediated; the next scan will re-evaluate

            verdict = by_resource.get(rid, {})
            kind = res.get("type", "Cloud Run")
            action_type = {
                "Persistent Disk": "delete_disk",
                "Static IP": "release_address",
                "Container Image": "delete_image",
            }.get(kind, "resize_service")
            action_key = {
                "delete_disk": "act.delete_disk",
                "release_address": "act.release_ip",
                "delete_image": "act.purge_image",
            }.get(action_type, "act.right_size")

            # A shape may not be resolvable for a malformed record. The ticket
            # is still raised — an open anomaly with no ticket is a dead end for
            # the operator — and execution refuses it rather than guessing.
            shape = _shape_for(res, verdict) if action_type == "resize_service" else None
            if action_type == "resize_service" and shape is None:
                logger.warning("No target shape for %s; ticket needs manual action", rid)

            request_human_approval(
                resource_id=rid,
                proposed_action=(
                    f"Resize to {rationale.describe_shape(shape)}" if shape
                    else verdict.get("recommendation")
                    or f"Resolve {res['status'].lower()} {kind.lower()}"
                ),
                estimated_roi=res["wasted_cost"],
                resource_url=res.get("url", ""),
                detailed_reason=verdict.get("diagnosis")
                or f"{res['status']}: ${res['wasted_cost']:.2f}/mo recoverable.",
                severity=res.get("severity", "MEDIUM"),
                target_memory=(shape or {}).get("memory", ""),
                action_key=action_key,
                action_type=action_type,
                action_params={k: v for k, v in {
                    "zone": res.get("zone") or res.get("location"),
                    "region": res.get("region"),
                    "cpu": (shape or {}).get("cpu"),
                    "min_instances": (shape or {}).get("min_instances"),
                }.items() if v is not None},
                target_shape=shape,
                model_recommendation=verdict.get("recommendation", ""),
            )
            raised += 1

        if raised:
            tracer.step(
                DECISION,
                f"{raised} open anomal{'y' if raised == 1 else 'ies'} had no ticket — escalated",
                status="warn",
                detail={"reason": "every anomaly must be actionable"},
            )
        return raised

    def _summarise_with_gemma(
        self, fleet: List[Dict[str, Any]]
    ) -> Optional[str]:
        """Second tier: Gemma writes the fleet summary when Gemini will not.

        Gemini going down is usually quota or capacity, and both are per-model,
        so a different model is a real second chance rather than a retry of the
        same failure. Gemma is served by the same API and the same SDK — this is
        a model name, not a second integration.

        It is deliberately given the *narrow* job. Measured against this fleet,
        Gemma cannot return the analyst's per-resource schema inside a usable
        deadline, but summarises the same estate in around twenty seconds. So
        the per-resource judgement degrades to the deterministic rules, which
        were always going to run anyway, and what the second tier buys back is
        the narrative the report would otherwise lose entirely.
        """
        if not self.client or not settings.GEMMA_MODEL:
            return None

        with telemetry.span(
            "agent.summarise.fallback",
            **{"gen_ai.system": "gemma", "gen_ai.request.model": settings.GEMMA_MODEL,
               "agent.resources": len(fleet)},
        ), tracer.timed(
            ANALYSIS, f"Gemini unavailable — {settings.GEMMA_MODEL} summarising the fleet"
        ) as step:
            step.add(request={"model": settings.GEMMA_MODEL, "resources": len(fleet),
                              "scope": "fleet summary only",
                              "reason": "primary model returned no analysis"})
            summary = summarise_fleet(self.client, settings.GEMMA_MODEL, fleet)
            step.add(response={"available": summary is not None,
                               "summary": summary})

        if summary is None:
            tracer.step(ANALYSIS, "Gemma unavailable too — deterministic rules only",
                        status="warn")
        return summary

    def _plan_and_execute(
        self, data: Dict[str, Any], analysis: Dict[str, Any], fleet: List[Dict[str, Any]]
    ) -> Optional[str]:
        """Ask the model for a plan, carry it out, and re-plan on failure.

        Returns None if planning is unavailable, so the caller can fall back to
        acting directly on the analysis.
        """
        # Whichever model produced the analysis also plans from it. If Gemini
        # was down and Gemma answered, planning with Gemini would fail on the
        # same outage and cost the run its plan.
        planner = Planner(self.client, analysis.get("model") or self.model_name)
        handled = [r["resource_id"] for r in memory_bank.snapshot()["remediations"]]

        with telemetry.span(
            "agent.plan",
            **{"gen_ai.system": "gemini", "gen_ai.request.model": self.model_name,
               "agent.resources": len(fleet)},
        ), tracer.timed(PLANNING, "LLM building an execution plan") as step:
            step.add(request={"model": self.model_name, "resources": len(fleet)})
            plan = planner.plan(analysis, fleet, handled)
            step.add(response={"steps": len(plan["steps"]) if plan else 0,
                               "available": plan is not None})

        if not plan:
            tracer.step(PLANNING, "Planning unavailable — acting on the analysis directly",
                        status="warn")
            return None

        executor = PlanExecutor(analysis, fleet)
        results, failures = executor.run(plan)
        replans = 0

        # Adapting after a failure is what makes this a plan rather than a list.
        while failures and replans < MAX_REPLANS:
            replans += 1
            completed = [r["resource_id"] for r in results if r["status"] != "failed"]
            tracer.step(
                PLANNING, f"{len(failures)} step(s) failed — asking the model to re-plan",
                status="warn", detail={"failures": failures, "attempt": replans},
            )
            revised = planner.replan(plan, failures, completed)
            if not revised or not revised.get("steps"):
                break
            results, failures = executor.run(revised)
            plan = revised

        return executor.report(plan, replans)

    def _act_on_analysis(self, data: Dict[str, Any], analysis: Dict[str, Any]) -> str:
        """Apply the model's recommendations through the autonomy matrix.

        The model decides *what* should change; this decides *whether it may
        happen without a human*. Keeping enforcement in code means a persuasive
        model still cannot execute a Level 2 action on its own.
        """
        by_id = analysis["by_resource"]
        lines: List[str] = [analysis["summary"].strip(), ""]
        acted = 0

        for anomaly in data.get("idle_services", []):
            rid = anomaly["resource_id"]
            verdict = by_id.get(rid)
            if verdict and verdict["verdict"] == "acceptable":
                lines.append(f"- `{rid}` — the model found the allocation acceptable; no action.")
                continue
            if memory_bank.check_history(rid).get("found"):
                lines.append(f"- `{rid}` — already remediated in an earlier scan.")
                continue

            # The cost model measured this; the model's `monthly_saving` is a
            # guess and only stands in when there is no measurement.
            savings = float(anomaly.get("potential_savings")
                            or (verdict or {}).get("monthly_saving") or 0.0)
            result = self._dispatch(rid, anomaly, verdict, savings)
            lines.append(result)
            acted += 1

        for disk in data.get("orphan_disks", []):
            cost = float(disk.get("monthly_cost", 0.0))
            delete_orphan_disk(disk["resource_id"], estimated_savings=cost,
                               zone=disk.get("zone", ""))
            lines.append(f"- `{disk['resource_id']}` — orphaned disk escalated (${cost:.2f}/mo).")

        for addr in data.get("unused_addresses", []):
            cost = float(addr.get("monthly_cost", 7.20))
            request_human_approval(
                resource_id=addr["resource_id"],
                proposed_action="Release unused static IP",
                estimated_roi=cost,
                detailed_reason=(by_id.get(addr["resource_id"], {}).get("diagnosis")
                                 or f"Static IP {addr.get('address', '')} is reserved but unused."),
                severity="MEDIUM",
                action_key="act.release_ip",
                reason_key="reason.irreversible",
                action_type="release_address",
                action_params={"region": addr.get("region", "")},
            )
            lines.append(f"- `{addr['resource_id']}` — unused static IP escalated (${cost:.2f}/mo).")

        for image in data.get("untagged_images", []):
            cost = float(image.get("monthly_cost", 0.10))
            purge_untagged_image(image["resource_id"], estimated_savings=cost)

        minor = data.get("below_threshold", [])
        if minor:
            names = ", ".join(f"`{m['resource_id']}`" for m in minor[:5])
            lines.append(
                f"- _{len(minor)} finding(s) below the ${settings.MIN_SAVINGS_THRESHOLD:.2f}/mo "
                f"action threshold ({names}) — reported, not actioned._"
            )
        for problem in data.get("problems", []):
            lines.append(f"- _{problem['source']}: {problem['detail']}_")

        total = sum(float(v.get("monthly_saving") or 0) for v in by_id.values())
        lines += ["", f"**Estimated monthly savings:** ${total:.2f}",
                  "", f"_Analysis by {analysis['model']}; the autonomy matrix was applied by the agent._"]
        return "\n".join(lines)

    def _dispatch(self, rid, anomaly, verdict, savings) -> str:
        """Route one recommendation through Level 1 / Level 2."""
        reason = (verdict or {}).get("diagnosis") or anomaly.get("issue", "")
        recommendation = (verdict or {}).get("recommendation") or (
            "Right-size allocation to match observed usage"
        )
        # The model names only what it cared about; the rest is resolved from
        # the deterministic sizing rather than from a constant.
        sizing = (anomaly.get("rationale") or {}).get("sizing") or {}
        shape = rationale.merge_target_shape(
            {
                "memory": (verdict or {}).get("target_memory"),
                "cpu": (verdict or {}).get("target_cpu"),
                "min_instances": (verdict or {}).get("target_min_instances"),
            },
            sizing.get("target"),
            sizing.get("current"),
        )
        target_memory = (shape or {}).get("memory", "")

        if savings >= settings.HIGH_RISK_ROI_THRESHOLD or anomaly["severity"] == "HIGH":
            result = request_human_approval(
                resource_id=rid,
                proposed_action=(
                    f"Resize to {rationale.describe_shape(shape)}" if shape
                    else recommendation
                ),
                estimated_roi=savings,
                resource_url=anomaly.get("resource_url", ""),
                detailed_reason=reason,
                severity=anomaly.get("severity", "HIGH"),
                target_memory=target_memory,
                action_key="act.right_size",
                action_type="resize_service",
                action_params={k: v for k, v in {
                    "cpu": (shape or {}).get("cpu"),
                    "min_instances": (shape or {}).get("min_instances"),
                }.items() if v is not None},
                target_shape=shape,
                model_recommendation=(verdict or {}).get("recommendation", ""),
                rationale=anomaly.get("rationale"),
            )
            # Report what actually happened, not what was attempted: the call
            # may have been skipped as a duplicate or a prior rejection.
            if result.startswith("SKIPPED"):
                return f"- `{rid}` — {result[9:]}"
            return f"- `{rid}` — escalated for approval (${savings:.2f}/mo): {recommendation}"

        if shape is None:
            return f"- `{rid}` — no target shape could be established; nothing applied."
        result = resize_cloud_run(
            rid, shape["memory"], estimated_savings=savings,
            new_cpu=shape["cpu"], new_min_instances=shape["min_instances"],
        )
        if result.startswith(("SKIPPED", "FAILED")):
            return f"- `{rid}` — {result}"
        return (f"- `{rid}` — resized to {rationale.describe_shape(shape)} "
                f"(${savings:.2f}/mo).")

    def _heuristic_audit(self, data: Dict[str, Any], reason: str = "") -> str:
        """Deterministic fallback that still applies the full autonomy matrix.

        Runs when no model is configured, or when the configured one is
        unreachable. `reason` keeps the report honest about which it was.
        """
        why = reason or (
            "Gemini unavailable" if self.is_live else "no Gemini credentials configured"
        )
        lines: List[str] = [f"**Heuristic audit** ({why})", ""]
        total = 0.0

        for anomaly in data.get("idle_services", []):
            rid = anomaly["resource_id"]
            savings = float(anomaly.get("potential_savings", 0.0))
            if memory_bank.check_history(rid).get("found"):
                lines.append(f"- `{rid}` — skipped, already remediated.")
                continue
            why = anomaly.get("rationale") or {}
            sizing = why.get("sizing") or {}
            rule = why.get("rule") or {}
            llm = last_analysis()["by_resource"].get(rid) or {}
            shape = rationale.merge_target_shape(
                {
                    "memory": llm.get("target_memory"),
                    "cpu": llm.get("target_cpu"),
                    "min_instances": llm.get("target_min_instances"),
                },
                sizing.get("target"),
                sizing.get("current"),
            )
            if shape is None:
                lines.append(f"- `{rid}` — no target shape could be established; skipped.")
                continue

            if savings >= settings.HIGH_RISK_ROI_THRESHOLD or anomaly["severity"] == "HIGH":
                request_human_approval(
                    resource_id=rid,
                    proposed_action=f"Resize to {rationale.describe_shape(shape)}",
                    action_key="act.right_size",
                    change_specs=sizing.get("change_specs") or [],
                    reason_key=f"{rule['key']}.why" if rule.get("key") else "",
                    reason_params=rule.get("params") or {},
                    estimated_roi=savings,
                    resource_url=anomaly.get("resource_url", ""),
                    detailed_reason=anomaly.get("issue", ""),
                    severity=anomaly.get("severity", "HIGH"),
                    target_memory=shape["memory"],
                    action_params={"cpu": shape["cpu"],
                                   "min_instances": shape["min_instances"]},
                    target_shape=shape,
                    model_recommendation=llm.get("recommendation", ""),
                    rationale=why,
                )
                lines.append(f"- `{rid}` — escalated for approval (${savings:.2f}/mo).")
            else:
                resize_cloud_run(
                    rid, shape["memory"], estimated_savings=savings,
                    new_cpu=shape["cpu"], new_min_instances=shape["min_instances"],
                )
                lines.append(f"- `{rid}` — resized to "
                             f"{rationale.describe_shape(shape)} (${savings:.2f}/mo).")
            total += savings

        for image in data.get("untagged_images", []):
            cost = float(image.get("monthly_cost", 0.10))
            purge_untagged_image(image["resource_id"], estimated_savings=cost)
            total += cost

        for disk in data.get("orphan_disks", []):
            cost = float(disk.get("monthly_cost", 0.0))
            delete_orphan_disk(disk["resource_id"], estimated_savings=cost)
            lines.append(f"- `{disk['resource_id']}` — orphaned disk escalated (${cost:.2f}/mo).")
            total += cost

        for addr in data.get("unused_addresses", []):
            cost = float(addr.get("monthly_cost", 7.20))
            request_human_approval(
                resource_id=addr["resource_id"],
                proposed_action="Release unused static IP",
                estimated_roi=cost,
                detailed_reason=f"Static IP {addr.get('address', '')} is reserved but not in use.",
                severity="MEDIUM",
                action_key="act.release_ip",
                reason_key="reason.irreversible",
                action_type="release_address",
                action_params={"region": addr.get("region", "")},
            )
            lines.append(f"- `{addr['resource_id']}` — unused static IP escalated (${cost:.2f}/mo).")
            total += cost

        minor = data.get("below_threshold", [])
        if minor:
            names = ", ".join(f"`{m['resource_id']}`" for m in minor[:5])
            lines.append(
                f"- _{len(minor)} finding(s) below the "
                f"${settings.MIN_SAVINGS_THRESHOLD:.2f}/mo action threshold "
                f"({names}) — reported, not actioned._"
            )

        for problem in data.get("problems", []):
            lines.append(f"- _{problem['source']}: {problem['detail']}_")

        lines += ["", f"**Estimated monthly savings:** ${total:.2f}"]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    @staticmethod
    def _summarise_from_ledger(before: Dict[str, int], anomalies: int) -> str:
        """Describe what the run actually did, from the memory bank.

        Used when the model ends its turn on a tool call and never writes a
        closing report. Reporting the real ledger beats a generic placeholder.
        """
        store = memory_bank.snapshot()
        applied = store["remediations"][before["remediations"]:]
        escalated = store["approvals"][before["approvals"]:]

        lines = [f"**Findings** — {anomalies} anomal{'y' if anomalies == 1 else 'ies'} in scope.", ""]

        if applied:
            lines.append("**Actions taken**")
            for r in applied:
                note = "" if r.get("applied", True) else " *(dry run)*"
                lines.append(
                    f"- `{r['resource_id']}` — {r['action']} (+${r['savings']:.2f}/mo){note}"
                )
            lines.append("")

        if escalated:
            lines.append("**Awaiting approval**")
            for a in escalated:
                lines.append(
                    f"- `{a['resource_id']}` — {a['proposed_action']} "
                    f"(+${a['estimated_roi']:.2f}/mo, {a.get('severity', 'HIGH')})"
                )
            lines.append("")

        if not applied and not escalated:
            lines.append("No new action required; everything in scope was already handled.")
            lines.append("")

        total = sum(r["savings"] for r in applied) + sum(a["estimated_roi"] for a in escalated)
        lines.append(f"**Estimated monthly savings:** ${total:.2f}")
        lines.append("")
        lines.append(
            "_Report reconstructed from the action ledger — the model reached its "
            "tool-call budget before writing a closing summary._"
        )
        return "\n".join(lines)

    def audit_infrastructure(self, data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Run one full audit pass. Always returns a result dict, never raises."""
        run = memory_bank.start_run()
        run_id = run["run_id"]
        tracer.set_run(run_id)

        if data is None:
            # An explicit audit always re-queries GCP; serving the cache here
            # made every re-scan look identical.
            data = get_infrastructure_anomalies(force_refresh=True)

        # The model does the judgement; the deterministic layer only measured.
        # One call for the whole fleet keeps it inside a free-tier minute.
        clear_analysis()
        analysis: Optional[Dict[str, Any]] = None
        if self.is_live:
            from app.main import build_full_inventory  # local import: avoids a cycle

            try:
                fleet = build_full_inventory()
            except Exception:
                fleet = describe_resources()[0]
            with telemetry.span(
                "agent.analyse",
                **{"gen_ai.system": "gemini", "gen_ai.request.model": self.model_name,
                   "agent.resources": len(fleet), "agent.run_id": run_id},
            ), tracer.timed(ANALYSIS, f"LLM analysing {len(fleet)} resource(s)") as step:
                step.add(request={"model": self.model_name, "resources": len(fleet)})
                analysis = FleetAnalyst(self.client, self.model_name).analyse(fleet)
                step.add(response={
                    "analysed": len(analysis["by_resource"]) if analysis else 0,
                    "available": analysis is not None,
                })

            if analysis:
                store_analysis(analysis)
                tracer.step(
                    ANALYSIS, f"Fleet verdict from {analysis['model']}",
                    detail={"summary": analysis["summary"],
                            "per_resource": analysis["by_resource"]},
                )
            else:
                # `analysis` stays None on purpose: an empty by_resource must
                # not send the run down the planning path, which would spend a
                # second timeout discovering there is nothing to plan from.
                gemma_summary = self._summarise_with_gemma(fleet)
                if gemma_summary:
                    store_analysis({"by_resource": {}, "summary": gemma_summary,
                                    "model": settings.GEMMA_MODEL})
                    tracer.step(
                        ANALYSIS, f"Fleet summary from {settings.GEMMA_MODEL}",
                        detail={"summary": gemma_summary,
                                "per_resource": "deterministic rules"},
                    )
                else:
                    tracer.step(
                        ANALYSIS, "LLM analysis unavailable — deterministic rules only",
                        status="warn",
                    )

        anomaly_count = len(data.get("idle_services", []))

        # A compact fingerprint of what this scan saw, so the history can show
        # what changed rather than leaving the operator to compare by eye.
        # Named distinctly: `snapshot` is already the memory-bank dump below.
        resource_snapshot = {
            r["resource_id"]: {
                "status": r["status"],
                "cost": r["monthly_cost"],
                "waste": r["wasted_cost"],
                "cpu": r["cpu_utilization"],
                "memory": r["memory_utilization"],
            }
            for r in describe_resources()[0]
        }
        # Disks, IPs and images are part of the estate too: a newly orphaned
        # disk must register as a change, not go unnoticed.
        for kind, status in (
            ("orphan_disks", "Orphaned"),
            ("unused_addresses", "Unused"),
            ("untagged_images", "Untagged"),
        ):
            for item in data.get(kind, []):
                resource_snapshot[item.get("short_id") or item["resource_id"]] = {
                    "status": status,
                    "cost": item.get("monthly_cost", 0.0),
                    "waste": item.get("monthly_cost", 0.0),
                    "cpu": 0.0,
                    "memory": 0.0,
                }
        memory_bank.log_event(
            key="ev.audit_started", run_id=run_id, count=anomaly_count, level="INFO"
        )
        _before_snapshot = memory_bank.snapshot()
        ledger_before = {
            "remediations": len(_before_snapshot["remediations"]),
            "approvals": len(_before_snapshot["approvals"]),
        }
        actions_before = ledger_before["remediations"] + ledger_before["approvals"]

        degraded: Optional[str] = None
        try:
            telemetry.annotate(**{"agent.run_id": run_id, "agent.anomalies": anomaly_count})
            if self.is_live:
                try:
                    if analysis:
                        # goal -> plan -> execute -> adapt. The model plans;
                        # the executor enforces the autonomy matrix.
                        planned = self._plan_and_execute(data, analysis, fleet)
                        if planned is not None:
                            summary, mode = planned, "planned"
                        else:
                            summary = self._act_on_analysis(data, analysis)
                            mode = "llm"
                    else:
                        summary = self._generate(self._build_prompt(data))
                        mode = "gemini"
                        if not summary:
                            summary = self._summarise_from_ledger(ledger_before, anomaly_count)
                except Exception as exc:
                    # The LLM is the optional part. Quota limits, 5xx outages and
                    # timeouts are all transient, and this agent runs unattended
                    # on a schedule - losing the whole audit over one of them is
                    # worse than finishing it deterministically.
                    degraded = (
                        _quota_hint(str(exc))
                        if isinstance(exc, QuotaExceeded)
                        else _outage_hint(exc)
                    )
                    logger.warning("Gemini unavailable (%s); falling back to heuristic audit",
                                   type(exc).__name__)
                    memory_bank.log_event(
                        key="ev.llm_unavailable", error=type(exc).__name__, level="WARN"
                    )
                    summary = self._heuristic_audit(
                        data,
                        reason="Gemini quota exhausted"
                        if isinstance(exc, QuotaExceeded)
                        else f"Gemini unreachable: {type(exc).__name__}",
                    )
                    mode = "heuristic-fallback"
            else:
                summary = self._heuristic_audit(data)
                mode = "heuristic"

            # Structural guarantee, independent of what the plan chose to do.
            self._reconcile_open_anomalies(analysis)

            # Anything still waiting that nobody has been told about. Creation
            # is guarded against duplicates, so a ticket raised before a channel
            # was configured would otherwise stay pending and silent forever.
            try:
                from app.tools import notifications

                announced = notifications.announce_pending()
                if announced:
                    tracer.step(
                        APPROVAL,
                        f"{announced} pending ticket(s) had never been announced — sent now",
                        detail={"count": announced},
                    )
            except Exception as exc:
                logger.warning("Could not announce pending approvals: %s", exc)

            snapshot = memory_bank.snapshot()
            actions = len(snapshot["remediations"]) + len(snapshot["approvals"]) - actions_before

            memory_bank.finish_run(
                run_id,
                status="SUCCESS",
                anomalies_found=anomaly_count,
                actions_taken=max(0, actions),
                summary=summary,
                snapshot=resource_snapshot,
                mode=mode,
                degraded=degraded,
            )
            memory_bank.log_event(
                key="ev.audit_finished", run_id=run_id, count=max(0, actions), level="INFO"
            )
            return {
                "status": "success",
                "run_id": run_id,
                "mode": mode,
                "model": self.model_name if self.is_live else None,
                "anomalies_found": anomaly_count,
                "actions_taken": max(0, actions),
                "degraded": degraded,
                "response": summary,
            }

        except Exception as exc:
            logger.error("Agent execution failed: %s", exc, exc_info=True)
            memory_bank.finish_run(
                run_id, status="ERROR", anomalies_found=anomaly_count, error=str(exc)
            )
            memory_bank.log_event(
                key="ev.audit_failed", run_id=run_id, error=str(exc)[:120], level="WARN"
            )
            # Same shape as the success path - callers index these keys blindly.
            return {
                "status": "error",
                "run_id": run_id,
                "mode": "failed",
                "model": None,
                "anomalies_found": anomaly_count,
                "actions_taken": 0,
                "degraded": None,
                "response": "",
                "message": str(exc),
            }
