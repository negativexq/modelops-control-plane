"""Worker Prometheus metrics, served on WORKER_METRICS_PORT (see main.py). The
worker is the control loop, so the questions here are "is it still running?"
(last sweep timestamp), "how long does a sweep take?" and "what did it do?"."""

from prometheus_client import Counter, Gauge, Histogram

from app.worker.decide import WorkerAction

SWEEPS = Counter("modelops_worker_sweeps_total", "Completed sweeps over active rollouts.")
SWEEP_DURATION = Histogram(
    "modelops_worker_sweep_duration_seconds",
    "Wall time of one sweep: reconcile tick plus every active rollout.",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)
LAST_SWEEP = Gauge(
    "modelops_worker_last_sweep_timestamp_seconds",
    "Unix time the last sweep finished. time() minus this is the loop's staleness.",
)
ACTIVE_ROLLOUTS = Gauge(
    "modelops_worker_active_rollouts",
    "Active, automation-enabled rollouts seen by the last sweep.",
)
ACTIONS = Counter(
    "modelops_worker_actions_total",
    "Actions the worker took on a rollout after an evaluation.",
    ["action"],
)
ERRORS = Counter(
    "modelops_worker_errors_total",
    "Failures the worker logged and moved past, by stage: reconcile, deployment, sweep.",
    ["stage"],
)

# Start every known series at 0 so the first promote or error is visible to
# increase() - see app/control_plane/telemetry.py.
for _action in WorkerAction:
    ACTIONS.labels(_action.value)
for _stage in ("reconcile", "deployment", "sweep"):
    ERRORS.labels(_stage)
