from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any

class AnomalyEvent(BaseModel):
    resource_id: str
    resource_type: str
    anomaly_type: str
    severity: str # "LOW" or "HIGH"
    details: Dict[str, Any]

class ApprovalTicket(BaseModel):
    ticket_id: str
    event: AnomalyEvent
    proposed_action: str
    estimated_roi: float
    status: str = "PENDING" # PENDING, APPROVED, REJECTED
    
class RemediationResult(BaseModel):
    success: bool
    message: str
    action_taken: str
