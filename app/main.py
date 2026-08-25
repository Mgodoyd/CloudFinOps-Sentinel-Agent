from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
import os
import logging
from typing import Dict, Any

from app.core.agent import CloudFinOpsAgent
from app.tools.memory_tools import memory_bank

# Basic structured logging setup
logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": "%(message)s"}'
)
logger = logging.getLogger(__name__)

app = FastAPI(title="CloudFinOps Sentinel", version="1.0.0")

# Mount static files for the dashboard
static_dir = os.path.join(os.path.dirname(__file__), "web", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

agent = CloudFinOpsAgent()

@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "cloudfinops-sentinel"}

@app.get("/", response_class=HTMLResponse)
async def serve_dashboard():
    html_path = os.path.join(os.path.dirname(__file__), "web", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return "<h1>CloudFinOps Sentinel Dashboard (Under Construction)</h1>"

from app.tools.gcp_metrics import get_active_resources

@app.get("/api/state")
async def get_state():
    """Fetch pending approvals, remediation history, and active resources."""
    return {
        "approvals": memory_bank.mock_store["approvals"],
        "remediations": memory_bank.mock_store["remediations"],
        "active_resources": get_active_resources()
    }

class ApprovalRequest(BaseModel):
    resource_id: str
    status: str

@app.post("/api/approvals")
async def handle_approval(req: ApprovalRequest):
    """Approve or deny an action."""
    for approval in memory_bank.mock_store["approvals"]:
        if approval["resource_id"] == req.resource_id and approval["status"] == "PENDING":
            approval["status"] = req.status
            logger.info(f"Approval for {req.resource_id} marked as {req.status}")
            if req.status == "APPROVED":
                # Execute the action (mocked)
                memory_bank.log_remediation(
                    event_id=f"approved_{req.resource_id}",
                    action=approval["proposed_action"],
                    savings=approval["estimated_roi"]
                )
            return {"status": "success"}
    return {"status": "error", "message": "Approval not found"}

def run_audit_background():
    logger.info("Starting background infrastructure audit...")
    result = agent.audit_infrastructure()
    logger.info(f"Audit completed: {result}")

@app.post("/api/trigger")
async def trigger_agent(background_tasks: BackgroundTasks):
    """Manual trigger from the UI."""
    background_tasks.add_task(run_audit_background)
    return {"status": "initiated"}

@app.post("/webhook/pubsub")
async def pubsub_webhook(payload: Dict[str, Any], background_tasks: BackgroundTasks):
    """Webhook for Cloud Scheduler / PubSub triggers."""
    logger.info(f"Received PubSub webhook: {payload}")
    background_tasks.add_task(run_audit_background)
    return {"status": "accepted"}
