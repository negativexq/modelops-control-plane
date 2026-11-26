from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlPlaneSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTROL_PLANE_", protected_namespaces=())

    # Where the traffic router lives. The control plane only ever tells the router
    # "version -> weight" - it never resolves or sends host/port for a model version;
    # that mapping is the router's own deployment config.
    router_base_url: str = "http://router:8000"
    router_timeout_seconds: float = 5.0

    default_canary_weight: float = 0.1

    # The model registry's artifact tree, read for each version's training
    # profile (reference_profile.json) by the drift check. Read-only.
    artifacts_dir: Path = Path("artifacts")

    # Where decision notifications go (app/notifications/): a JSON list of URLs,
    # e.g. CONTROL_PLANE_WEBHOOK_URLS='["https://hooks.example.com/modelops"]'.
    # Empty means no notifications are queued at all.
    webhook_urls: list[str] = []
    # Signs each delivery (X-ModelOps-Signature: sha256=<HMAC of the body>) so a
    # receiver can tell it came from this control plane. Unsigned when unset.
    webhook_secret: str | None = None
    webhook_max_attempts: int = 6
    webhook_timeout_seconds: float = 5.0
    # Base URL of the dashboard, for the link in each notification.
    dashboard_url: str = "http://localhost:3000"


control_plane_settings = ControlPlaneSettings()
