"""Ground-truth label ingestion (POST /api/labels[, /batch] - see labels_api.py).

Two arrival orders are possible, since a label feeder and the router publish
completely independently of each other (see docs/DESIGN_NOTES.md):

- The PredictionMetric this label belongs to already exists (the common case -
  metrics are written on the hot path, labels arrive later on purpose). The label
  is applied directly to that row, right here.
- The PredictionMetric doesn't exist yet (a label feeder that's unusually fast, or
  a metric that's unusually slow/lost). The label is parked in PendingLabel and
  picked up later by metrics_service.record_metric, the only other place a match
  can happen - see that module and PendingLabel's own docstring.

Either way, "the same prediction_id reporting the same actual_label twice" is a
no-op (a feeder retry, or a batch resent after a partial failure), never an error;
"the same prediction_id reporting two *different* labels" is treated as a genuine
data-integrity problem, not silently overwritten.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.models import DeploymentEvent, PendingLabel, PredictionMetric


class LabelIngestOutcome(enum.StrEnum):
    APPLIED = "applied"  # 201 - first time seen, written straight onto a PredictionMetric
    NO_OP = "no_op"  # 200 - idempotent repeat of a label already recorded (or still pending)
    PENDING = "pending"  # 202 - no matching PredictionMetric yet; parked in PendingLabel


class LabelConflictError(Exception):
    """Raised when `prediction_id` already has a *different* actual_label recorded
    (whether on a PredictionMetric or still in PendingLabel) - a real data-integrity
    problem (two feeders disagreeing about the same prediction's ground truth, or a
    feeder bug re-sending a corrected value under the same id), not something to
    silently overwrite. `deployment_id` is None when the conflict is against a
    still-pending label (no PredictionMetric exists yet to know which deployment
    this even belongs to) - callers should only write a DeploymentEvent audit
    record when it's not None.
    """

    def __init__(
        self, prediction_id: str, existing_label: int, new_label: int, deployment_id: str | None
    ) -> None:
        self.prediction_id = prediction_id
        self.existing_label = existing_label
        self.new_label = new_label
        self.deployment_id = deployment_id
        super().__init__(
            f"prediction_id '{prediction_id}' already has actual_label="
            f"{existing_label} recorded; refusing to overwrite with {new_label}"
        )


def ingest_label(
    db: Session, prediction_id: str, actual_label: int, occurred_at: datetime
) -> LabelIngestOutcome:
    metric = db.execute(
        select(PredictionMetric).where(PredictionMetric.prediction_id == prediction_id)
    ).scalar_one_or_none()

    if metric is not None:
        if metric.actual_label is None:
            metric.actual_label = actual_label
            metric.label_ingested_at = datetime.now(UTC)
            db.commit()
            return LabelIngestOutcome.APPLIED
        if metric.actual_label == actual_label:
            return LabelIngestOutcome.NO_OP
        conflict = LabelConflictError(
            prediction_id, metric.actual_label, actual_label, metric.deployment_id
        )
        db.add(
            DeploymentEvent(
                deployment_id=metric.deployment_id,
                event_type="label_conflict",
                message=(
                    f"label conflict for prediction_id={prediction_id}: already "
                    f"recorded actual_label={metric.actual_label}, rejected new "
                    f"value {actual_label}"
                ),
            )
        )
        db.commit()
        raise conflict

    pending = db.execute(
        select(PendingLabel).where(PendingLabel.prediction_id == prediction_id)
    ).scalar_one_or_none()
    if pending is not None:
        if pending.actual_label == actual_label:
            return LabelIngestOutcome.NO_OP
        # No PredictionMetric exists yet, so there's no deployment to attach an
        # audit event to - see LabelConflictError's docstring.
        raise LabelConflictError(prediction_id, pending.actual_label, actual_label, None)

    db.add(
        PendingLabel(
            prediction_id=prediction_id, actual_label=actual_label, occurred_at=occurred_at
        )
    )
    db.commit()
    return LabelIngestOutcome.PENDING
