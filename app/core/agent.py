from google import genai
from app.core.config import settings
from app.core.prompts import SYSTEM_INSTRUCTION
from app.tools.gcp_metrics import get_infrastructure_anomalies
from app.tools.gcp_remediator import resize_cloud_run, delete_orphan_disk, request_human_approval, purge_untagged_image
from app.tools.memory_tools import memory_bank
import logging

logger = logging.getLogger(__name__)

class CloudFinOpsAgent:
    def __init__(self):
        try:
            # We use gemini-3.5-flash as requested in the proposal
            self.client = genai.Client(api_key=settings.GEMINI_API_KEY)
            self.model_name = 'gemini-3.5-flash'
        except Exception as e:
            logger.warning(f"Failed to initialize Gemini Client: {e}")
            self.client = None
            
        self.tools = [
            resize_cloud_run,
            delete_orphan_disk,
            request_human_approval,
            purge_untagged_image,
            memory_bank.check_history
        ]

    def audit_infrastructure(self, data: dict = None):
        if not self.client:
            return {"status": "error", "message": "Gemini client not initialized"}
            
        if data is None:
            data = get_infrastructure_anomalies()
            
        try:
            prompt = f"Audit the following infrastructure data for cost anomalies:\n{data}"
            response = self.client.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=genai.types.GenerateContentConfig(
                    system_instruction=SYSTEM_INSTRUCTION,
                    temperature=0.2,
                    tools=self.tools
                ),
            )
            return {"status": "success", "response": response.text}
        except Exception as e:
            logger.error(f"Agent execution failed: {e}")
            return {"status": "error", "message": str(e)}
