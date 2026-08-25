from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class DataSource(str, Enum):
    """Whether a payload came from the live GCP API or from the fallback."""

    GCP = "gcp"
    SIMULATED = "simulated"


class AnomalyEvent(BaseModel):
    resource_id: str
    resource_type: str = "Cloud Run"
    anomaly_type: str
    severity: Severity = Severity.LOW
    current_cost: float = 0.0
    potential_savings: float = 0.0
    details: Dict[str, Any] = Field(default_factory=dict)


class ApprovalTicket(BaseModel):
    ticket_id: str
    resource_id: str
    proposed_action: str
    estimated_roi: float
    resource_url: str = ""
    detailed_reason: str = ""
    severity: Severity = Severity.HIGH
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: str = Field(default_factory=_now)
    resolved_at: Optional[str] = None


class Remediation(BaseModel):
    event_id: str
    resource_id: str
    action: str
    savings: float
    source: str = "agent"  # "agent" | "human-approved"
    timestamp: str = Field(default_factory=_now)


class ActivityEvent(BaseModel):
    timestamp: str = Field(default_factory=_now)
    level: str = "INFO"  # INFO | ACTION | WARN | APPROVAL
    actor: str = "sentinel"
    message: str
    resource_id: Optional[str] = None


class AuditRun(BaseModel):
    run_id: str
    started_at: str = Field(default_factory=_now)
    finished_at: Optional[str] = None
    status: str = "RUNNING"  # RUNNING | SUCCESS | ERROR
    anomalies_found: int = 0
    actions_taken: int = 0
    summary: str = ""
    error: Optional[str] = None


class ApprovalRequest(BaseModel):
    resource_id: str
    status: ApprovalStatus


class ResourceView(BaseModel):
    resource_id: str
    type: str = "Cloud Run"
    status: str = "Healthy"
    cpu_limit: str = ""
    memory_limit: str = ""
    cpu_utilization: float = 0.0
    memory_utilization: float = 0.0
    monthly_cost: float = 0.0
    wasted_cost: float = 0.0
    metric: str = ""
    url: str = ""


class KPISnapshot(BaseModel):
    monthly_spend: float = 0.0
    wasted_spend: float = 0.0
    realized_savings: float = 0.0
    resources_monitored: int = 0
    anomalies_open: int = 0
    approvals_pending: int = 0
    efficiency_score: float = 0.0
    audits_completed: int = 0
    remediations_count: int = 0


class DashboardState(BaseModel):
    kpis: KPISnapshot
    approvals: List[Dict[str, Any]] = Field(default_factory=list)
    remediations: List[Dict[str, Any]] = Field(default_factory=list)
    active_resources: List[Dict[str, Any]] = Field(default_factory=list)
    anomalies: List[Dict[str, Any]] = Field(default_factory=list)
    events: List[Dict[str, Any]] = Field(default_factory=list)
    runs: List[Dict[str, Any]] = Field(default_factory=list)
    charts: Dict[str, Any] = Field(default_factory=dict)
    data_source: DataSource = DataSource.SIMULATED
    generated_at: str = Field(default_factory=_now)
