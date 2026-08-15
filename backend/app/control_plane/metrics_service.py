import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.models import GroundTruthLabel, PredictionMetric
from app.control_plane.schemas import MetricIn, MetricsDeltas, MetricsSummary


def record_metric(db: Session, deployment_id: str, payload: MetricIn) -> None:
    """Writes one PredictionMetric row. Nothing else: ground truth is a
    completely independent write (see label_service.ingest_label and
    GroundTruthLabel's docstring) that this function no longer needs to check
    for - compute_version_summary below joins the two tables together at read
    time instead, which is what makes label arrival order irrelevant.
    """
    metric = PredictionMetric(
        deployment_id=deployment_id,
        model_version=payload.model_version,
        latency_ms=payload.latency_ms,
        status_code=payload.status_code,
        prediction=payload.prediction,
        prediction_id=payload.prediction_id,
    )
    db.add(metric)
    db.commit()


def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear-interpolation percentile (same convention as numpy.percentile's
    default), computed in Python since SQLite has no percentile_cont/window function
    equivalent."""
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_values[int(k)]
    return sorted_values[f] * (c - k) + sorted_values[c] * (k - f)


def compute_version_summary(
    db: Session,
    deployment_id: str,
    version: str,
    window_seconds: int,
    *,
    window_end_offset_seconds: int = 0,
) -> MetricsSummary:
    """Summarizes PredictionMetric rows in [now - offset - window, now - offset],
    joined against GroundTruthLabel on prediction_id for anything quality-related
    (labeled_sample_count, label_coverage, positive_label_count, precision,
    recall, false_positive_rate, label delay) - see GroundTruthLabel's docstring
    for why this is a read-time JOIN rather than a value copied onto
    PredictionMetric at write time.

    `window_end_offset_seconds` (default 0, i.e. the window ends *now*) is what
    lets the policy engine read two genuinely different windows from the same
    function: reliability checks want the freshest data (offset=0), quality checks
    want an *older* window shifted back by the deployment's label_maturity_seconds,
    since labels arrive delayed and the freshest window is definitionally the
    least-labeled one - see app/policy/engine.py and docs/DESIGN_NOTES.md.
    """
    window_end = datetime.now(UTC) - timedelta(seconds=window_end_offset_seconds)
    window_start = window_end - timedelta(seconds=window_seconds)
    stmt = (
        select(PredictionMetric, GroundTruthLabel)
        .outerjoin(
            GroundTruthLabel, GroundTruthLabel.prediction_id == PredictionMetric.prediction_id
        )
        .where(
            PredictionMetric.deployment_id == deployment_id,
            PredictionMetric.model_version == version,
            PredictionMetric.created_at >= window_start,
            PredictionMetric.created_at <= window_end,
        )
    )
    rows = list(db.execute(stmt).all())

    sample_count = len(rows)
    if sample_count == 0:
        return MetricsSummary(
            version=version,
            sample_count=0,
            p50_latency_ms=None,
            p95_latency_ms=None,
            p99_latency_ms=None,
            error_rate=None,
            precision=None,
            recall=None,
            false_positive_rate=None,
            labeled_sample_count=0,
            label_coverage=None,
            positive_label_count=0,
            label_delay_p50_seconds=None,
            label_delay_p95_seconds=None,
        )

    latencies = sorted(metric.latency_ms for metric, _label in rows)
    error_count = sum(1 for metric, _label in rows if metric.status_code >= 400)

    labeled_rows = [(metric, label) for metric, label in rows if label is not None]
    labeled_sample_count = len(labeled_rows)
    label_coverage = labeled_sample_count / sample_count

    label_delays = sorted(
        (label.ingested_at - metric.created_at).total_seconds() for metric, label in labeled_rows
    )
    label_delay_p50 = _percentile(label_delays, 50) if label_delays else None
    label_delay_p95 = _percentile(label_delays, 95) if label_delays else None

    labeled = [
        (metric.prediction, label.actual_label)
        for metric, label in labeled_rows
        if metric.prediction is not None
    ]
    precision = recall = false_positive_rate = None
    positive_label_count = 0
    if labeled:
        true_positives = sum(1 for p, a in labeled if p == 1 and a == 1)
        false_positives = sum(1 for p, a in labeled if p == 1 and a == 0)
        false_negatives = sum(1 for p, a in labeled if p == 0 and a == 1)
        true_negatives = sum(1 for p, a in labeled if p == 0 and a == 0)

        pred_positive = true_positives + false_positives
        actual_positive = true_positives + false_negatives
        actual_negative = false_positives + true_negatives

        precision = true_positives / pred_positive if pred_positive > 0 else None
        recall = true_positives / actual_positive if actual_positive > 0 else None
        false_positive_rate = (
            false_positives / actual_negative if actual_negative > 0 else None
        )
        # recall's real denominator (TP+FN) - see MetricsSummary.positive_label_count.
        positive_label_count = actual_positive

    return MetricsSummary(
        version=version,
        sample_count=sample_count,
        p50_latency_ms=_percentile(latencies, 50),
        p95_latency_ms=_percentile(latencies, 95),
        p99_latency_ms=_percentile(latencies, 99),
        error_rate=error_count / sample_count,
        precision=precision,
        recall=recall,
        false_positive_rate=false_positive_rate,
        labeled_sample_count=labeled_sample_count,
        label_coverage=label_coverage,
        positive_label_count=positive_label_count,
        label_delay_p50_seconds=label_delay_p50,
        label_delay_p95_seconds=label_delay_p95,
    )


def compute_deltas(stable: MetricsSummary, canary: MetricsSummary) -> MetricsDeltas:
    """canary - stable, per metric. None if either side lacks the data."""

    def _delta(a: float | None, b: float | None) -> float | None:
        return a - b if a is not None and b is not None else None

    return MetricsDeltas(
        p95_latency_ms=_delta(canary.p95_latency_ms, stable.p95_latency_ms),
        error_rate=_delta(canary.error_rate, stable.error_rate),
        recall=_delta(canary.recall, stable.recall),
    )
