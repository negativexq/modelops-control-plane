import math
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.models import PredictionMetric
from app.control_plane.schemas import MetricIn, MetricsDeltas, MetricsSummary


def record_metric(db: Session, deployment_id: str, payload: MetricIn) -> None:
    db.add(
        PredictionMetric(
            deployment_id=deployment_id,
            model_version=payload.model_version,
            latency_ms=payload.latency_ms,
            status_code=payload.status_code,
            prediction=payload.prediction,
            actual_label=payload.actual_label,
        )
    )
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
    db: Session, deployment_id: str, version: str, window_seconds: int
) -> MetricsSummary:
    cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
    stmt = select(PredictionMetric).where(
        PredictionMetric.deployment_id == deployment_id,
        PredictionMetric.model_version == version,
        PredictionMetric.created_at >= cutoff,
    )
    rows = list(db.execute(stmt).scalars().all())

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
        )

    latencies = sorted(row.latency_ms for row in rows)
    error_count = sum(1 for row in rows if row.status_code >= 400)

    labeled = [
        (row.prediction, row.actual_label)
        for row in rows
        if row.prediction is not None and row.actual_label is not None
    ]
    precision = recall = false_positive_rate = None
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
