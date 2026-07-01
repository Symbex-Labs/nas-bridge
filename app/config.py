from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    bridge_token: str = Field(..., description="Bearer token required on all API requests")
    root_path: str = Field("/volume1/Jobs", description="Absolute path to the Jobs share on the NAS")
    host: str = Field("0.0.0.0", description="Interface to bind to")
    port: int = Field(8080, description="TCP port to listen on")
    log_level: str = Field("info", description="Uvicorn log level (debug/info/warning/error)")
    version: str = Field("1.5.0", description="Service version string returned by /health")
    max_file_size_bytes: int = Field(
        200 * 1024 * 1024,
        description="Maximum file size in bytes accepted by POST /api/v1/write (default 200 MB)",
    )

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
