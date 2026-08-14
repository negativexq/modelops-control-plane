from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from app.control_plane import metrics_service
from app.control_plane.models import Deployment, DeploymentStatus, PredictionMetric
from app.control_plane.schemas import MetricsSummary


@pytest.fixture
def deployment(db_session: Session) -> Deployment:
    d = Deployment(
        model_name="fraud-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.CANARY_RUNNING,
    )
    db_session.add(d)
    db_session.commit()
    db_session.refresh(d)
    return d


def _add_metric(
    db_session: Session,
    deployment_id: str,
    version: str,
    latency_ms: float,
    status_code: int = 200,
    prediction: int | None = None,
    actual_label: int | None = None,
    age_seconds: float = 0,
) -> None:
    db_session.add(
        PredictionMetric(
            deployment_id=deployment_id,
            model_version=version,
            latency_ms=latency_ms,
            status_code=status_code,
            prediction=prediction,
            actual_label=actual_label,
            created_at=datetime.now(UTC) - timedelta(seconds=age_seconds),
        )
    )
    db_session.commit()


def test_compute_version_summary_p50_p95_p99_known_values(
    db_session: Session, deployment: Deployment
) -> None:
    # 10, 20, ..., 100 -> numpy.percentile(v, 50/95/99) == 55 / 95.5 / 99.1
    for latency in range(10, 101, 10):
        _add_metric(db_session, deployment.id, "v1", latency_ms=float(latency))

    summary = metrics_service.compute_version_summary(db_session, deployment.id, "v1", 3600)

    assert summary.sample_count == 10
    assert summary.p50_latency_ms == pytest.approx(55.0)
    assert summary.p95_latency_ms == pytest.approx(95.5)
    assert summary.p99_latency_ms == pytest.approx(99.1)


def test_compute_version_summary_single_sample(
    db_session: Session, deployment: Deployment
) -> None:
    _add_metric(db_session, deployment.id, "v1", latency_ms=42.0)
    summary = metrics_service.compute_version_summary(db_session, deployment.id, "v1", 3600)
    assert summary.p50_latency_ms == summary.p95_latency_ms == summary.p99_latency_ms == 42.0


def test_compute_version_summary_no_samples_returns_nones(
    db_session: Session, deployment: Deployment
) -> None:
    summary = metrics_service.compute_version_summary(db_session, deployment.id, "v1", 3600)
    assert summary.sample_count == 0
    assert summary.p50_latency_ms is None
    assert summary.error_rate is None
    assert summary.precision is None


def test_compute_version_summary_window_filters_old_rows(
    db_session: Session, deployment: Deployment
) -> None:
    _add_metric(db_session, deployment.id, "v1", latency_ms=1000, age_seconds=600)
    _add_metric(db_session, deployment.id, "v1", latency_ms=10, age_seconds=1)

    summary = metrics_service.compute_version_summary(db_session, deployment.id, "v1", 60)
    assert summary.sample_count == 1
    assert summary.p50_latency_ms == pytest.approx(10)


def test_compute_version_summary_separates_by_version(
    db_session: Session, deployment: Deployment
) -> None:
    _add_metric(db_session, deployment.id, "v1", latency_ms=10)
    _add_metric(db_session, deployment.id, "v2-good", latency_ms=999)

    stable_summary = metrics_service.compute_version_summary(db_session, deployment.id, "v1", 3600)
    assert stable_summary.sample_count == 1
    assert stable_summary.p50_latency_ms == pytest.approx(10)


def test_compute_version_summary_separates_by_deployment(
    db_session: Session, deployment: Deployment
) -> None:
    other = Deployment(
        # Different model_name than the `deployment` fixture - only deployment_id
        # isolation is under test here, but two simultaneously non-terminal
        # deployments for the *same* model_name now violates a DB-level invariant
        # (uq_deployments_active_per_model - see app/control_plane/models.py).
        model_name="other-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.CANARY_RUNNING,
    )
    db_session.add(other)
    db_session.commit()
    db_session.refresh(other)

    _add_metric(db_session, deployment.id, "v1", latency_ms=10)
    _add_metric(db_session, other.id, "v1", latency_ms=999)

    summary = metrics_service.compute_version_summary(db_session, deployment.id, "v1", 3600)
    assert summary.sample_count == 1
    assert summary.p50_latency_ms == pytest.approx(10)


def test_error_rate_counts_4xx_and_5xx(db_session: Session, deployment: Deployment) -> None:
    _add_metric(db_session, deployment.id, "v1", latency_ms=1, status_code=200)
    _add_metric(db_session, deployment.id, "v1", latency_ms=1, status_code=422)
    _add_metric(db_session, deployment.id, "v1", latency_ms=1, status_code=500)
    _add_metric(db_session, deployment.id, "v1", latency_ms=1, status_code=503)

    summary = metrics_service.compute_version_summary(db_session, deployment.id, "v1", 3600)
    assert summary.error_rate == pytest.approx(0.75)


def test_compute_deltas_is_canary_minus_stable() -> None:
    stable = MetricsSummary(
        version="v1",
        sample_count=10,
        p50_latency_ms=10,
        p95_latency_ms=20,
        p99_latency_ms=30,
        error_rate=0.01,
        precision=0.8,
        recall=0.7,
        false_positive_rate=0.02,
    )
    canary = MetricsSummary(
        version="v2-good",
        sample_count=10,
        p50_latency_ms=15,
        p95_latency_ms=25,
        p99_latency_ms=35,
        error_rate=0.02,
        precision=0.9,
        recall=0.85,
        false_positive_rate=0.01,
    )

    deltas = metrics_service.compute_deltas(stable, canary)
    assert deltas.p95_latency_ms == pytest.approx(5)
    assert deltas.error_rate == pytest.approx(0.01)
    assert deltas.recall == pytest.approx(0.15)


def test_compute_deltas_none_when_either_side_missing() -> None:
    empty = MetricsSummary(
        version="v1",
        sample_count=0,
        p50_latency_ms=None,
        p95_latency_ms=None,
        p99_latency_ms=None,
        error_rate=None,
        precision=None,
        recall=None,
        false_positive_rate=None,
    )
    populated = MetricsSummary(
        version="v2-good",
        sample_count=1,
        p50_latency_ms=10,
        p95_latency_ms=10,
        p99_latency_ms=10,
        error_rate=0.0,
        precision=1.0,
        recall=1.0,
        false_positive_rate=0.0,
    )

    deltas = metrics_service.compute_deltas(empty, populated)
    assert deltas.p95_latency_ms is None
    assert deltas.error_rate is None
    assert deltas.recall is None
