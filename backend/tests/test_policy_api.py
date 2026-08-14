from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.models import Deployment, DeploymentStatus, PredictionMetric
from app.db import get_db
from app.main import app


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


def _add_metrics(
    db_session: Session,
    deployment_id: str,
    version: str,
    count: int,
    latency_ms: float = 20.0,
    status_code: int = 200,
    recall_labels: tuple[int, int] | None = None,
) -> None:
    """Insert `count` metric rows. If recall_labels=(prediction, actual_label), every
    row gets that same (prediction, actual_label) pair - just enough to make recall
    computable for tests that need it."""
    prediction, actual_label = recall_labels if recall_labels else (None, None)
    for _ in range(count):
        db_session.add(
            PredictionMetric(
                deployment_id=deployment_id,
                model_version=version,
                latency_ms=latency_ms,
                status_code=status_code,
                prediction=prediction,
                actual_label=actual_label,
                created_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    db_session.commit()


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def test_evaluate_returns_inconclusive_when_not_enough_traffic(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=5)
    _add_metrics(db_session, deployment.id, "v2-good", count=5)

    response = client.post(
        f"/api/deployments/{deployment.id}/evaluate",
        json={"minimum_requests": 100},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["overall_result"] == "INCONCLUSIVE"
    assert len(body["checks"]) == 1
    assert body["checks"][0]["policy_name"] == "minimum_requests"


def test_evaluate_records_all_checks_when_traffic_sufficient(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=20, latency_ms=20.0)
    _add_metrics(db_session, deployment.id, "v2-good", count=20, latency_ms=22.0)

    response = client.post(
        f"/api/deployments/{deployment.id}/evaluate",
        json={"minimum_requests": 10},
    )
    assert response.status_code == 200
    body = response.json()
    policy_names = {c["policy_name"] for c in body["checks"]}
    # No labels were recorded, so the quality data-sufficiency gate stays
    # INCONCLUSIVE and minimum_recall never runs - that's the point of the gate,
    # see test_evaluate_passes_when_recall_meets_threshold_with_labels below for
    # the case where it does.
    assert policy_names == {
        "minimum_requests",
        "latency_p95_increase",
        "max_error_rate",
        "minimum_labeled_samples",
        "minimum_label_coverage",
        "minimum_positive_labels",
    }
    # No actual_label backfilled -> the quality gate must be inconclusive, and
    # that alone must not drag a clean deployment down to FAIL.
    coverage_check = next(c for c in body["checks"] if c["policy_name"] == "minimum_label_coverage")
    assert coverage_check["result"] == "INCONCLUSIVE"
    assert body["overall_result"] == "INCONCLUSIVE"


def test_evaluate_fails_on_excessive_latency_increase(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=20, latency_ms=20.0)
    _add_metrics(db_session, deployment.id, "v2-good", count=20, latency_ms=100.0)

    response = client.post(
        f"/api/deployments/{deployment.id}/evaluate",
        json={"minimum_requests": 10, "latency": {"p95_max_increase_percent": 20.0}},
    )
    body = response.json()
    assert body["overall_result"] == "FAIL"
    latency_check = next(c for c in body["checks"] if c["policy_name"] == "latency_p95_increase")
    assert latency_check["result"] == "FAIL"


def test_evaluate_fails_on_excessive_error_rate(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=20, status_code=200)
    _add_metrics(db_session, deployment.id, "v2-good", count=10, status_code=200)
    _add_metrics(db_session, deployment.id, "v2-good", count=10, status_code=500)

    response = client.post(
        f"/api/deployments/{deployment.id}/evaluate",
        json={"minimum_requests": 10, "reliability": {"max_error_rate_percent": 5.0}},
    )
    body = response.json()
    assert body["overall_result"] == "FAIL"
    error_check = next(c for c in body["checks"] if c["policy_name"] == "max_error_rate")
    assert error_check["result"] == "FAIL"
    assert error_check["observed_value"] == pytest.approx(50.0)


def test_evaluate_passes_when_recall_meets_threshold_with_labels(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=20)
    _add_metrics(
        db_session, deployment.id, "v2-good", count=20, recall_labels=(1, 1)
    )  # perfect recall

    response = client.post(
        f"/api/deployments/{deployment.id}/evaluate",
        json={
            "minimum_requests": 10,
            "quality": {"minimum_recall": 0.8},
            # Quality reads an older, matured window by default (label_maturity_
            # seconds=60) - these rows were inserted ~now, not 60s+ ago, so without
            # collapsing the offset to 0 they'd fall outside that window entirely
            # and the quality gate would never even see them. The two-window
            # mechanic itself is covered by dedicated tests elsewhere; this test is
            # about minimum_recall's own PASS logic.
            "label_maturity_seconds": 0,
            "minimum_labeled_samples": 10,
            # All 20 canary rows are labeled positive (recall_labels=(1, 1)), so
            # positive_label_count=20 here - well above a lowered threshold, same
            # reasoning as minimum_labeled_samples above.
            "minimum_positive_labels": 10,
        },
    )
    body = response.json()
    recall_check = next(c for c in body["checks"] if c["policy_name"] == "minimum_recall")
    assert recall_check["result"] == "PASS"
    assert recall_check["observed_value"] == pytest.approx(1.0)
    assert body["overall_result"] == "PASS"


def test_evaluate_does_not_change_deployment_status(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=20)
    _add_metrics(db_session, deployment.id, "v2-good", count=20)

    client.post(f"/api/deployments/{deployment.id}/evaluate", json={"minimum_requests": 10})

    detail = client.get(f"/api/deployments/{deployment.id}").json()
    assert detail["status"] == "CANARY_RUNNING"


def test_evaluate_rejects_terminal_deployment(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    """A PROMOTED/ROLLED_BACK/FAILED/INCONCLUSIVE deployment has nothing left to
    evaluate - writing more PolicyEvaluation rows against it would just pollute its
    timeline with checks that can no longer affect anything."""
    deployment.status = DeploymentStatus.PROMOTED
    db_session.commit()

    response = client.post(
        f"/api/deployments/{deployment.id}/evaluate", json={"minimum_requests": 1}
    )
    assert response.status_code == 409

    # And no PolicyEvaluation row was actually written.
    evaluations = client.get(f"/api/deployments/{deployment.id}/policy-evaluations").json()
    assert evaluations == []


def test_evaluate_unknown_deployment_returns_404(client: TestClient) -> None:
    response = client.post("/api/deployments/does-not-exist/evaluate", json={})
    assert response.status_code == 404


def test_evaluate_without_body_uses_default_policy_config(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=5)
    _add_metrics(db_session, deployment.id, "v2-good", count=5)

    response = client.post(f"/api/deployments/{deployment.id}/evaluate")
    assert response.status_code == 200
    # Default minimum_requests (100) is well above 5, so this should be inconclusive.
    assert response.json()["overall_result"] == "INCONCLUSIVE"


def test_policy_evaluations_endpoint_returns_persisted_checks_newest_first(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=20)
    _add_metrics(db_session, deployment.id, "v2-good", count=20)

    client.post(f"/api/deployments/{deployment.id}/evaluate", json={"minimum_requests": 10})
    client.post(f"/api/deployments/{deployment.id}/evaluate", json={"minimum_requests": 10})

    response = client.get(f"/api/deployments/{deployment.id}/policy-evaluations")
    assert response.status_code == 200
    body = response.json()
    # Two evaluate() calls x 6 checks each (minimum_requests, latency_p95_increase,
    # max_error_rate, minimum_labeled_samples, minimum_label_coverage,
    # minimum_positive_labels - no labels were recorded here, so minimum_recall
    # never runs, see test_evaluate_records_all_checks_when_traffic_sufficient
    # above).
    assert len(body) == 12
    timestamps = [item["evaluated_at"] for item in body]
    assert timestamps == sorted(timestamps, reverse=True)


def test_policy_evaluations_endpoint_unknown_deployment_404(client: TestClient) -> None:
    response = client.get("/api/deployments/does-not-exist/policy-evaluations")
    assert response.status_code == 404
