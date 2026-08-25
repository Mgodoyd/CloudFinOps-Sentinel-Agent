import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class MemoryBank:
    def __init__(self):
        # We will use a mock in-memory store for the hackathon prototype
        self.mock_store: Dict[str, List[Dict[str, Any]]] = {
            "remediations": [],
            "approvals": []
        }

    def log_remediation(self, event_id: str, action: str, savings: float):
        logger.info(f"Logging remediation to Memory Bank: {event_id} - {action} (Savings: ${savings})")
        self.mock_store["remediations"].append({
            "event_id": event_id,
            "action": action,
            "savings": savings
        })
        return True

    def check_history(self, resource_id: str) -> Optional[Dict[str, Any]]:
        for r in self.mock_store["remediations"]:
            if r.get("resource_id") == resource_id:
                return r
        return None

memory_bank = MemoryBank()
