from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """The worker is a separate process/container that only ever talks to the
    control plane over HTTP (see app/worker/client.py) - it has no direct DB access
    and keeps no in-memory rollout state, so a restart just resumes from whatever the
    control plane's API currently reports."""

    model_config = SettingsConfigDict(env_prefix="WORKER_", protected_namespaces=())

    control_plane_base_url: str = "http://backend:8000"
    request_timeout_seconds: float = 10.0
    # How often the worker sweeps the list of active deployments. This is NOT the
    # same as a policy's evaluation_window_seconds - the worker still skips a
    # deployment whose window hasn't elapsed since its last evaluation (see
    # loop.py's _is_due_for_evaluation), so polling faster than the window just means
    # noticing sooner that nothing is due yet, not re-evaluating too often.
    poll_interval_seconds: float = 15.0
    # Fallback only: used when a deployment somehow has no persisted policy_config
    # (pre-Sprint-8 data) and gating on "how often is a re-evaluation due" still
    # needs a number.
    default_evaluation_window_seconds: int = 300


worker_settings = WorkerSettings()
