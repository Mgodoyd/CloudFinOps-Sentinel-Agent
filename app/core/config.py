import os
from pydantic_settings import BaseSettings

# Auto-cargar credenciales de Service Account para el Hackathon
cred_path = r"c:\Users\godoy\Desktop\All Things Agentic Hackathon\synox-ai-27d1864d0cf1.json"
if os.path.exists(cred_path):
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = cred_path

class Settings(BaseSettings):
    PROJECT_ID: str = "synox-ai"
    REGION: str = "us-central1"
    # El valor real debe ir en el archivo .env, no en el código
    GEMINI_API_KEY: str = "tu-api-key-aqui"
    
    class Config:
        env_file = ".env"

settings = Settings()
