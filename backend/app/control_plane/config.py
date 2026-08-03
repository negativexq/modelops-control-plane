from pydantic_settings import BaseSettings, SettingsConfigDict


class ControlPlaneSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CONTROL_PLANE_", protected_namespaces=())

    # Where the traffic router lives. The control plane only ever tells the router
    # "version -> weight" - it never resolves or sends host/port for a model version;
    # that mapping is the router's own deployment config.
    router_base_url: str = "http://router:8000"
    router_timeout_seconds: float = 5.0

    default_canary_weight: float = 0.1


control_plane_settings = ControlPlaneSettings()
