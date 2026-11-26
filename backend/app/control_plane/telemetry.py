"""Control-plane Prometheus metrics: rollout decisions, policy evaluations and
router reconciliation. Counters are incremented only after the change they
describe has been committed, so a rolled-back transaction never counts.

They're process counters, so they start from zero when the backend restarts -
use rate()/increase() over them, which handle resets. The durable history is
the database; GET /api/fleet/summary reads it for the dashboard."""

from prometheus_client import Counter, Gauge

DECISIONS = Counter(
    "modelops_decisions_total",
    "Rollouts that reached a final decision, by outcome and by who decided "
    "(automatic = the worker acting on a policy verdict, manual = an operator).",
    ["outcome", "trigger"],
)
POLICY_EVALUATIONS = Counter(
    "modelops_policy_evaluations_total",
    "Policy evaluation runs, by overall verdict.",
    ["result"],
)
POLICY_CHECKS = Counter(
    "modelops_policy_checks_total",
    "Individual policy checks, by policy and verdict - which gate fails most.",
    ["policy", "result"],
)
RECONCILE_RUNS = Counter(
    "modelops_reconcile_runs_total",
    "Desired/observed comparisons per model, by result: in_sync, corrected, "
    "router_unreachable, push_failed.",
    ["model", "result"],
)
RECONCILE_ERRORS = Counter(
    "modelops_reconcile_errors_total",
    "Reconcile ticks that couldn't bring the router in line, by reason.",
    ["reason"],
)
ROUTER_PUSH_FAILURES = Counter(
    "modelops_router_push_failures_total",
    "Best-effort router pushes after a state change that failed (the reconciler "
    "repairs them on its next tick).",
    ["model"],
)
WEBHOOK_DELIVERIES = Counter(
    "modelops_webhook_deliveries_total",
    "Decision notification attempts, by event and result: delivered, retrying "
    "(failed, will retry) or failed (gave up).",
    ["event", "result"],
)
LAST_RECONCILE = Gauge(
    "modelops_reconcile_last_run_timestamp_seconds",
    "Unix time of the last reconcile tick, whatever its result.",
)


# Every label combination that's known up front starts at 0. Without this a
# series is born at 1 on its first increment, and increase()/rate() - which need
# two samples - never see that first rollback or failed check.
POLICY_NAMES = (
    "minimum_requests",
    "latency_p95_increase",
    "max_error_rate",
    "minimum_labeled_samples",
    "minimum_label_coverage",
    "minimum_positive_labels",
    "minimum_recall",
)
VERDICTS = ("PASS", "FAIL", "INCONCLUSIVE")

for _outcome, _triggers in (
    ("PROMOTED", ("automatic", "manual")),
    ("ROLLED_BACK", ("automatic", "manual")),
    ("INCONCLUSIVE", ("automatic",)),
):
    for _trigger in _triggers:
        DECISIONS.labels(_outcome, _trigger)
for _verdict in VERDICTS:
    POLICY_EVALUATIONS.labels(_verdict)
    for _policy in POLICY_NAMES:
        POLICY_CHECKS.labels(_policy, _verdict)
for _reason in ("router_unreachable", "push_failed"):
    RECONCILE_ERRORS.labels(_reason)
