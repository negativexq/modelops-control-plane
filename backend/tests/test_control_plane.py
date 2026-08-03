from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.router_gateway import RouterGateway, get_router_gateway
from app.db import get_db
from app.main import app


class FakeRouterGateway:
    """Records every push instead of making a real HTTP call.

    `should_fail` lets tests simulate the router being unreachable / rejecting the
    config, to exercise the FAILED-transition path.
    """

    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []

    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, targets: list[dict[str, Any]]
    ) -> None:
        from app.control_plane.router_gateway import RouterUpdateError

        if self.should_fail:
            raise RouterUpdateError("simulated router failure")
        self.calls.append((model_name, deployment_id, targets))


@pytest.fixture
def fake_gateway() -> FakeRouterGateway:
    return FakeRouterGateway()


@pytest.fixture
def client(db_session: Session, fake_gateway: FakeRouterGateway) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    async def _get_gateway() -> RouterGateway:
        return fake_gateway  # type: ignore[return-value]

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_router_gateway] = _get_gateway
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_router_gateway, None)


def _create_deployment(
    client: TestClient, idempotency_key: str | None = None, **overrides: Any
) -> httpx.Response:
    payload = {
        "model_name": "fraud-model",
        "stable_version": "v1",
        "canary_version": "v2-good",
        "canary_weight": 0.1,
        **overrides,
    }
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
    return client.post("/api/deployments", json=payload, headers=headers)


def test_create_deployment_reaches_canary_running(
    client: TestClient, fake_gateway: FakeRouterGateway
) -> None:
    response = _create_deployment(client)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "CANARY_RUNNING"
    assert body["model_name"] == "fraud-model"
    assert body["started_at"] is not None

    assert len(fake_gateway.calls) == 1
    model_name, deployment_id, targets = fake_gateway.calls[0]
    assert model_name == "fraud-model"
    assert deployment_id == body["id"]
    weights = {t["version"]: t["weight"] for t in targets}
    assert weights == {"v1": 0.9, "v2-good": 0.1}


def test_create_deployment_logs_events(client: TestClient) -> None:
    response = _create_deployment(client)
    body = response.json()
    event_types = [e["event_type"] for e in body["events"]]
    assert "created" in event_types
    assert event_types.count("status_changed") >= 2  # DEPLOYING, then CANARY_RUNNING


def test_create_deployment_marks_failed_when_router_push_fails(db_session: Session) -> None:
    failing_gateway = FakeRouterGateway(should_fail=True)

    def _get_db() -> Iterator[Session]:
        yield db_session

    async def _get_gateway() -> RouterGateway:
        return failing_gateway  # type: ignore[return-value]

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_router_gateway] = _get_gateway
    try:
        with TestClient(app) as client:
            response = _create_deployment(client)
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_router_gateway, None)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "FAILED"
    assert any("router update failed" in e["message"] for e in body["events"])


def test_duplicate_idempotency_key_does_not_create_second_deployment(client: TestClient) -> None:
    first = _create_deployment(client, idempotency_key="deploy-abc-123")
    assert first.status_code == 201
    first_id = first.json()["id"]

    second = _create_deployment(client, idempotency_key="deploy-abc-123")
    assert second.status_code == 200
    assert second.json()["id"] == first_id

    listing = client.get("/api/deployments").json()
    matching = [d for d in listing if d["id"] == first_id]
    assert len(matching) == 1


def test_requests_without_idempotency_key_each_create_a_new_deployment(
    client: TestClient,
) -> None:
    first = _create_deployment(client)
    second = _create_deployment(client)
    assert first.json()["id"] != second.json()["id"]


def test_get_deployment_not_found_returns_404(client: TestClient) -> None:
    response = client.get("/api/deployments/does-not-exist")
    assert response.status_code == 404


def test_promote_updates_traffic_allocation_and_status(
    client: TestClient, fake_gateway: FakeRouterGateway
) -> None:
    deployment_id = _create_deployment(client).json()["id"]

    response = client.post(f"/api/deployments/{deployment_id}/promote")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PROMOTED"
    assert body["completed_at"] is not None
    assert body["traffic_allocation"]["targets"] == [{"version": "v2-good", "weight": 1.0}]

    # Last call to the router gateway should be the 100%-canary allocation.
    model_name, called_deployment_id, targets = fake_gateway.calls[-1]
    assert model_name == "fraud-model"
    assert called_deployment_id == deployment_id
    assert targets == [{"version": "v2-good", "weight": 1.0}]

    event_types = [e["event_type"] for e in body["events"]]
    assert event_types.count("status_changed") >= 4  # DEPLOYING, CANARY_RUNNING, EVALUATING,
    # PROMOTING, PROMOTED


def test_rollback_returns_all_traffic_to_stable(
    client: TestClient, fake_gateway: FakeRouterGateway
) -> None:
    deployment_id = _create_deployment(client).json()["id"]

    response = client.post(f"/api/deployments/{deployment_id}/rollback")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ROLLED_BACK"
    assert body["traffic_allocation"]["targets"] == [{"version": "v1", "weight": 1.0}]

    model_name, called_deployment_id, targets = fake_gateway.calls[-1]
    assert called_deployment_id == deployment_id
    assert targets == [{"version": "v1", "weight": 1.0}]


def test_promote_after_terminal_state_is_rejected(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    promote_response = client.post(f"/api/deployments/{deployment_id}/promote")
    assert promote_response.json()["status"] == "PROMOTED"

    second_promote = client.post(f"/api/deployments/{deployment_id}/promote")
    assert second_promote.status_code == 409


def test_rollback_after_promoted_is_rejected(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/promote")

    rollback_response = client.post(f"/api/deployments/{deployment_id}/rollback")
    assert rollback_response.status_code == 409


def test_router_config_endpoint_returns_active_allocation(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]

    response = client.get("/api/router-config/fraud-model")
    assert response.status_code == 200
    body = response.json()
    assert body["model_name"] == "fraud-model"
    assert body["deployment_id"] == deployment_id
    weights = {t["version"]: t["weight"] for t in body["targets"]}
    assert weights == {"v1": 0.9, "v2-good": 0.1}


def test_router_config_endpoint_404_for_unknown_model(client: TestClient) -> None:
    response = client.get("/api/router-config/unknown-model")
    assert response.status_code == 404


def test_router_config_endpoint_ignores_rolled_back_deployment(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/rollback")

    # The only deployment for this model is now ROLLED_BACK (a terminal state), so
    # there's no "active" allocation left - the router-config endpoint should not
    # fall back to serving stale history from a rolled-back deployment.
    response = client.get("/api/router-config/fraud-model")
    assert response.status_code == 404


def test_router_config_endpoint_prefers_active_over_newer_terminal_deployment(
    client: TestClient,
) -> None:
    first_id = _create_deployment(client, model_name="shared-model").json()["id"]

    second = _create_deployment(client, model_name="shared-model")
    second_id = second.json()["id"]
    client.post(f"/api/deployments/{second_id}/promote")  # -> PROMOTED (terminal)

    # `second` is newer but terminal; `first` is still CANARY_RUNNING and should be
    # the one the router syncs from.
    response = client.get("/api/router-config/shared-model")
    assert response.status_code == 200
    assert response.json()["deployment_id"] == first_id


def test_list_deployments_orders_newest_first(client: TestClient) -> None:
    first_id = _create_deployment(client, model_name="model-a").json()["id"]
    second_id = _create_deployment(client, model_name="model-b").json()["id"]

    listing = client.get("/api/deployments").json()
    ids = [d["id"] for d in listing]
    assert ids.index(second_id) < ids.index(first_id)


def _post_metric(
    client: TestClient,
    deployment_id: str,
    model_version: str,
    latency_ms: float,
    status_code: int = 200,
    prediction: int | None = None,
    actual_label: int | None = None,
) -> httpx.Response:
    return client.post(
        f"/api/deployments/{deployment_id}/metrics",
        json={
            "model_version": model_version,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "prediction": prediction,
            "actual_label": actual_label,
        },
    )


def test_record_metric_returns_202(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    response = _post_metric(client, deployment_id, "v1", latency_ms=12.5)
    assert response.status_code == 202
    assert response.json() == {"recorded": True}


def test_record_metric_unknown_deployment_404(client: TestClient) -> None:
    response = _post_metric(client, "does-not-exist", "v1", latency_ms=12.5)
    assert response.status_code == 404


def test_metrics_endpoint_separates_stable_and_canary(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]

    for latency in (10, 20, 30):
        _post_metric(client, deployment_id, "v1", latency_ms=latency)
    for latency in (100, 200):
        _post_metric(client, deployment_id, "v2-good", latency_ms=latency)

    response = client.get(f"/api/deployments/{deployment_id}/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["stable"]["version"] == "v1"
    assert body["stable"]["sample_count"] == 3
    assert body["canary"]["version"] == "v2-good"
    assert body["canary"]["sample_count"] == 2


def test_metrics_endpoint_error_rate(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    _post_metric(client, deployment_id, "v1", latency_ms=10, status_code=200)
    _post_metric(client, deployment_id, "v1", latency_ms=10, status_code=200)
    _post_metric(client, deployment_id, "v1", latency_ms=10, status_code=500)

    body = client.get(f"/api/deployments/{deployment_id}/metrics").json()
    assert body["stable"]["error_rate"] == pytest.approx(1 / 3)


def test_metrics_endpoint_precision_recall_none_without_labels(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    _post_metric(client, deployment_id, "v1", latency_ms=10, prediction=1)

    body = client.get(f"/api/deployments/{deployment_id}/metrics").json()
    assert body["stable"]["precision"] is None
    assert body["stable"]["recall"] is None
    assert body["stable"]["false_positive_rate"] is None


def test_metrics_endpoint_precision_recall_with_labels(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    # tp=1, fp=1, fn=1, tn=1
    _post_metric(client, deployment_id, "v1", latency_ms=10, prediction=1, actual_label=1)
    _post_metric(client, deployment_id, "v1", latency_ms=10, prediction=1, actual_label=0)
    _post_metric(client, deployment_id, "v1", latency_ms=10, prediction=0, actual_label=1)
    _post_metric(client, deployment_id, "v1", latency_ms=10, prediction=0, actual_label=0)

    body = client.get(f"/api/deployments/{deployment_id}/metrics").json()
    stable = body["stable"]
    assert stable["precision"] == pytest.approx(0.5)
    assert stable["recall"] == pytest.approx(0.5)
    assert stable["false_positive_rate"] == pytest.approx(0.5)


def test_metrics_endpoint_missing_deployment_404(client: TestClient) -> None:
    response = client.get("/api/deployments/does-not-exist/metrics")
    assert response.status_code == 404


def test_metrics_endpoint_window_seconds_excludes_old_samples(
    client: TestClient, db_session: Session
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.control_plane.models import PredictionMetric

    deployment_id = _create_deployment(client).json()["id"]

    old = PredictionMetric(
        deployment_id=deployment_id,
        model_version="v1",
        latency_ms=999,
        status_code=200,
        created_at=datetime.now(UTC) - timedelta(seconds=3600),
    )
    db_session.add(old)
    db_session.commit()

    _post_metric(client, deployment_id, "v1", latency_ms=10)

    body = client.get(f"/api/deployments/{deployment_id}/metrics?window_seconds=60").json()
    assert body["stable"]["sample_count"] == 1
    assert body["stable"]["p50_latency_ms"] == pytest.approx(10)


def test_comparison_endpoint_includes_deltas(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    _post_metric(client, deployment_id, "v1", latency_ms=100)
    _post_metric(client, deployment_id, "v2-good", latency_ms=50)

    body = client.get(f"/api/deployments/{deployment_id}/comparison").json()
    assert body["stable"]["version"] == "v1"
    assert body["canary"]["version"] == "v2-good"
    assert body["deltas"]["p95_latency_ms"] == pytest.approx(-50)


def test_comparison_endpoint_delta_none_when_one_side_empty(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    _post_metric(client, deployment_id, "v1", latency_ms=100)

    body = client.get(f"/api/deployments/{deployment_id}/comparison").json()
    assert body["canary"]["sample_count"] == 0
    assert body["deltas"]["p95_latency_ms"] is None
