"""FastAPI entrypoint for CloudFinOps Sentinel."""

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.core.agent import CloudFinOpsAgent
from app.core.config import settings
from app.core.i18n import DEFAULT_LANG, normalise, t
from pydantic import BaseModel

from app.models.schemas import ApprovalRequest
from app.tools import gcp_metrics
from app.tools.gcp_metrics import (
    build_charts,
    describe_resources,
    fetch_services,
    fleet_utilization,
    has_scanned,
    last_problems,
    last_scan_age_seconds,
)
from app.tools.gcp_remediator import execute_approved_action
from app.core.analyst import last_analysis
from app.core.auth import (
    SESSION_COOKIE,
    ensure_token,
    auth_configured,
    describe as describe_auth,
    issue_session,
    require_operator,
    require_webhook,
    revoke_session,
    verify_login,
)
from app.core import telemetry
from app.core.trace import SYSTEM, tracer
from app.tools.preflight import run_preflight
from app.tools.rationale import explain
from app.tools.memory_tools import build_history, memory_bank, render_approval, render_event

class _TraceCorrelatedFormatter(logging.Formatter):
    """Cloud Logging groups a log line with its span when both ids are present."""

    def format(self, record: logging.LogRecord) -> str:
        ctx = telemetry.trace_context()
        payload = {
            "severity": record.levelname,
            "time": self.formatTime(record),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if ctx["trace_id"]:
            payload["logging.googleapis.com/trace"] = (
                f"projects/{settings.PROJECT_ID}/traces/{ctx['trace_id']}"
            )
            payload["logging.googleapis.com/spanId"] = ctx["span_id"]
        return json.dumps(payload)


_handler = logging.StreamHandler()
_handler.setFormatter(_TraceCorrelatedFormatter())
logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(__name__)

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
agent = CloudFinOpsAgent()

# One audit at a time — concurrent runs would double-remediate.
_audit_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "CloudFinOps Sentinel starting (project=%s region=%s mode=%s)",
        settings.PROJECT_ID,
        settings.REGION,
        "gemini" if agent.is_live else "heuristic",
    )
    memory_bank.log_event(key="ev.online", level="INFO")

    # The event loop is needed so worker threads can push trace steps to SSE.
    tracer.bind_loop(asyncio.get_running_loop())
    telemetry.init_telemetry(app)

    generated = ensure_token()
    if generated:
        logger.warning(
            "No DASHBOARD_TOKEN configured — generated one for this run only: %s",
            generated,
        )
    tracer.step(
        SYSTEM, "Sentinel online — waiting for an audit to be triggered",
        detail={"project": settings.PROJECT_ID, "regions": len(settings.regions),
                "dry_run": settings.DRY_RUN,
                "engine": agent.model_name if agent.is_live else "heuristic"},
    )
    # No scan happens here on purpose: nothing touches GCP until the operator
    # asks for it.
    yield
    tracer.step(SYSTEM, "Sentinel shutting down")
    memory_bank.log_event(key="ev.offline", level="INFO")


app = FastAPI(title="CloudFinOps Sentinel", version="2.0.0", lifespan=lifespan)

static_dir = os.path.join(WEB_DIR, "static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ----------------------------------------------------------------------
# Pages & health
# ----------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def serve_dashboard():
    index = os.path.join(WEB_DIR, "index.html")
    if os.path.exists(index):
        return FileResponse(index, media_type="text/html")
    raise HTTPException(status_code=404, detail="Dashboard not built")


class LoginRequest(BaseModel):
    token: str


@app.post("/api/login")
async def login(req: LoginRequest, response: Response):
    """Exchange the shared token for an HttpOnly session cookie."""
    if not auth_configured():
        return {"status": "open", "detail": "No token configured; access is unrestricted."}
    if not verify_login(req.token):
        raise HTTPException(status_code=401, detail="Invalid token.")

    session = issue_session()
    response.set_cookie(
        SESSION_COOKIE, session,
        httponly=True,      # not readable from JavaScript
        samesite="strict",  # not sent on cross-site requests
        secure=bool(os.environ.get("K_SERVICE")),  # HTTPS-only once hosted
        max_age=60 * 60 * 12,
    )
    logger.info("Operator session opened")
    return {"status": "ok"}


@app.post("/api/logout")
async def logout(response: Response, request: Request):
    revoke_session(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE)
    return {"status": "ok"}


@app.get("/api/auth")
async def auth_status():
    """Whether a login is required. Open so the UI knows what to render."""
    return describe_auth()


@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "service": "cloudfinops-sentinel",
        "version": app.version,
        "agent_mode": "gemini" if agent.is_live else "heuristic",
        "model": agent.model_name if agent.is_live else None,
        "backend": agent.backend,
        "project_id": settings.PROJECT_ID,
        "region": settings.REGION,
        "dry_run": settings.DRY_RUN,
        "mock_mode": settings.MOCK_MODE,
        "auth": describe_auth()["posture"],
        "telemetry": "opentelemetry" if telemetry.is_enabled() else "disabled",
    }


@app.get("/api/preflight", dependencies=[Depends(require_operator)])
async def preflight():
    """Verify credentials, APIs and write access; report how to fix failures."""
    return await asyncio.to_thread(run_preflight)


# ----------------------------------------------------------------------
# State
# ----------------------------------------------------------------------
def _non_compute_inventory(lang: str = DEFAULT_LANG, allow_discovery: bool = True) -> List[Dict[str, Any]]:
    """Disks, IPs and images, normalised into the shape the inventory expects."""
    if settings.MOCK_MODE:
        return []

    from app.tools import gcp_inventory

    found = gcp_inventory.discover_all(allow_discovery=allow_discovery)
    items: List[Dict[str, Any]] = []

    for disk in found["orphan_disks"]:
        items.append({
            **disk,
            "spec": f"{disk['size_gb']:.0f} GB {disk.get('disk_type', '')}",
            "location": disk.get("zone", ""),
            "status": _settled_or(disk["monthly_cost"], "Orphaned"),
            "wasted_cost": disk["monthly_cost"],
            "utilization": t(lang, "usage.unattached"),
            "url": (
                f"https://console.cloud.google.com/compute/disksDetail/zones/"
                f"{disk.get('zone', '')}/disks/{disk['resource_id']}?project={settings.PROJECT_ID}"
            ),
        })

    for addr in found["unused_addresses"]:
        items.append({
            **addr,
            "spec": addr.get("address", ""),
            "location": addr.get("region", ""),
            "status": _settled_or(addr["monthly_cost"], "Unused"),
            "wasted_cost": addr["monthly_cost"],
            "utilization": t(lang, "usage.not_in_use"),
            "url": f"https://console.cloud.google.com/networking/addresses/list?project={settings.PROJECT_ID}",
        })

    for image in found["untagged_images"]:
        items.append({
            **image,
            "resource_id": image.get("short_id") or image["resource_id"],
            "spec": image.get("repository", ""),
            "location": "registry",
            "status": _settled_or(image.get("monthly_cost", 0.10), "Untagged"),
            "wasted_cost": image.get("monthly_cost", 0.10),
            "utilization": t(lang, "usage.untagged"),
            "url": f"https://console.cloud.google.com/artifacts?project={settings.PROJECT_ID}",
        })

    for item in items:
        item["rationale"] = explain(item, lang)
    return items


def _settled_or(waste: float, status: str) -> str:
    """Below the action threshold nothing will ever be proposed, so the
    resource must not be painted as an open anomaly. Every type, not just
    Cloud Run — otherwise the anomaly count and the approval queue disagree.
    """
    return "Tolerated" if waste < settings.MIN_SAVINGS_THRESHOLD else status


def refresh_inventory() -> None:
    """Re-read the estate from GCP after the agent changed something.

    Deliberate, not incidental: polling still never triggers a scan. This runs
    only when an action has just made the cache untrue.
    """
    from app.tools import gcp_inventory

    if settings.MOCK_MODE:
        # Nothing to re-read: the demo fleet is static. Seeding from an empty
        # scan would blank it and label the result "gcp", which is how an
        # approval used to turn a simulated run into one claiming live data.
        return

    with telemetry.span("agent.refresh_after_action"):
        try:
            # One scan, not two: discover_all already lists Cloud Run, so its
            # result seeds the service cache instead of being fetched again.
            found = gcp_inventory.discover_all(force_refresh=True)
            gcp_metrics.seed_services(found.get("cloud_run", []))
            gcp_metrics._utilization_cache.clear()
            tracer.step(SYSTEM, "Inventory re-read after the change",
                        detail={"reason": "post-execution",
                                "resources": len(found.get("cloud_run", []))})
        except Exception as exc:
            logger.warning("Could not refresh the inventory after acting: %s", exc)


def build_full_inventory(allow_discovery: bool = True) -> List[Dict[str, Any]]:
    """Every managed resource, of every type, in one list."""
    resources, _ = describe_resources(allow_discovery=allow_discovery)
    return resources + _non_compute_inventory(allow_discovery=allow_discovery)


def _build_state(lang: str = DEFAULT_LANG) -> Dict[str, Any]:
    """Assemble the full dashboard payload. Runs off the event loop."""
    # allow_discovery=False: polling the dashboard must never start a scan.
    resources, data_source = describe_resources(allow_discovery=False)
    _, metrics_source = fleet_utilization(allow_discovery=False)
    store = memory_bank.snapshot()

    # Every resource carries the reasoning behind its verdict, so the UI can
    # answer "why did the agent propose this?" without another round-trip.
    analysis = last_analysis()
    for resource in resources:
        resource["rationale"] = explain(resource, lang)
        resource["analysis"] = analysis["by_resource"].get(resource["resource_id"])

    inventory = resources + _non_compute_inventory(lang, allow_discovery=False)
    for item in inventory:
        item.setdefault("analysis", analysis["by_resource"].get(item["resource_id"]))

    scanned = has_scanned()
    pending = [a for a in store["approvals"] if a["status"] == "PENDING"]
    # Tolerated resources exist and are fine; they are not open anomalies.
    settled = {"Healthy", "Tolerated"}
    anomalies = [r for r in inventory if r["status"] not in settled]
    monthly_spend = round(sum(r["monthly_cost"] for r in inventory), 2)
    wasted = round(sum(r["wasted_cost"] for r in inventory), 2)
    realized = memory_bank.total_savings()

    return {
        "kpis": {
            "monthly_spend": monthly_spend,
            "wasted_spend": wasted,
            "realized_savings": realized,
            "resources_monitored": len(inventory),
            "anomalies_open": len(anomalies),
            "approvals_pending": len(pending),
            "efficiency_score": round(100 * (1 - wasted / monthly_spend), 1) if monthly_spend else 100.0,
            "audits_completed": len([r for r in store["runs"] if r["status"] == "SUCCESS"]),
            "remediations_count": len(store["remediations"]),
        },
        "approvals": [render_approval(a, lang) for a in store["approvals"]],
        "remediations": list(reversed(store["remediations"]))[:20],
        "active_resources": [r for r in resources if r["status"] in settled],
        "all_resources": resources,
        "inventory": inventory,
        "anomalies": anomalies,
        "events": [render_event(e, lang) for e in list(reversed(store["events"]))[:25]],
        "runs": list(reversed(store["runs"]))[:10],
        "history": build_history(lang, limit=10),
        "charts": build_charts(resources, store["remediations"], inventory),
        "data_source": data_source,
        "agent_mode": "gemini" if agent.is_live else "heuristic",
        # The model that actually answered, not the one that was configured.
        # After a fallback these differ, and the header claiming Gemini while
        # Gemma wrote the analysis is the kind of small lie that makes an
        # operator stop trusting the rest of the panel.
        "model": (analysis.get("model") or agent.model_name) if agent.is_live else None,
        "backend": agent.backend,
        "project_id": settings.PROJECT_ID,
        "region": settings.REGION,
        "dry_run": settings.DRY_RUN,
        "writes_enabled": settings.writes_enabled,
        "metrics_source": metrics_source,
        "scanned": scanned,
        "scanned_age_seconds": last_scan_age_seconds(),
        "scanning": _audit_lock.locked(),
        "lang": lang,
        "regions_scanned": list(settings.regions),
        "analysis": {
            "summary": analysis.get("summary", ""),
            "model": analysis.get("model"),
            "at": analysis.get("at"),
            "covered": len(analysis.get("by_resource") or {}),
        },
        "problems": last_problems(),
    }


@app.get("/api/state", dependencies=[Depends(require_operator)])
async def get_state(lang: str = DEFAULT_LANG):
    """Full dashboard snapshot: KPIs, approvals, resources, charts and activity."""
    try:
        return await asyncio.to_thread(_build_state, normalise(lang))
    except Exception as exc:
        logger.error("Failed to build dashboard state: %s", exc, exc_info=True)
        raise HTTPException(status_code=503, detail="Unable to read infrastructure state")


@app.get("/api/resources", dependencies=[Depends(require_operator)])
async def list_resources(refresh: bool = False):
    if refresh:
        await asyncio.to_thread(fetch_services, True)
    resources, source = await asyncio.to_thread(describe_resources)
    return {"data_source": source, "resources": resources}


@app.get("/api/resources/{resource_id}/rationale", dependencies=[Depends(require_operator)])
async def resource_rationale(resource_id: str, lang: str = DEFAULT_LANG):
    """Why this resource was flagged, and the concrete fix."""
    resources, _ = await asyncio.to_thread(describe_resources)
    match = next((r for r in resources if r["resource_id"] == resource_id), None)
    if match is None:
        raise HTTPException(status_code=404, detail=f"Unknown resource {resource_id}")
    return await asyncio.to_thread(explain, match, normalise(lang))


@app.get("/api/history", dependencies=[Depends(require_operator)])
async def get_history(lang: str = DEFAULT_LANG, limit: int = 20):
    """Per-scan history: findings, recommendations, decisions and outcomes."""
    return {"history": await asyncio.to_thread(build_history, normalise(lang), limit)}


@app.get("/api/trace", dependencies=[Depends(require_operator)])
async def get_trace(since: int = 0, limit: int = 200):
    """Recent execution steps. `since` is the last `seq` the client holds."""
    return {"steps": tracer.steps(since=since, limit=limit)}


@app.get("/api/trace/stream", dependencies=[Depends(require_operator)])
async def stream_trace(request: Request):
    """Server-sent events: every step as it happens, live."""
    queue = tracer.subscribe()

    async def events():
        try:
            # Replay what already happened so a late subscriber sees the run.
            for step in tracer.steps(limit=60):
                yield f"data: {json.dumps(step)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=20)
                    yield f"data: {json.dumps(message)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # keep proxies from closing the stream
        finally:
            tracer.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/events", dependencies=[Depends(require_operator)])
async def list_events(limit: int = 50, lang: str = DEFAULT_LANG):
    events = list(reversed(memory_bank.snapshot()["events"]))[:limit]
    return {"events": [render_event(e, normalise(lang)) for e in events]}


# ----------------------------------------------------------------------
# Human-in-the-loop
# ----------------------------------------------------------------------
@app.post("/api/approvals", dependencies=[Depends(require_operator)])
async def handle_approval(req: ApprovalRequest):
    """Approve or reject a pending ticket. Approving applies the action."""
    approval = await asyncio.to_thread(
        memory_bank.resolve_approval, req.resource_id, req.status.value
    )
    if approval is None:
        raise HTTPException(
            status_code=404, detail=f"No pending approval for {req.resource_id}"
        )

    memory_bank.log_event(
        key="ev.human_decision",
        decision_key=f"ev.decision.{req.status.value.lower()}",
        action=approval["proposed_action"],
        action_key=approval.get("action_key") or "",
        resource=req.resource_id,
        level="APPROVAL",
        actor="operator",
        resource_id=req.resource_id,
    )

    tracer.step(
        "APPROVAL",
        f"Operator {req.status.value.lower()} '{approval['proposed_action']}' on {req.resource_id}",
        status="ok" if req.status.value == "APPROVED" else "warn",
        resource_id=req.resource_id,
        detail={"ticket_id": approval.get("ticket_id"),
                "decision": req.status.value,
                "estimated_savings_monthly": approval.get("estimated_roi")},
    )

    # Let every open dashboard know immediately, not on its next poll.
    tracer.notify_state_changed(f"decision:{req.resource_id}")

    if req.status.value == "APPROVED":
        await asyncio.to_thread(execute_approved_action, req.resource_id)
        # The change just altered live infrastructure, so the cached inventory
        # is now wrong. Re-read it before telling clients to refresh, otherwise
        # they redraw the same stale costs and states.
        await asyncio.to_thread(refresh_inventory)
        tracer.notify_state_changed(f"executed:{req.resource_id}")

    return {"status": "success", "ticket": approval}


# ----------------------------------------------------------------------
# Agent triggers
# ----------------------------------------------------------------------
async def run_audit_background() -> None:
    if _audit_lock.locked():
        logger.info("Audit already in progress; skipping duplicate trigger")
        return
    async with _audit_lock:
        logger.info("Starting infrastructure audit")
        tracer.clear()
        tracer.step(SYSTEM, "Audit triggered — starting infrastructure scan")
        # One root span per audit: everything below hangs off it in Cloud Trace.
        with telemetry.span("agent.audit", **{"agent.trigger": "manual",
                                              "gcp.project": settings.PROJECT_ID}):
            result = await asyncio.to_thread(agent.audit_infrastructure)
        tracer.step(
            SYSTEM, f"Audit finished: {result.get('status')}",
            status="ok" if result.get("status") == "success" else "error",
            detail={"mode": result.get("mode"), "anomalies": result.get("anomalies_found"),
                    "actions": result.get("actions_taken"), "degraded": result.get("degraded")},
        )
        logger.info("Audit finished: %s", result.get("status"))
        tracer.notify_state_changed("audit-finished")


@app.post("/api/trigger", dependencies=[Depends(require_operator)])
async def trigger_agent(background_tasks: BackgroundTasks):
    """Manual audit trigger from the dashboard."""
    if _audit_lock.locked():
        return JSONResponse({"status": "busy", "message": "An audit is already running"}, 409)
    background_tasks.add_task(run_audit_background)
    return {"status": "initiated"}


@app.post("/api/audit", dependencies=[Depends(require_operator)])
async def run_audit_sync():
    """Run an audit and wait for the result (useful for CLI / testing)."""
    async with _audit_lock:
        return await asyncio.to_thread(agent.audit_infrastructure)


@app.post("/webhook/pubsub", dependencies=[Depends(require_webhook)])
async def pubsub_webhook(payload: Dict[str, Any], background_tasks: BackgroundTasks):
    """Entrypoint for Cloud Scheduler / Pub/Sub push subscriptions."""
    logger.info("Received Pub/Sub push: %s", str(payload)[:400])
    background_tasks.add_task(run_audit_background)
    return {"status": "accepted"}


@app.post("/api/reset", dependencies=[Depends(require_operator)])
async def reset_state():
    """Clear the memory bank — handy for resetting a demo."""
    from app.tools import gcp_inventory

    memory_bank.reset()
    gcp_metrics._services_cache.reset()
    gcp_metrics._utilization_cache.reset()
    gcp_inventory._discovery_cache.reset()
    tracer.clear()
    memory_bank.log_event(key="ev.memory_reset", level="WARN")
    tracer.notify_state_changed("reset")
    return {"status": "reset"}
