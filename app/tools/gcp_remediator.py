import logging
from app.tools.memory_tools import memory_bank
from app.core.config import settings

logger = logging.getLogger(__name__)

def resize_cloud_run(service_id: str, new_memory: str) -> str:
    """Action to resize a cloud run service."""
    logger.info(f"Resizing Cloud Run service {service_id} to {new_memory}")
    memory_bank.log_remediation(event_id=f"resize_{service_id}", action="resize_cloud_run", savings=50.0)
    return f"Successfully resized {service_id} to {new_memory}"

def delete_orphan_disk(disk_id: str) -> str:
    """Action to delete an orphan disk."""
    logger.info(f"Deleting orphan disk {disk_id}")
    memory_bank.log_remediation(event_id=f"delete_{disk_id}", action="delete_disk", savings=25.0)
    return f"Successfully deleted disk {disk_id}"

def request_human_approval(resource_id: str, proposed_action: str, estimated_roi: float, resource_url: str = "", detailed_reason: str = "") -> str:
    """Action to request human approval for a high-risk operation."""
    logger.info(f"Requesting human approval for {proposed_action} on {resource_id}. ROI: {estimated_roi}")
    memory_bank.mock_store["approvals"].append({
        "resource_id": resource_id,
        "proposed_action": proposed_action,
        "estimated_roi": estimated_roi,
        "resource_url": resource_url or f"https://console.cloud.google.com/run/detail/{settings.REGION}/{resource_id}/metrics?project={settings.PROJECT_ID}",
        "detailed_reason": detailed_reason or "Automated anomaly detection requested this change.",
        "status": "PENDING"
    })
    return f"Approval ticket created for {resource_id}"

def purge_untagged_image(image_id: str) -> str:
    """Action to delete/purge an untagged container image from Artifact Registry."""
    logger.info(f"Purging untagged image {image_id}")
    memory_bank.log_remediation(event_id=f"purge_{image_id}", action="purge_untagged_image", savings=5.0)
    return f"Successfully purged untagged image {image_id}"
