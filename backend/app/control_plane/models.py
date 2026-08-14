import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy import text as sql_text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utcnow() -> datetime:
    # Python-side, microsecond-resolution default. SQLite's CURRENT_TIMESTAMP (the
    # server_default below) is only second-resolution, which made otherwise-correct
    # "ORDER BY created_at DESC" ties break in insertion order instead of the other
    # way around for rows created within the same second.
    return datetime.now(UTC)


class DeploymentStatus(enum.StrEnum):
    PENDING = "PENDING"
    DEPLOYING = "DEPLOYING"
    CANARY_RUNNING = "CANARY_RUNNING"
    EVALUATING = "EVALUATING"
    PROMOTING = "PROMOTING"
    PROMOTED = "PROMOTED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


TERMINAL_STATUSES = frozenset(
    {
        DeploymentStatus.PROMOTED,
        DeploymentStatus.ROLLED_BACK,
        DeploymentStatus.FAILED,
    }
)

# Raw SQL fragment for the partial-unique-index WHERE clause below - built from
# TERMINAL_STATUSES (sorted for a deterministic string - a plain frozenset's
# iteration order isn't guaranteed stable) so the index's notion of "still
# in-flight" can never silently drift from the enum it's derived from. Broader
# than ACTIVE_STATUSES in control_plane/service.py on purpose: INCONCLUSIVE is a
# frozen-but-unresolved deployment (a human still has to promote/roll it back), so
# a second deployment for the same model would be just as confusing there as while
# CANARY_RUNNING - see the migration and docs/DESIGN_NOTES.md for the full
# reasoning. Do not touch service.get_active_deployment's ACTIVE_STATUSES to match
# this - that helper answers a different, narrower question ("what does the router
# sync from"), not "what does this DB invariant allow".
_TERMINAL_STATUS_VALUES_SQL = ", ".join(
    f"'{value}'" for value in sorted(status.value for status in TERMINAL_STATUSES)
)
ACTIVE_PER_MODEL_INDEX_NAME = "uq_deployments_active_per_model"


def _new_id() -> str:
    return str(uuid.uuid4())


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    stable_version: Mapped[str] = mapped_column(String(100), nullable=False)
    canary_version: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[DeploymentStatus] = mapped_column(
        Enum(DeploymentStatus, native_enum=False, length=32),
        nullable=False,
        default=DeploymentStatus.PENDING,
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(200), unique=True, nullable=True, index=True
    )
    # The resolved PolicyConfig (see app/policy/config.py) this deployment was created
    # with - always a fully-resolved dict (defaults already applied), never partial,
    # so a worker reading it back never needs to know about env-var defaults. Stored
    # per-deployment (not a shared/global row) so each rollout's thresholds stay fixed
    # even if the global defaults change later.
    policy_config: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # How many consecutive automated evaluate cycles have come back INCONCLUSIVE.
    # Reset implicitly by never being touched once a PASS/FAIL action fires (those
    # move the deployment out of the loop that increments this). Persisted (not
    # in-memory) so a worker restart doesn't forget how many retries have happened.
    inconclusive_retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Bumped on every write (see control_plane/service.py's _touch) so a concurrent
    # promote/rollback/advance-traffic/record-inconclusive that read a stale copy of
    # this row loses its commit instead of silently overwriting a newer one - see
    # version_id below. Also independently useful as a plain "last modified" column.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, onupdate=_utcnow
    )
    # SQLAlchemy optimistic-locking column: every UPDATE to this row is guarded by
    # `WHERE version_id = <the value this session last read>`, and SQLAlchemy raises
    # StaleDataError (caught in service.py as ConcurrentUpdateError, surfaced as a
    # 409) if zero rows matched - i.e. someone else committed a change to this same
    # deployment first. This is what actually makes concurrent actions race-safe,
    # not just sequential stale-status checks (see docs/DESIGN_NOTES.md).
    version_id: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    # DB-level backstop for "one in-flight deployment per model" (app-level
    # pre-check: service.get_active_deployment + ActiveDeploymentExistsError, in
    # service.create_deployment). The pre-check alone is check-then-act and races;
    # this partial unique index makes the invariant true even under a concurrent
    # INSERT that slipped past the pre-check - see the migration that added it and
    # docs/DESIGN_NOTES.md. sqlite_where/postgresql_where are both given so this
    # survives an eventual SQLite -> Postgres move without rewriting the index.
    __table_args__ = (
        Index(
            ACTIVE_PER_MODEL_INDEX_NAME,
            "model_name",
            unique=True,
            sqlite_where=sql_text(f"status NOT IN ({_TERMINAL_STATUS_VALUES_SQL})"),
            postgresql_where=sql_text(f"status NOT IN ({_TERMINAL_STATUS_VALUES_SQL})"),
        ),
    )

    traffic_allocation: Mapped["TrafficAllocation | None"] = relationship(
        back_populates="deployment", uselist=False, cascade="all, delete-orphan"
    )
    events: Mapped[list["DeploymentEvent"]] = relationship(
        back_populates="deployment",
        cascade="all, delete-orphan",
        order_by="DeploymentEvent.created_at",
    )

    __mapper_args__ = {"version_id_col": version_id}


class TrafficAllocation(Base):
    __tablename__ = "traffic_allocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id"), unique=True, nullable=False
    )
    # [{"version": "v1", "weight": 0.9}, {"version": "v2-good", "weight": 0.1}, ...]
    targets: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, onupdate=_utcnow
    )

    deployment: Mapped["Deployment"] = relationship(back_populates="traffic_allocation")


class DeploymentEvent(Base):
    __tablename__ = "deployment_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(ForeignKey("deployments.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow
    )

    deployment: Mapped["Deployment"] = relationship(back_populates="events")


class PolicyEvaluationResult(enum.StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    # No verdict is possible - e.g. not enough traffic yet, or (for recall) no
    # actual_label has been backfilled. Deliberately distinct from FAIL: a policy
    # engine or human must never treat "couldn't tell" as "looked fine".
    INCONCLUSIVE = "INCONCLUSIVE"


class PolicyEvaluation(Base):
    """One row per individual policy check from a single POST .../evaluate call
    (e.g. "minimum_requests", "latency_p95_increase", "max_error_rate",
    "minimum_recall") - never an aggregate. The overall PASS/FAIL/INCONCLUSIVE verdict
    is derived from these rows (see app/policy/engine.py's overall_result), not stored
    separately.
    """

    __tablename__ = "policy_evaluations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id"), nullable=False, index=True
    )
    policy_name: Mapped[str] = mapped_column(String(100), nullable=False)
    metric_name: Mapped[str] = mapped_column(String(100), nullable=False)
    observed_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    result: Mapped[PolicyEvaluationResult] = mapped_column(
        Enum(PolicyEvaluationResult, native_enum=False, length=16), nullable=False
    )
    evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, index=True
    )
    # Snapshot of the deployment's own context AT THE MOMENT this check ran - not
    # derivable after the fact from the deployment's *current* state, which can
    # (and does) change later. Without this, a human reading an old PolicyEvaluation
    # after the deployment moved on would see an explanation computed from whatever
    # traffic split happens to be live *now*, silently misdescribing what was
    # actually true when the check fired - see app/policy/explain.py and
    # app/control_plane/timeline.py. All nullable: rows written before this column
    # existed have no snapshot, and explain.py falls back to current-state
    # estimation for those (flagged as estimated, never silently treated as exact).
    evaluation_window_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stable_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    canary_weight: Mapped[float | None] = mapped_column(Float, nullable=True)
    stable_sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canary_sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Quality checks (minimum_labeled_samples, minimum_label_coverage,
    # minimum_recall) read a *different*, older window than reliability checks -
    # see app/policy/engine.py and docs/DESIGN_NOTES.md for why (labels arrive
    # delayed, so the freshest window is definitionally the least-labeled one).
    # Snapshotted here for the same audit-accuracy reason as the fields above: an
    # old check's timeline explanation must describe the window it actually used,
    # not one recomputed from the deployment's current policy_config. All nullable
    # for the same pre-migration-row reason as the fields above.
    label_maturity_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_window_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    quality_window_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    labeled_sample_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    label_coverage: Mapped[float | None] = mapped_column(Float, nullable=True)
    # How many of `labeled_sample_count` are the positive class (actual_label=1) -
    # recall's denominator (TP+FN) is positives, not all labeled samples, so a
    # dataset with a low positive rate can clear minimum_labeled_samples/
    # minimum_label_coverage while still resting on 1-3 positive examples, making
    # recall statistically meaningless. See app/policy/engine.py's
    # minimum_positive_labels gate and docs/DESIGN_NOTES.md.
    positive_label_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class BenchmarkRunStatus(enum.StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class BenchmarkRun(Base):
    """One row per dashboard-triggered `scripts.benchmarks.run_benchmark` subprocess
    (see app/benchmarks/service.py). Deliberately independent of Deployment - a
    benchmark run creates its own isolated Deployment internally (model_name=
    "benchmark-<scenario>"), but this row is what the dashboard polls for
    RUNNING/COMPLETED/FAILED status and, once finished, the parsed report.
    """

    __tablename__ = "benchmark_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    scenario: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[BenchmarkRunStatus] = mapped_column(
        Enum(BenchmarkRunStatus, native_enum=False, length=16),
        nullable=False,
        default=BenchmarkRunStatus.RUNNING,
    )
    # The full Sprint-9 BenchmarkResult JSON (see scripts/benchmarks/report.py),
    # populated once the subprocess exits 0. None while RUNNING or if it FAILED
    # before producing a report.
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    # Subprocess stderr/stdout tail, populated only on FAILED, for debugging without
    # needing to dig through container logs.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PredictionMetric(Base):
    """One row per /router/predict forward. Written on the hot path (router fires one
    of these after every forward) - deliberately has no relationship back to
    Deployment and no join is needed to insert one; reads (aggregation) filter by
    deployment_id + model_version + a time window instead.
    """

    __tablename__ = "prediction_metrics"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.id"), nullable=False, index=True
    )
    model_version: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    prediction: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actual_label: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Born exactly once, in app/serving/ (a UUID4 minted per /predict call, returned
    # in the response body) - the control plane and router never generate one, they
    # only ever carry it through. This is the join key a delayed ground-truth label
    # (POST /api/labels, see app/control_plane/labels_api.py) uses to find its way
    # back to the specific prediction it's labeling - see docs/DESIGN_NOTES.md for
    # why the id has to be born at the point of prediction and not, say, assigned by
    # the router or the control plane on ingest. Nullable because rows written
    # before this column existed have none; unique because it's a join key, not a
    # descriptive attribute - two rows can never legitimately share one.
    prediction_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    # When actual_label was actually populated (whichever of the two paths got
    # there first - see metrics_service.record_metric and labels_api.py). Distinct
    # from `created_at` (when the *prediction* happened): the gap between them is
    # exactly the real, measurable label delay this sprint exists to make visible
    # (see metrics_service.compute_version_summary's label_delay_p50/p95_seconds).
    label_ingested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow, index=True
    )


class PendingLabel(Base):
    """A ground-truth label that arrived (POST /api/labels) *before* the
    PredictionMetric row it belongs to did - possible because the router publishes
    metrics fire-and-forget (see docs/DESIGN_NOTES.md#metrics) while a label feeder
    reports ground truth independently and asynchronously; there's no ordering
    guarantee between the two. Matched and consumed the moment a PredictionMetric
    with the same prediction_id is actually written (metrics_service.record_metric)
    - never by a separate polling/background task, since that write is already the
    one and only place a match becomes possible. A row that's never claimed just
    sits here harmlessly (see Known limitations: no retention policy in this
    sprint).
    """

    __tablename__ = "pending_labels"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_id)
    prediction_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True, index=True)
    actual_label: Mapped[int] = mapped_column(Integer, nullable=False)
    # When the label actually happened in the real world, per whoever's reporting it
    # (a label feeder that just picked a known-answer row knows this exactly) -
    # distinct from `ingested_at` (when *this server* received it). Only the latter
    # feeds the label-delay metric (see PredictionMetric.label_ingested_at); this is
    # kept for audit/debugging, not computed against anywhere yet.
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), default=_utcnow
    )
