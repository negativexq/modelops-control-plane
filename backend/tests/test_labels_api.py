from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.models import (
    Deployment,
    DeploymentStatus,
    GroundTruthLabel,
    PredictionMetric,
)
from app.db import get_db
from app.main import app


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


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
    db_session: Session, deployment_id: str, prediction_id: str, prediction: int = 1
) -> PredictionMetric:
    metric = PredictionMetric(
        deployment_id=deployment_id,
        model_version="v2-good",
        latency_ms=10.0,
        status_code=200,
        prediction=prediction,
        prediction_id=prediction_id,
    )
    db_session.add(metric)
    db_session.commit()
    db_session.refresh(metric)
    return metric


def _label_payload(prediction_id: str, actual_label: int) -> dict[str, object]:
    return {
        "prediction_id": prediction_id,
        "actual_label": actual_label,
        "occurred_at": datetime.now(UTC).isoformat(),
    }


def _get_label(db_session: Session, prediction_id: str) -> GroundTruthLabel | None:
    return (
        db_session.query(GroundTruthLabel).filter_by(prediction_id=prediction_id).one_or_none()
    )


# --- POST /api/labels: idempotency table -----------------------------------------


def test_label_applied_to_existing_metric_returns_201(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metric(db_session, deployment.id, "pred-1")

    response = client.post("/api/labels", json=_label_payload("pred-1", 1))
    assert response.status_code == 201
    assert response.json() == {"prediction_id": "pred-1", "status": "applied", "detail": None}

    label = _get_label(db_session, "pred-1")
    assert label is not None
    assert label.actual_label == 1


def test_same_label_twice_is_idempotent_no_op(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metric(db_session, deployment.id, "pred-2")

    first = client.post("/api/labels", json=_label_payload("pred-2", 0))
    assert first.status_code == 201

    second = client.post("/api/labels", json=_label_payload("pred-2", 0))
    assert second.status_code == 200
    assert second.json()["status"] == "no_op"


def test_conflicting_label_returns_409_and_logs_audit_event(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metric(db_session, deployment.id, "pred-3")
    client.post("/api/labels", json=_label_payload("pred-3", 1))

    response = client.post("/api/labels", json=_label_payload("pred-3", 0))
    assert response.status_code == 409
    assert response.json()["status"] == "conflict"

    timeline = client.get(f"/api/deployments/{deployment.id}/timeline").json()
    conflict_events = [
        item
        for item in timeline
        if item["type"] == "event" and item["event_type"] == "label_conflict"
    ]
    assert len(conflict_events) == 1
    assert "pred-3" in conflict_events[0]["message"]

    # The rejected value must not have overwritten the original.
    label = _get_label(db_session, "pred-3")
    assert label is not None
    assert label.actual_label == 1


def test_unknown_prediction_id_is_recorded_durably_and_returns_202(
    client: TestClient, db_session: Session
) -> None:
    """No PendingLabel to "park" in anymore - the label is written to
    GroundTruthLabel unconditionally, regardless of whether a matching
    PredictionMetric exists yet (see label_service.py). 202 just means "no
    matching metric yet", not "not yet durably recorded"."""
    response = client.post("/api/labels", json=_label_payload("pred-unknown", 1))
    assert response.status_code == 202
    assert response.json()["status"] == "pending"

    label = _get_label(db_session, "pred-unknown")
    assert label is not None
    assert label.actual_label == 1


def test_conflicting_pending_label_is_still_a_409_without_a_deployment_event(
    client: TestClient, db_session: Session
) -> None:
    """No PredictionMetric exists yet, so there's no deployment to log an audit
    event against - the conflict must still be reported (409), just without an
    event (see LabelConflictError's docstring)."""
    client.post("/api/labels", json=_label_payload("pred-pending", 1))

    response = client.post("/api/labels", json=_label_payload("pred-pending", 0))
    assert response.status_code == 409
    assert response.json()["status"] == "conflict"


# --- GroundTruthLabel <-> PredictionMetric join order-independence ----------------


def test_label_arriving_before_metric_is_joined_when_metric_is_recorded(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    label_response = client.post("/api/labels", json=_label_payload("pred-early", 1))
    assert label_response.status_code == 202

    metric_response = client.post(
        f"/api/deployments/{deployment.id}/metrics",
        json={
            "model_version": "v2-good",
            "latency_ms": 12.0,
            "status_code": 200,
            "prediction": 1,
            "prediction_id": "pred-early",
        },
    )
    assert metric_response.status_code == 202

    # The metric write itself never touches the label - it's still there, and
    # the two are only ever linked by a read-time JOIN on prediction_id (see
    # metrics_service.compute_version_summary).
    label = _get_label(db_session, "pred-early")
    assert label is not None
    assert label.actual_label == 1
    metric = db_session.query(PredictionMetric).filter_by(prediction_id="pred-early").one()
    assert metric.prediction_id == label.prediction_id

    metrics = client.get(f"/api/deployments/{deployment.id}/metrics").json()
    assert metrics["canary"]["labeled_sample_count"] == 1
    assert metrics["canary"]["positive_label_count"] == 1


def test_metric_arriving_before_label_is_labeled_normally(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    metric_response = client.post(
        f"/api/deployments/{deployment.id}/metrics",
        json={
            "model_version": "v2-good",
            "latency_ms": 12.0,
            "status_code": 200,
            "prediction": 0,
            "prediction_id": "pred-late",
        },
    )
    assert metric_response.status_code == 202

    label_response = client.post("/api/labels", json=_label_payload("pred-late", 0))
    assert label_response.status_code == 201

    label = _get_label(db_session, "pred-late")
    assert label is not None
    assert label.actual_label == 0


# --- POST /api/labels/batch -------------------------------------------------------


def test_batch_endpoint_reports_per_item_status(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metric(db_session, deployment.id, "batch-applied")
    client.post("/api/labels", json=_label_payload("batch-conflict", 1))  # -> pending, label=1

    response = client.post(
        "/api/labels/batch",
        json=[
            _label_payload("batch-applied", 1),
            _label_payload("batch-pending", 0),
            _label_payload("batch-conflict", 0),  # conflicts with the pending 1 above
        ],
    )
    assert response.status_code == 200
    by_id = {item["prediction_id"]: item["status"] for item in response.json()}
    assert by_id == {
        "batch-applied": "applied",
        "batch-pending": "pending",
        "batch-conflict": "conflict",
    }


# --- Labeling a prediction from a terminal deployment -----------------------------


def test_label_for_terminal_deployment_prediction_is_still_accepted(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    """Labels are pure data - a deployment reaching PROMOTED/ROLLED_BACK doesn't
    stop its historical predictions from being labeled later (no data loss), even
    though a new PolicyEvaluation could never be triggered for it (see the /evaluate
    active-only guard, unrelated to this endpoint)."""
    _add_metric(db_session, deployment.id, "pred-terminal")
    deployment.status = DeploymentStatus.PROMOTED
    db_session.commit()

    response = client.post("/api/labels", json=_label_payload("pred-terminal", 1))
    assert response.status_code == 201

    label = _get_label(db_session, "pred-terminal")
    assert label is not None
    assert label.actual_label == 1

    evaluations = client.get(f"/api/deployments/{deployment.id}/policy-evaluations").json()
    assert evaluations == []
