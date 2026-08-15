from datetime import datetime
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from app.control_plane.models import DeploymentStatus, PolicyEvaluationResult
from app.policy.config import PolicyConfig


class TargetWeight(BaseModel):
    version: str
    weight: float = Field(ge=0)


class CreateDeploymentRequest(BaseModel):
    model_name: str
    stable_version: str
    canary_version: str
    canary_weight: float = Field(default=0.1, ge=0, le=1)
    # If omitted, the environment-configured PolicySettings defaults are resolved and
    # stored on the deployment at creation time (see service.create_deployment) - the
    # deployment's policy_config is always a fully-resolved snapshot, never None.
    policy_config: PolicyConfig | None = None
    # Internal-only escape hatch: the benchmark suite's "baseline" scenario
    # deliberately uses canary_weight=0 (no canary traffic at all, v1-only
    # throughput measurement - see scripts/benchmarks/scenarios.py) - a
    # canary_version is still required by the schema even though it's never
    # actually routed to. A normal deployment with canary_weight=0 or 1 is
    # meaningless (there's no canary rollout happening), so the public API rejects
    # it by default; this flag opts back in explicitly rather than loosening
    # validation for every caller. The dashboard's NewDeploymentForm never sets it.
    allow_degenerate_canary_weight: bool = False
    # Creates the deployment already paused (see Deployment.automation_paused) -
    # lets a caller that wants to drive it purely manually do so without a
    # create-then-pause round trip, which would otherwise leave a real window
    # where the automated worker could already see and act on it. Used by
    # scripts/ci_smoke_test.py's manual-flow scenario for exactly this reason.
    automation_paused: bool = False

    @model_validator(mode="after")
    def _validate_versions_and_weight(self) -> Self:
        if self.stable_version == self.canary_version:
            raise ValueError("stable_version and canary_version must differ")
        if not self.allow_degenerate_canary_weight and not (0 < self.canary_weight < 1):
            raise ValueError(
                "canary_weight must be strictly between 0 and 1 for a real canary "
                "rollout (set allow_degenerate_canary_weight=true only if you "
                "genuinely mean 'no canary traffic at all')"
            )
        return self


class DeploymentEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_type: str
    message: str
    created_at: datetime


class TrafficAllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    targets: list[TargetWeight]
    # This deployment's desired-state revision (see
    # app/control_plane/models.py's TrafficAllocation.revision) - what the
    # router *should* have applied. Compared against the router's own observed
    # revision (GET /router/config) for drift detection - see
    # docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    revision: int
    updated_at: datetime


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    model_name: str
    stable_version: str
    canary_version: str
    status: DeploymentStatus
    policy_config: dict[str, Any] | None
    inconclusive_retry_count: int
    automation_paused: bool
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    traffic_allocation: TrafficAllocationOut | None
    events: list[DeploymentEventOut]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_benchmark(self) -> bool:
        """True for deployments created by the benchmark suite (model_name prefixed
        "benchmark-", see scripts/benchmarks/scenarios.py) - lets the dashboard tell
        them apart from real deployments without a DB column, since it's fully
        derivable from a field already on the row."""
        return self.model_name.startswith("benchmark-")


class TimelineEventItem(BaseModel):
    """One DeploymentEvent row, tagged for the merged timeline (see
    app/control_plane/timeline.py)."""

    type: Literal["event"] = "event"
    id: str
    timestamp: datetime
    event_type: str
    message: str


class TimelinePolicyItem(BaseModel):
    """One PolicyEvaluation row, tagged for the merged timeline, plus a derived
    human-readable `explanation` (see app/policy/explain.py) - the raw
    observed_value/threshold/result alone don't say *why* e.g. minimum_requests
    stayed INCONCLUSIVE."""

    type: Literal["policy_evaluation"] = "policy_evaluation"
    id: str
    timestamp: datetime
    policy_name: str
    metric_name: str
    observed_value: float | None
    threshold: float | None
    result: PolicyEvaluationResult
    explanation: str
    # True when this check predates the traffic-context snapshot columns on
    # PolicyEvaluation (added in a later migration) and `explanation` therefore had
    # to fall back to the deployment's *current* traffic split rather than what was
    # actually true at evaluation time - see app/control_plane/timeline.py.
    is_estimated: bool


TimelineItem = Annotated[
    TimelineEventItem | TimelinePolicyItem, Field(discriminator="type")
]


class MetricIn(BaseModel):
    """What the router POSTs after each forward. Kept small on purpose - this is the
    hot path.

    Deliberately has no `actual_label` field: ground truth is never known at
    prediction time, so this endpoint must never accept it directly from a
    caller - it's only ever written to GroundTruthLabel, via POST /api/labels
    (see label_service.ingest_label) and joined against PredictionMetric at
    read time (see metrics_service.compute_version_summary). See
    docs/DESIGN_NOTES.md.
    """

    model_version: str
    latency_ms: float = Field(ge=0)
    status_code: int
    prediction: int | None = None
    # Minted in app/serving/ (see PredictionResponse.prediction_id), carried
    # through unmodified by the router. The join key POST /api/labels uses to
    # attach a delayed ground-truth label to this specific prediction - see
    # metrics_service.record_metric and docs/DESIGN_NOTES.md.
    prediction_id: str | None = None


class MetricsSummary(BaseModel):
    version: str
    sample_count: int
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None
    error_rate: float | None
    # precision/recall/false_positive_rate are only meaningful once actual_label has
    # been recorded for at least one sample in the window (see POST /api/labels) -
    # None (not 0) when there's nothing labeled to compute them from.
    precision: float | None
    recall: float | None
    false_positive_rate: float | None
    # How many of `sample_count` rows have a real actual_label, and what fraction
    # that is - None only when sample_count itself is 0 (nothing to have a
    # coverage fraction of); a real 0.0 means "samples exist, none labeled yet",
    # which is a meaningfully different, common state (see label_maturity_seconds
    # in app/policy/config.py).
    labeled_sample_count: int
    label_coverage: float | None
    # How many of `labeled_sample_count` are the positive class (actual_label=1) -
    # recall's true denominator (TP+FN), not `labeled_sample_count` itself. On a
    # dataset with a low positive rate, `labeled_sample_count` clearing its own
    # threshold says nothing about whether enough *positive* examples landed to
    # make `recall` statistically meaningful - see app/policy/engine.py's
    # minimum_positive_labels gate and docs/DESIGN_NOTES.md.
    positive_label_count: int
    # Wall-clock delay between a prediction happening and its label actually
    # arriving (GroundTruthLabel.ingested_at - PredictionMetric.created_at),
    # summarized across whatever's labeled in this window. None when nothing in
    # the window is labeled yet.
    label_delay_p50_seconds: float | None
    label_delay_p95_seconds: float | None


class LabelIn(BaseModel):
    """One ground-truth label - what a label feeder POSTs to /api/labels[/batch].
    `prediction_id` is the join key back to a specific PredictionMetric - see
    GroundTruthLabel and label_service.py."""

    prediction_id: str
    actual_label: int = Field(ge=0, le=1)
    # When the label actually happened in the real world, per the feeder - distinct
    # from `ingested_at` (server-assigned, see GroundTruthLabel), which is what the
    # label-delay metric is actually computed from.
    occurred_at: datetime


class LabelReportItem(BaseModel):
    """Per-item result, returned by both POST /api/labels (a list of one) and
    POST /api/labels/batch (the whole batch) - the same shape either way, so a
    caller never needs two different response parsers."""

    prediction_id: str
    status: Literal["applied", "no_op", "pending", "conflict"]
    detail: str | None = None


class MetricsOut(BaseModel):
    window_seconds: int
    stable: MetricsSummary
    canary: MetricsSummary


class MetricsDeltas(BaseModel):
    p95_latency_ms: float | None
    error_rate: float | None
    recall: float | None


class ComparisonOut(BaseModel):
    window_seconds: int
    stable: MetricsSummary
    canary: MetricsSummary
    deltas: MetricsDeltas
