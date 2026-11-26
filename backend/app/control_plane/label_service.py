"""Ground-truth label ingestion (POST /api/labels[, /batch] - see labels_api.py).

A label is always written to GroundTruthLabel, unconditionally - never
check-then-act against whether a matching PredictionMetric exists yet. That's
the whole point of the Sprint 14 redesign (see GroundTruthLabel's own
docstring in models.py): label ingestion and metric ingestion are two
completely independent writers with no ordering guarantee between them (the
router publishes metrics fire-and-forget while a label feeder reports ground
truth on its own delayed schedule - see docs/DESIGN_NOTES.md), and the
pre-Sprint-14 design (park an unmatched label in PendingLabel, consumed only
by metrics_service.record_metric) had a real race: interleave the two writes
so each transaction's "does the other side exist yet" check misses the
other's not-yet-committed row, and the label and its metric never get linked -
metrics_service.compute_version_summary now closes that gap by joining the
two tables at *read* time instead of copying a value across at write time, so
there's nothing left for arrival order to break.

"The same prediction_id reporting the same actual_label twice" is a no-op (a
feeder retry, or a batch resent after a partial failure), never an error;
"the same prediction_id reporting two *different* labels" is treated as a
genuine data-integrity problem, not silently overwritten.
"""

import enum
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.control_plane.models import DeploymentEvent, GroundTruthLabel, PredictionMetric


class LabelIngestOutcome(enum.StrEnum):
    APPLIED = "applied"  # 201 - first time seen, a matching PredictionMetric already exists
    NO_OP = "no_op"  # 200 - idempotent repeat of a label already recorded
    # 202 - first time seen, but no matching PredictionMetric yet. Purely advisory:
    # unlike the pre-Sprint-14 PendingLabel design, nothing here is "parked" waiting
    # to be matched later - the label is already durably recorded and will be
    # picked up by the next read that joins GroundTruthLabel against
    # PredictionMetric, whenever that metric actually lands.
    PENDING = "pending"


class LabelConflictError(Exception):
    """Raised when `prediction_id` already has a *different* actual_label recorded -
    a real data-integrity problem (two feeders disagreeing about the same
    prediction's ground truth, or a feeder bug re-sending a corrected value under
    the same id), not something to silently overwrite. `deployment_id` is None
    when no PredictionMetric has landed for this prediction_id yet (so there's no
    deployment to know this belongs to) - callers should only write a
    DeploymentEvent audit record when it's not None.
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


def _find_metric(db: Session, prediction_id: str) -> PredictionMetric | None:
    return db.execute(
        select(PredictionMetric).where(PredictionMetric.prediction_id == prediction_id)
    ).scalar_one_or_none()


def _resolve_existing(
    db: Session, existing: GroundTruthLabel, prediction_id: str, actual_label: int
) -> LabelIngestOutcome:
    if existing.actual_label == actual_label:
        return LabelIngestOutcome.NO_OP
    metric = _find_metric(db, prediction_id)
    conflict = LabelConflictError(
        prediction_id, existing.actual_label, actual_label, metric.deployment_id if metric else None
    )
    if metric is not None:
        db.add(
            DeploymentEvent(
                deployment_id=metric.deployment_id,
                event_type="label_conflict",
                message=(
                    f"label conflict for prediction_id={prediction_id}: already "
                    f"recorded actual_label={existing.actual_label}, rejected new "
                    f"value {actual_label}"
                ),
            )
        )
        db.commit()
    raise conflict


def ingest_label(
    db: Session, prediction_id: str, actual_label: int, occurred_at: datetime
) -> LabelIngestOutcome:
    existing = db.execute(
        select(GroundTruthLabel).where(GroundTruthLabel.prediction_id == prediction_id)
    ).scalar_one_or_none()
    if existing is not None:
        return _resolve_existing(db, existing, prediction_id, actual_label)

    db.add(
        GroundTruthLabel(
            prediction_id=prediction_id, actual_label=actual_label, occurred_at=occurred_at
        )
    )
    try:
        db.commit()
    except IntegrityError:
        # Lost a race: another writer's INSERT for the same prediction_id
        # committed first (the unique constraint on GroundTruthLabel.
        # prediction_id is what actually makes this race-safe under two real,
        # independent DB connections - the SELECT above can't see an
        # uncommitted concurrent insert). Re-check exactly like the "already
        # existed" branch above - this is a no-op or a genuine conflict, not a
        # server error.
        db.rollback()
        existing = db.execute(
            select(GroundTruthLabel).where(GroundTruthLabel.prediction_id == prediction_id)
        ).scalar_one_or_none()
        if existing is None:
            raise  # not the race we expected - surface the original error
        return _resolve_existing(db, existing, prediction_id, actual_label)

    metric = _find_metric(db, prediction_id)
    return LabelIngestOutcome.APPLIED if metric is not None else LabelIngestOutcome.PENDING


# SQLite's bound-parameter limit is far above this; it just keeps each IN (...) sane.
_LOOKUP_CHUNK = 500


def _chunks(values: list[str]) -> list[list[str]]:
    return [values[i : i + _LOOKUP_CHUNK] for i in range(0, len(values), _LOOKUP_CHUNK)]


def ingest_labels(
    db: Session, labels: list[tuple[str, int, datetime]]
) -> list[LabelIngestOutcome | LabelConflictError]:
    """ingest_label for a whole batch of (prediction_id, actual_label, occurred_at),
    in one transaction: two lookups and one commit instead of a SELECT, INSERT,
    COMMIT and metric lookup per label. Per-label commits capped a batch of 50 at
    ~150 ms on an idle host, and a busy one (the router writing a metric per
    prediction into the same SQLite file) fell behind a benchmark's ~80 labels/s:
    labels landed after the quality window had moved past their predictions.

    Outcomes match calling ingest_label once per item, in order - including a
    prediction_id that appears twice in the same batch. If a concurrent writer
    inserts one of these prediction_ids first, the batch is retried item by item,
    which resolves that race exactly like the single-label path does."""
    ids = list(dict.fromkeys(prediction_id for prediction_id, _label, _at in labels))
    known: dict[str, int] = {}
    deployment_for: dict[str, str] = {}
    for chunk in _chunks(ids):
        known.update(
            db.execute(
                select(GroundTruthLabel.prediction_id, GroundTruthLabel.actual_label).where(
                    GroundTruthLabel.prediction_id.in_(chunk)
                )
            )
            .tuples()
            .all()
        )
        metrics = db.execute(
            select(PredictionMetric.prediction_id, PredictionMetric.deployment_id).where(
                PredictionMetric.prediction_id.in_(chunk)
            )
        ).tuples()
        deployment_for.update(
            (prediction_id, deployment_id)
            for prediction_id, deployment_id in metrics
            if prediction_id is not None
        )

    results: list[LabelIngestOutcome | LabelConflictError] = []
    for prediction_id, actual_label, occurred_at in labels:
        deployment_id = deployment_for.get(prediction_id)
        if prediction_id in known:
            existing_label = known[prediction_id]
            if existing_label == actual_label:
                results.append(LabelIngestOutcome.NO_OP)
                continue
            if deployment_id is not None:
                db.add(
                    DeploymentEvent(
                        deployment_id=deployment_id,
                        event_type="label_conflict",
                        message=(
                            f"label conflict for prediction_id={prediction_id}: already "
                            f"recorded actual_label={existing_label}, rejected new "
                            f"value {actual_label}"
                        ),
                    )
                )
            results.append(
                LabelConflictError(prediction_id, existing_label, actual_label, deployment_id)
            )
            continue
        db.add(
            GroundTruthLabel(
                prediction_id=prediction_id, actual_label=actual_label, occurred_at=occurred_at
            )
        )
        known[prediction_id] = actual_label
        results.append(
            LabelIngestOutcome.APPLIED if deployment_id is not None else LabelIngestOutcome.PENDING
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return [_ingest_or_conflict(db, *label) for label in labels]
    return results


def _ingest_or_conflict(
    db: Session, prediction_id: str, actual_label: int, occurred_at: datetime
) -> LabelIngestOutcome | LabelConflictError:
    try:
        return ingest_label(db, prediction_id, actual_label, occurred_at)
    except LabelConflictError as exc:
        return exc
