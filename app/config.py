from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    bridge_token: str = Field(..., description="Bearer token required on all API requests")
    root_path: str = Field("/volume1/Jobs", description="Absolute path to the Jobs share on the NAS")
    host: str = Field("0.0.0.0", description="Interface to bind to")
    port: int = Field(8080, description="TCP port to listen on")
    log_level: str = Field("info", description="Uvicorn log level (debug/info/warning/error)")
    version: str = Field("1.2.0", description="Service version string returned by /health")

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
