from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
RESULTS_DIR = BACKEND_DIR / "benchmark-results"


class BenchmarkApiSettings(BaseSettings):
    """Where to point `scripts.benchmarks.run_benchmark` when the backend spawns it
    as a subprocess for a dashboard-triggered run. Defaults are docker-internal
    service addresses (this settings class is only ever used by the backend
    container itself, unlike scripts/benchmarks/run_benchmark.py's own CLI defaults,
    which target host-mapped ports for Makefile/CLI use).
    """

    model_config = SettingsConfigDict(env_prefix="BENCHMARK_", protected_namespaces=())

    router_url: str = "http://router:8000"
    control_plane_url: str = "http://localhost:8000"
    default_users: int = 20
    default_target_rps: float = 100.0


benchmark_api_settings = BenchmarkApiSettings()
