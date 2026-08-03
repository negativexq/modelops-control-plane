from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent

PRODUCTION_ENVIRONMENT = "production"


class ServingSettings(BaseSettings):
    """Configuration for a single model-serving process.

    One process serves exactly one (model_name, model_version) pair, selected via
    environment variables so the same image can run as different containers per version.
    """

    model_config = SettingsConfigDict(env_prefix="", protected_namespaces=())

    model_name: str = "fraud-model"
    model_version: str = "v1"
    artifacts_dir: Path = BACKEND_DIR / "artifacts"
    environment: str = "development"

    # Fault injection (disabled by default, forced off in production regardless of env vars).
    injected_latency_ms: int = 0
    injected_error_rate: float = 0.0

    @model_validator(mode="after")
    def _disable_fault_injection_in_production(self) -> "ServingSettings":
        if self.environment.lower() == PRODUCTION_ENVIRONMENT:
            self.injected_latency_ms = 0
            self.injected_error_rate = 0.0
        return self

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == PRODUCTION_ENVIRONMENT


serving_settings = ServingSettings()
