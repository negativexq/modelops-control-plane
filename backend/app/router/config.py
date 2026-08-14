import threading

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class TargetWeight(BaseModel):
    """A version's share of traffic. No host/port here - the control plane only ever
    knows "version -> weight"; resolving a version to where it's actually running is
    the router's own concern (see RouterSettings.version_hosts below).
    """

    version: str
    weight: float = Field(ge=0)


class RouterConfig(BaseModel):
    model_name: str
    targets: list[TargetWeight]
    # Which control-plane Deployment this traffic split belongs to, if any. Carried
    # along so the router can attribute the metrics it emits per forward to the right
    # deployment - it's set by the control plane on every PUT /router/config and is
    # otherwise None (e.g. the router's own static default before any deployment
    # exists), in which case metric emission is skipped.
    deployment_id: str | None = None
    # Desired-state revision this config represents, for the *same* deployment_id -
    # see app/control_plane/models.py's TrafficAllocation.revision. 0 for the
    # router's own static bootstrap config (no real deployment behind it yet), so
    # any real push (always >= 1) is accepted regardless of what came before.
    # PUT /router/config (below) rejects a push for the same deployment_id whose
    # revision isn't strictly greater than what's already applied - see
    # docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    revision: int = 0

    @model_validator(mode="after")
    def _validate_targets(self) -> "RouterConfig":
        if not self.targets:
            raise ValueError("targets must not be empty")
        versions = [t.version for t in self.targets]
        if len(versions) != len(set(versions)):
            raise ValueError("duplicate versions in targets")
        if sum(t.weight for t in self.targets) <= 0:
            raise ValueError("sum of target weights must be > 0")
        return self


class RouterSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ROUTER_", protected_namespaces=())

    model_name: str = "fraud-model"

    # Static deployment topology: which host:port serves each known version. This is
    # the router's own config - the control plane never sends host/port, only
    # "version -> weight" (see RouterConfig above).
    version_hosts: dict[str, str] = {
        "v1": "model-serving-v1:8000",
        "v2-good": "model-serving-v2-good:8000",
        "v2-quality-bad": "model-serving-v2-quality-bad:8000",
    }

    # Initial traffic split, used until the control plane pushes a real one (either at
    # startup sync, via control_plane_url, or later via PUT /router/config).
    initial_targets: list[TargetWeight] = [
        TargetWeight(version="v1", weight=0.9),
        TargetWeight(version="v2-good", weight=0.1),
    ]

    # If set, the router fetches the current traffic allocation from the control plane
    # once at startup. Best-effort only: not a full sync mechanism, just a way to avoid
    # starting with a stale default after a restart. Failures fall back to
    # initial_targets and are logged, not raised.
    control_plane_url: str | None = None

    # Deliberately no retries: a canary rollout needs to see each upstream call's real
    # outcome. Silently retrying would mask the transient errors a promotion decision
    # is supposed to catch, and could double-apply side effects downstream. If a client
    # of the router wants retries, that's their call to make explicitly - not the
    # router's to make on their behalf.
    upstream_timeout_seconds: float = 5.0

    def to_router_config(self) -> RouterConfig:
        return RouterConfig(model_name=self.model_name, targets=list(self.initial_targets))

    def resolve_base_url(self, version: str) -> str | None:
        host_port = self.version_hosts.get(version)
        if host_port is None:
            return None
        return f"http://{host_port}"


class RouterConfigStore:
    """Thread-safe holder for the mutable, runtime-updatable router configuration."""

    def __init__(self, initial: RouterConfig) -> None:
        self._lock = threading.Lock()
        self._config = initial

    def get(self) -> RouterConfig:
        with self._lock:
            return self._config

    def set(self, config: RouterConfig) -> None:
        with self._lock:
            self._config = config


router_settings = RouterSettings()
