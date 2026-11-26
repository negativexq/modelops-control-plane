"""Router-side Prometheus metrics: what was routed where, how it went, and the
traffic split currently applied per model (so a dashboard can plot the split
moving over a rollout without asking the control plane)."""

from prometheus_client import Counter, Gauge, Histogram

from app.observability.metrics import LATENCY_BUCKETS
from app.router.config import RouterConfig

ROUTED_REQUESTS = Counter(
    "modelops_router_requests_total",
    "Prediction requests routed, by model, the version picked and the status returned "
    "(503: the picked version wasn't ready or couldn't be reached).",
    ["model", "version", "status"],
)
UPSTREAM_LATENCY = Histogram(
    "modelops_router_upstream_latency_seconds",
    "Round trip from the router to the chosen model service's /predict.",
    ["model", "version"],
    buckets=LATENCY_BUCKETS,
)
TRAFFIC_WEIGHT = Gauge(
    "modelops_router_traffic_weight",
    "Share of a model's traffic currently sent to each version (0-1).",
    ["model", "version"],
)
CONFIG_REVISION = Gauge(
    "modelops_router_config_revision",
    "Routing generation currently applied for a model.",
    ["model"],
)


def record_split(config: RouterConfig, previous: RouterConfig | None) -> None:
    """Replace the model's weight series: versions that left the split are
    removed rather than left at a stale weight."""
    current = {t.version for t in config.targets}
    for target in previous.targets if previous else []:
        if target.version not in current:
            TRAFFIC_WEIGHT.remove(config.model_name, target.version)
    for target in config.targets:
        TRAFFIC_WEIGHT.labels(config.model_name, target.version).set(target.weight)
    CONFIG_REVISION.labels(config.model_name).set(config.revision)
