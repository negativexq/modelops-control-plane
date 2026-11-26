import math
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.models import GroundTruthLabel, PredictionMetric
from app.control_plane.schemas import (
    MetricIn,
    MetricsBucket,
    MetricsDeltas,
    MetricsSummary,
    MetricsTimeseriesOut,
    VersionSeries,
)


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
        features=payload.features,
        artifact=payload.artifact,
    )
    db.add(metric)
    db.commit()


def _is_answered(status_code: int) -> bool:
    """Latency percentiles are computed over successful responses only. Failures
    are the error-rate check's job, and their "latency" says nothing about how
    fast the version serves: a request the gateway failed fast because the
    version's circuit breaker is open is reported as a 503 after ~0 ms, so
    counting it would make a canary that is down look *faster* than stable."""
    return status_code < 400


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
    now: datetime | None = None,
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
    window_end = (now or datetime.now(UTC)) - timedelta(seconds=window_end_offset_seconds)
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

    latencies = sorted(
        metric.latency_ms for metric, _label in rows if _is_answered(metric.status_code)
    )
    error_count = sum(1 for metric, _label in rows if not _is_answered(metric.status_code))

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
        p50_latency_ms=_percentile(latencies, 50) if latencies else None,
        p95_latency_ms=_percentile(latencies, 95) if latencies else None,
        p99_latency_ms=_percentile(latencies, 99) if latencies else None,
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


def window_latencies(
    db: Session, deployment_id: str, version: str, window_seconds: int, *, now: datetime
) -> list[float]:
    """Raw latencies (ms) of successful responses for one version in
    [now - window, now] - the same samples compute_version_summary(..., now=now)
    takes its percentiles from (see _is_answered). The policy engine's latency
    check bootstraps over these (see app/policy/stats.py)."""
    stmt = select(PredictionMetric.latency_ms).where(
        PredictionMetric.deployment_id == deployment_id,
        PredictionMetric.model_version == version,
        PredictionMetric.created_at >= now - timedelta(seconds=window_seconds),
        PredictionMetric.created_at <= now,
        PredictionMetric.status_code < 400,
    )
    return [float(v) for v in db.execute(stmt).scalars().all()]


def window_inputs(
    db: Session, deployment_id: str, version: str, window_seconds: int, *, now: datetime
) -> list[tuple[str, dict[str, Any]]]:
    """(artifact, features) of every answered prediction for one version in
    [now - window, now] that recorded its inputs - what the drift check
    compares to the artifact's training profile (app/policy/drift.py)."""
    stmt = select(PredictionMetric.artifact, PredictionMetric.features).where(
        PredictionMetric.deployment_id == deployment_id,
        PredictionMetric.model_version == version,
        PredictionMetric.created_at >= now - timedelta(seconds=window_seconds),
        PredictionMetric.created_at <= now,
        PredictionMetric.status_code < 400,
        PredictionMetric.features.is_not(None),
        PredictionMetric.artifact.is_not(None),
    )
    return [(artifact, features) for artifact, features in db.execute(stmt).tuples().all()]


def compute_deltas(stable: MetricsSummary, canary: MetricsSummary) -> MetricsDeltas:
    """canary - stable, per metric. None if either side lacks the data."""

    def _delta(a: float | None, b: float | None) -> float | None:
        return a - b if a is not None and b is not None else None

    return MetricsDeltas(
        p95_latency_ms=_delta(canary.p95_latency_ms, stable.p95_latency_ms),
        error_rate=_delta(canary.error_rate, stable.error_rate),
        recall=_delta(canary.recall, stable.recall),
    )


# Bucket sizes a timeseries may use; the smallest one that keeps the series at or
# under MAX_BUCKETS points wins, so a 2-minute rollout and a 2-day one both chart.
BUCKET_CHOICES_SECONDS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600)
MAX_BUCKETS = 120


def choose_bucket_seconds(span_seconds: float) -> int:
    for size in BUCKET_CHOICES_SECONDS:
        if span_seconds / size <= MAX_BUCKETS:
            return size
    return BUCKET_CHOICES_SECONDS[-1]


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def compute_timeseries(
    db: Session,
    deployment_id: str,
    versions: list[tuple[str, Literal["stable", "canary"]]],
    start: datetime,
    end: datetime,
) -> MetricsTimeseriesOut:
    """Latency percentiles and error rate per version per time bucket, over
    [start, end]. Empty buckets are kept (count=0, values None) so a chart shows
    gaps as gaps instead of drawing a line across them."""
    start, end = as_utc(start), as_utc(end)
    if end <= start:
        end = start + timedelta(seconds=1)
    bucket_seconds = choose_bucket_seconds((end - start).total_seconds())
    bucket_count = math.ceil((end - start).total_seconds() / bucket_seconds)

    rows = db.execute(
        select(
            PredictionMetric.model_version,
            PredictionMetric.created_at,
            PredictionMetric.latency_ms,
            PredictionMetric.status_code,
        ).where(
            PredictionMetric.deployment_id == deployment_id,
            PredictionMetric.created_at >= start,
            PredictionMetric.created_at <= end,
        )
    ).all()

    grouped: dict[tuple[str, int], list[tuple[float, int]]] = {}
    for version, created_at, latency_ms, status_code in rows:
        index = int((as_utc(created_at) - start).total_seconds() // bucket_seconds)
        index = min(max(index, 0), bucket_count - 1)
        grouped.setdefault((version, index), []).append((latency_ms, status_code))

    series: list[VersionSeries] = []
    for version, role in versions:
        buckets: list[MetricsBucket] = []
        for index in range(bucket_count):
            samples = grouped.get((version, index), [])
            latencies = sorted(latency for latency, code in samples if _is_answered(code))
            buckets.append(
                MetricsBucket(
                    t=start + timedelta(seconds=index * bucket_seconds),
                    count=len(samples),
                    error_rate=(
                        sum(1 for _, code in samples if not _is_answered(code)) / len(samples)
                        if samples
                        else None
                    ),
                    p50_latency_ms=_percentile(latencies, 50) if latencies else None,
                    p95_latency_ms=_percentile(latencies, 95) if latencies else None,
                )
            )
        series.append(VersionSeries(version=version, role=role, buckets=buckets))

    return MetricsTimeseriesOut(
        deployment_id=deployment_id,
        start=start,
        end=end,
        bucket_seconds=bucket_seconds,
        series=series,
    )
