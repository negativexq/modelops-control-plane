from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MODELOPS_")

    app_name: str = "ModelOps Control Plane"
    environment: str = "development"
    database_url: str = "sqlite:///./modelops.db"
    model_artifacts_dir: Path = BACKEND_DIR / "artifacts"
    # The dashboard (frontend/) calls this API directly from the browser, so it needs
    # to be a CORS-allowed origin. Comma-separated list of origins.
    cors_allow_origins: str = "http://localhost:3000"


settings = Settings()
