"""Explains *why* a resource was flagged and *what* to do about it.

Every recommendation the agent makes should be auditable: which numbers were
measured, where they came from, which rule they tripped, what the concrete fix
is, and why it did or did not need a human. This module turns a resource record
into that chain of reasoning.
"""

from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.core.i18n import DEFAULT_LANG, normalise, t

# Memory values Cloud Run actually accepts, ascending.
MEMORY_STEPS_MIB = [128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768]
# Cloud Run requires >= 1 vCPU for >= 4Gi, and >= 2 vCPU for >= 8Gi.
CPU_STEPS = [0.25, 0.5, 1.0, 2.0, 4.0, 8.0]

SAFETY_HEADROOM = 1.4  # keep 40% above observed peak
# Never cut more than this in a single step. A service with little traffic
# history shows artificially low peaks, and a 16x cut on that evidence is a
# guess, not a right-sizing. Repeated audits converge safely instead.
MAX_REDUCTION_FACTOR = 4
# Floor for any recommendation, regardless of observed usage.
MIN_RECOMMENDED_MIB = 256


def _fmt_memory(mib: int) -> str:
    return f"{mib // 1024}Gi" if mib >= 1024 and mib % 1024 == 0 else f"{mib}Mi"


def _fmt_cpu(cpu: float) -> str:
    return str(int(cpu)) if cpu >= 1 and cpu == int(cpu) else f"{int(cpu * 1000)}m"


def _min_cpu_for_memory(mib: int) -> float:
    if mib >= 8192:
        return 2.0
    if mib >= 4096:
        return 1.0
    return 0.25


def recommend_sizing(resource: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, Any]:
    """Propose a concrete new shape, sized from observed peaks plus headroom."""
    from app.tools.gcp_metrics import parse_cpu, parse_memory_gib

    current_mib = int(parse_memory_gib(resource["memory_limit"]) * 1024)
    current_cpu = parse_cpu(resource["cpu_limit"])

    mem_peak_mib = current_mib * (resource["memory_utilization"] / 100)
    cpu_peak = current_cpu * (resource["cpu_utilization"] / 100)

    floor_mib = max(MIN_RECOMMENDED_MIB, current_mib // MAX_REDUCTION_FACTOR)
    target_mib = next(
        (s for s in MEMORY_STEPS_MIB if s >= max(mem_peak_mib * SAFETY_HEADROOM, floor_mib)),
        current_mib,
    )
    target_mib = min(target_mib, current_mib)  # never recommend growing

    cpu_floor = max(current_cpu / MAX_REDUCTION_FACTOR, _min_cpu_for_memory(target_mib))
    target_cpu = next(
        (c for c in CPU_STEPS if c >= max(cpu_peak * SAFETY_HEADROOM, cpu_floor)),
        current_cpu,
    )
    target_cpu = min(target_cpu, current_cpu)

    min_instances = int(resource.get("min_instances") or 0)
    target_min = 0 if resource["status"] == "Idle" else min_instances

    specs = _change_specs(resource, target_cpu, target_mib, min_instances, target_min)

    return {
        "current": {
            "cpu": resource["cpu_limit"],
            "memory": resource["memory_limit"],
            "min_instances": min_instances,
        },
        "target": {
            "cpu": _fmt_cpu(target_cpu),
            "memory": _fmt_memory(target_mib),
            "min_instances": target_min,
        },
        "change_specs": specs,
        "changes": render_changes(specs, lang),
    }


def _change_specs(
    resource: Dict[str, Any], target_cpu: float, target_mib: int,
    min_instances: int, target_min: int,
) -> List[Dict[str, Any]]:
    """Structured description of each change, language-independent.

    Approval tickets persist these rather than a rendered sentence, so a ticket
    raised in one language still reads correctly in another.
    """
    from app.tools.gcp_metrics import parse_cpu, parse_memory_gib

    specs: List[Dict[str, Any]] = []
    if target_mib < int(parse_memory_gib(resource["memory_limit"]) * 1024):
        specs.append({"kind": "memory", "from": resource["memory_limit"],
                      "to": _fmt_memory(target_mib)})
    if target_cpu < parse_cpu(resource["cpu_limit"]):
        specs.append({"kind": "cpu", "from": resource["cpu_limit"], "to": _fmt_cpu(target_cpu)})
    if target_min < min_instances:
        specs.append({"kind": "min_instances", "from": min_instances, "to": target_min})
    return specs


def render_changes(specs: List[Dict[str, Any]], lang: str = DEFAULT_LANG) -> List[str]:
    """Turn change specs into readable text in the requested language."""
    return [
        t(lang, f"chg.{c['kind']}", from_=c["from"], to=c["to"])
        for c in specs or []
    ]


def gcloud_command(resource: Dict[str, Any], target: Dict[str, Any]) -> str:
    """The exact command an operator could run to apply the same change."""
    parts = [
        f"gcloud run services update {resource['resource_id']} \\",
        f"  --region={resource.get('region', settings.REGION)} \\",
        f"  --project={settings.PROJECT_ID} \\",
        f"  --memory={target['memory']} \\",
        f"  --cpu={target['cpu']} \\",
        f"  --min-instances={target['min_instances']}",
    ]
    return "\n".join(parts)


def assess_confidence(resource: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, str]:
    """How much the recommendation should be trusted.

    Peaks measured over a few minutes of a freshly deployed service are not
    evidence of steady-state demand.
    """
    from app.tools import gcp_monitoring

    if resource.get("metrics_source") != "monitoring":
        return {"level": "low", "reason": t(lang, "conf.modelled")}

    history = gcp_monitoring.fetch_utilization_history() or []
    if len(history) < 6:
        return {"level": "low", "reason": t(lang, "conf.sparse", n=len(history))}
    if len(history) < 18:
        return {"level": "medium", "reason": t(lang, "conf.partial", n=len(history))}
    return {
        "level": "high",
        "reason": t(lang, "conf.full", n=len(history), hours=settings.METRICS_LOOKBACK_HOURS),
    }


def build_evidence(resource: Dict[str, Any], lang: str = DEFAULT_LANG) -> List[Dict[str, str]]:
    """The measured facts behind the verdict, each with its provenance."""
    measured = resource.get("metrics_source") == "monitoring"
    source = t(lang, "src.monitoring" if measured else "src.modelled")
    min_instances = int(resource.get("min_instances") or 0)

    billing = (
        t(lang, "val.billed_always", count=min_instances)
        if min_instances
        else t(lang, "val.scale_to_zero")
    )
    hours = settings.METRICS_LOOKBACK_HOURS
    api = t(lang, "src.cloud_run")

    return [
        {"label": t(lang, "ev.allocated_cpu"), "value": f"{resource['cpu_limit']} vCPU", "source": api},
        {"label": t(lang, "ev.allocated_memory"), "value": resource["memory_limit"], "source": api},
        {"label": t(lang, "ev.billing_model"), "value": billing, "source": api},
        {"label": t(lang, "ev.cpu_peak", hours=hours),
         "value": f"{resource['cpu_utilization']}%", "source": source},
        {"label": t(lang, "ev.memory_peak", hours=hours),
         "value": f"{resource['memory_utilization']}%", "source": source},
        {"label": t(lang, "ev.monthly_cost"), "value": f"${resource['monthly_cost']:.2f}",
         "source": t(lang, "src.cost_model")},
        {"label": t(lang, "ev.waste"), "value": f"${resource['wasted_cost']:.2f}/mo",
         "source": t(lang, "src.cost_model")},
    ]


def rule_triggered(resource: Dict[str, Any], lang: str = DEFAULT_LANG) -> Optional[Dict[str, str]]:
    """Which detection rule fired, stated as the condition that was checked."""
    min_instances = int(resource.get("min_instances") or 0)
    cpu = resource["cpu_utilization"]
    mem = resource["memory_utilization"]

    # Nothing actionable: explain() returns a clean bill of health instead.
    if resource["status"] in ("Healthy", "Tolerated"):
        return None

    if resource["status"] == "Idle" and min_instances > 0:
        key = "rule.idle_always_on"
        params = {"min_instances": min_instances, "cpu": cpu}
        rule_id = "IDLE_ALWAYS_ON"
    elif resource["status"] == "Idle":
        key = "rule.idle_service"
        params = {"cpu": cpu, "mem": mem}
        rule_id = "IDLE_SERVICE"
    elif resource["status"] == "Oversized":
        key = "rule.oversized"
        params = {
            "threshold": settings.MEMORY_ANOMALY_THRESHOLD_GIB,
            "memory": resource["memory_limit"],
            "mem": mem,
        }
        rule_id = "OVERSIZED_ALLOCATION"
    else:
        return None

    return {
        "id": rule_id,
        # Key + params let an approval ticket re-render this in any language.
        "key": key,
        "params": params,
        "condition": t(lang, f"{key}.cond", **params),
        "observed": t(lang, f"{key}.obs", **params),
        "why_it_matters": t(lang, f"{key}.why", **params),
    }


def autonomy_decision(resource: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, str]:
    """Why this action is (or is not) allowed to run without a human."""
    savings = f"{resource['wasted_cost']:.2f}"

    if resource["wasted_cost"] < settings.MIN_SAVINGS_THRESHOLD:
        return {
            "level": t(lang, "auto.level.none"),
            "decision": t(lang, "auto.dec.reported"),
            "reason": t(lang, "auto.why.below_threshold", savings=savings,
                        threshold=f"{settings.MIN_SAVINGS_THRESHOLD:.2f}"),
        }
    if resource["wasted_cost"] >= settings.HIGH_RISK_ROI_THRESHOLD:
        return {
            "level": t(lang, "auto.level.2"),
            "decision": t(lang, "auto.dec.approval"),
            "reason": t(lang, "auto.why.high_value", savings=savings,
                        threshold=f"{settings.HIGH_RISK_ROI_THRESHOLD:.2f}"),
        }
    return {
        "level": t(lang, "auto.level.1"),
        "decision": t(lang, "auto.dec.auto"),
        "reason": t(lang, "auto.why.low_risk", savings=savings,
                    threshold=f"{settings.HIGH_RISK_ROI_THRESHOLD:.2f}"),
    }


def explain_orphan_disk(disk: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, Any]:
    """An unattached disk bills in full for storage nothing can read."""
    cost = disk["monthly_cost"]
    api = t(lang, "src.compute")
    zone = disk.get("zone", "")
    return {
        "resource_id": disk["resource_id"],
        "verdict": t(lang, "verdict.Orphaned"),
        "status": "Orphaned",
        "severity": "HIGH" if cost >= settings.HIGH_RISK_ROI_THRESHOLD else "MEDIUM",
        "evidence": [
            {"label": t(lang, "ev.type"), "value": disk.get("disk_type", "pd-standard"), "source": api},
            {"label": t(lang, "ev.size"), "value": f"{disk['size_gb']:.0f} GB", "source": api},
            {"label": t(lang, "ev.zone"), "value": zone, "source": api},
            {"label": t(lang, "ev.attached_to"), "value": t(lang, "val.nothing"), "source": api},
            {"label": t(lang, "ev.cost"), "value": f"${cost:.2f}", "source": t(lang, "src.cost_model")},
        ],
        "rule": {
            "id": "ORPHANED_DISK",
            "condition": t(lang, "rule.orphan_disk.cond"),
            "observed": t(lang, "rule.orphan_disk.obs"),
            "why_it_matters": t(lang, "rule.orphan_disk.why"),
        },
        "diagnosis": t(lang, "diag.orphan_disk"),
        "sizing": None,
        "solution": t(lang, "sol.delete_disk", id=disk["resource_id"]),
        "command": (
            f"gcloud compute snapshots create {disk['resource_id']}-final \\\n"
            f"  --source-disk={disk['resource_id']} --source-disk-zone={zone} \\\n"
            f"  --project={settings.PROJECT_ID}\n\n"
            f"gcloud compute disks delete {disk['resource_id']} \\\n"
            f"  --zone={zone} --project={settings.PROJECT_ID}"
        ),
        "expected_result": t(lang, "result.remove", savings=f"{cost:.2f}", yearly=f"{cost * 12:.2f}"),
        "autonomy": {
            "level": t(lang, "auto.level.2"),
            "decision": t(lang, "auto.dec.approval"),
            "reason": t(lang, "auto.why.irreversible_disk"),
        },
        "confidence": {"level": "high", "reason": t(lang, "conf.direct_read")},
        "savings": cost,
    }


def explain_unused_address(addr: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, Any]:
    """A reserved but unattached static IP bills hourly for doing nothing."""
    cost = addr["monthly_cost"]
    api = t(lang, "src.compute")
    return {
        "resource_id": addr["resource_id"],
        "verdict": t(lang, "verdict.Unused"),
        "status": "Unused",
        "severity": "MEDIUM",
        "evidence": [
            {"label": t(lang, "ev.address"), "value": addr.get("address", ""), "source": api},
            {"label": t(lang, "ev.region"), "value": addr.get("region", ""), "source": api},
            {"label": t(lang, "ev.status"), "value": t(lang, "val.reserved_unused"), "source": api},
            {"label": t(lang, "ev.cost"), "value": f"${cost:.2f}", "source": t(lang, "src.pricing")},
        ],
        "rule": {
            "id": "UNUSED_STATIC_IP",
            "condition": t(lang, "rule.unused_ip.cond"),
            "observed": t(lang, "rule.unused_ip.obs"),
            "why_it_matters": t(lang, "rule.unused_ip.why"),
        },
        "diagnosis": t(lang, "diag.unused_ip"),
        "sizing": None,
        "solution": t(lang, "sol.release_ip", id=addr["resource_id"]),
        "command": (
            f"gcloud compute addresses delete {addr['resource_id']} \\\n"
            f"  --region={addr.get('region', settings.REGION)} --project={settings.PROJECT_ID}"
        ),
        "expected_result": t(lang, "result.remove", savings=f"{cost:.2f}", yearly=f"{cost * 12:.2f}"),
        "autonomy": {
            "level": t(lang, "auto.level.2"),
            "decision": t(lang, "auto.dec.approval"),
            "reason": t(lang, "auto.why.irreversible_ip"),
        },
        "confidence": {"level": "high", "reason": t(lang, "conf.direct_read")},
        "savings": cost,
    }


def explain_untagged_image(image: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, Any]:
    cost = image.get("monthly_cost", 0.10)
    api = t(lang, "src.artifact")
    return {
        "resource_id": image.get("short_id") or image["resource_id"],
        "verdict": t(lang, "verdict.Untagged"),
        "status": "Untagged",
        "severity": "LOW",
        "evidence": [
            {"label": t(lang, "ev.repository"), "value": image.get("repository", ""), "source": api},
            {"label": t(lang, "ev.tags"), "value": t(lang, "val.none"), "source": api},
            {"label": t(lang, "ev.created"), "value": image.get("created", "")[:10], "source": api},
            {"label": t(lang, "ev.cost"), "value": f"${cost:.2f}", "source": t(lang, "src.cost_model")},
        ],
        "rule": {
            "id": "UNTAGGED_IMAGE",
            "condition": t(lang, "rule.untagged_image.cond"),
            "observed": t(lang, "rule.untagged_image.obs"),
            "why_it_matters": t(lang, "rule.untagged_image.why"),
        },
        "diagnosis": t(lang, "diag.untagged_image"),
        "sizing": None,
        "solution": t(lang, "sol.delete_image"),
        "command": f"gcloud artifacts versions delete {image['resource_id']} --quiet",
        "expected_result": t(lang, "result.remove", savings=f"{cost:.2f}", yearly=f"{cost * 12:.2f}"),
        "autonomy": {
            "level": t(lang, "auto.level.1"),
            "decision": t(lang, "auto.dec.auto"),
            "reason": t(lang, "auto.why.safe_reclaim"),
        },
        "confidence": {"level": "high", "reason": t(lang, "conf.tags_read")},
        "savings": cost,
    }


def explain(resource: Dict[str, Any], lang: str = DEFAULT_LANG) -> Dict[str, Any]:
    """Full rationale for one resource, or a clean bill of health."""
    lang = normalise(lang)
    kind = resource.get("type", "Cloud Run")
    if kind == "Persistent Disk":
        return explain_orphan_disk(resource, lang)
    if kind == "Static IP":
        return explain_unused_address(resource, lang)
    if kind == "Container Image":
        return explain_untagged_image(resource, lang)

    rule = rule_triggered(resource, lang)
    evidence = build_evidence(resource, lang)

    if rule is None:
        return {
            "resource_id": resource["resource_id"],
            "verdict": t(lang, f"verdict.{resource['status']}"),
            "status": resource["status"],
            "evidence": evidence,
            "rule": None,
            "diagnosis": t(
                lang,
                "diag.tolerated" if resource["status"] == "Tolerated" else "diag.healthy",
                threshold=f"{settings.MIN_SAVINGS_THRESHOLD:.2f}",
                waste=f"{resource.get('wasted_cost', 0.0):.2f}",
            ),
            "sizing": None,
            "command": None,
            "confidence": assess_confidence(resource, lang),
            "autonomy": {
                "level": t(lang, "auto.level.none"),
                "decision": t(lang, "auto.dec.noaction"),
                "reason": t(lang, "auto.why.nothing"),
            },
            "savings": 0.0,
        }

    sizing = recommend_sizing(resource, lang)
    changes = sizing["changes"]
    waste = resource["wasted_cost"]

    return {
        "resource_id": resource["resource_id"],
        "verdict": t(lang, f"verdict.{resource['status']}"),
        "status": resource["status"],
        "severity": resource["severity"],
        "evidence": evidence,
        "rule": rule,
        "diagnosis": rule["why_it_matters"],
        "sizing": sizing,
        "solution": t(lang, "sol.apply", changes=", ".join(changes)) if changes else t(lang, "sol.none"),
        "command": gcloud_command(resource, sizing["target"]) if changes else None,
        "expected_result": t(
            lang, "result.resize",
            before=f"{resource['monthly_cost']:.2f}",
            after=f"{max(0.0, resource['monthly_cost'] - waste):.2f}",
            savings=f"{waste:.2f}",
            yearly=f"{waste * 12:.2f}",
        ),
        "autonomy": autonomy_decision(resource, lang),
        "confidence": assess_confidence(resource, lang),
        "savings": waste,
        "capped": (
            t(lang, "cap.reduction", factor=MAX_REDUCTION_FACTOR)
            if changes and sizing["target"]["memory"] != _safe_floor_label(resource)
            else None
        ),
    }


def _safe_floor_label(resource: Dict[str, Any]) -> str:
    """The shape an unconstrained recommendation would have suggested."""
    from app.tools.gcp_metrics import parse_memory_gib

    current_mib = int(parse_memory_gib(resource["memory_limit"]) * 1024)
    peak_mib = current_mib * (resource["memory_utilization"] / 100)
    unconstrained = next(
        (s for s in MEMORY_STEPS_MIB if s >= peak_mib * SAFETY_HEADROOM), current_mib
    )
    return _fmt_memory(unconstrained)
