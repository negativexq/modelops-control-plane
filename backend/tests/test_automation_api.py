from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.router_gateway import RouterGateway, RouterUpdateError, get_router_gateway
from app.db import get_db
from app.main import app


class FakeRouterGateway:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []

    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, targets: list[dict[str, Any]]
    ) -> None:
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


def _create_deployment(client: TestClient, **overrides: Any) -> httpx.Response:
    payload: dict[str, Any] = {
        "model_name": "fraud-model",
        "stable_version": "v1",
        "canary_version": "v2-good",
        "canary_weight": 0.1,
        **overrides,
    }
    return client.post("/api/deployments", json=payload)


def _add_metrics(client: TestClient, deployment_id: str, version: str, count: int) -> None:
    for _ in range(count):
        client.post(
            f"/api/deployments/{deployment_id}/metrics",
            json={"model_version": version, "latency_ms": 10.0, "status_code": 200},
        )


# --- policy_config persistence --------------------------------------------------


def test_create_deployment_persists_default_policy_config(client: TestClient) -> None:
    body = _create_deployment(client).json()
    assert body["policy_config"] is not None
    assert body["policy_config"]["minimum_requests"] == 100
    assert body["policy_config"]["evaluation_window_seconds"] == 300
    assert body["inconclusive_retry_count"] == 0


def test_create_deployment_persists_custom_policy_config(client: TestClient) -> None:
    body = _create_deployment(
        client,
        policy_config={
            "minimum_requests": 5,
            "evaluation_window_seconds": 60,
            "max_inconclusive_retries": 2,
            "latency": {"p95_max_increase_percent": 10.0},
            "reliability": {"max_error_rate_percent": 1.0},
            "quality": {"minimum_recall": 0.9},
        },
    ).json()
    assert body["policy_config"]["minimum_requests"] == 5
    assert body["policy_config"]["max_inconclusive_retries"] == 2
    assert body["policy_config"]["latency"]["p95_max_increase_percent"] == 10.0


def test_evaluate_uses_deployment_persisted_policy_config_by_default(client: TestClient) -> None:
    deployment_id = _create_deployment(
        client, policy_config={"minimum_requests": 3}
    ).json()["id"]
    _add_metrics(client, deployment_id, "v1", count=5)
    _add_metrics(client, deployment_id, "v2-good", count=5)

    # No body -> must fall back to the deployment's own minimum_requests=3, not the
    # global default of 100.
    response = client.post(f"/api/deployments/{deployment_id}/evaluate")
    body = response.json()
    assert len(body["checks"]) > 1  # more than just the minimum_requests gate check


# --- triggered_by: manual vs automatic ------------------------------------------


def test_promote_default_is_manual(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    response = client.post(f"/api/deployments/{deployment_id}/promote")
    events = [e["message"] for e in response.json()["events"]]
    assert any("manual promote requested" in m for m in events)
    assert not any("automatic promote requested" in m for m in events)


def test_promote_triggered_by_automatic_is_labeled_in_event_log(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    response = client.post(f"/api/deployments/{deployment_id}/promote?triggered_by=automatic")
    events = [e["message"] for e in response.json()["events"]]
    assert any("automatic promote requested" in m for m in events)


def test_rollback_triggered_by_automatic_is_labeled_in_event_log(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    response = client.post(f"/api/deployments/{deployment_id}/rollback?triggered_by=automatic")
    events = [e["message"] for e in response.json()["events"]]
    assert any("rolled back to stable (automatic)" in m for m in events)


# --- advance-traffic --------------------------------------------------------------


def test_advance_traffic_moves_to_next_stage(
    client: TestClient, fake_gateway: FakeRouterGateway
) -> None:
    deployment_id = _create_deployment(client, canary_weight=0.1).json()["id"]

    response = client.post(f"/api/deployments/{deployment_id}/advance-traffic")
    assert response.status_code == 200
    body = response.json()
    weights = {t["version"]: t["weight"] for t in body["traffic_allocation"]["targets"]}
    assert weights["v2-good"] == 0.25
    assert weights["v1"] == 0.75
    assert body["status"] == "CANARY_RUNNING"  # traffic advance alone doesn't change status

    events = [e for e in body["events"] if e["event_type"] == "traffic_advanced"]
    assert len(events) == 1
    assert "auto:" in events[0]["message"]

    model_name, called_deployment_id, targets = fake_gateway.calls[-1]
    assert called_deployment_id == deployment_id
    assert {t["version"]: t["weight"] for t in targets} == {"v1": 0.75, "v2-good": 0.25}


def test_advance_traffic_walks_through_all_stages(client: TestClient) -> None:
    deployment_id = _create_deployment(client, canary_weight=0.1).json()["id"]

    expected_stages = [0.25, 0.5, 1.0]
    for expected_canary_weight in expected_stages:
        body = client.post(f"/api/deployments/{deployment_id}/advance-traffic").json()
        weights = {t["version"]: t["weight"] for t in body["traffic_allocation"]["targets"]}
        assert weights["v2-good"] == expected_canary_weight


def test_advance_traffic_at_final_stage_returns_409(client: TestClient) -> None:
    deployment_id = _create_deployment(client, canary_weight=0.1).json()["id"]
    for _ in range(3):  # 0.1 -> 0.25 -> 0.5 -> 1.0
        client.post(f"/api/deployments/{deployment_id}/advance-traffic")

    response = client.post(f"/api/deployments/{deployment_id}/advance-traffic")
    assert response.status_code == 409
    assert "promote" in response.json()["detail"]


def test_advance_traffic_rejects_terminal_deployment(client: TestClient) -> None:
    """Race condition: a human promotes/rolls back a deployment; a worker that had a
    stale view of it (still thinks it's CANARY_RUNNING) tries to advance traffic
    anyway. The server must reject it, not silently corrupt the (already-final)
    traffic allocation."""
    deployment_id = _create_deployment(client).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/rollback")  # -> ROLLED_BACK

    response = client.post(f"/api/deployments/{deployment_id}/advance-traffic")
    assert response.status_code == 409

    # And the rollback's traffic allocation must be untouched.
    detail = client.get(f"/api/deployments/{deployment_id}").json()
    assert detail["status"] == "ROLLED_BACK"
    weights = {t["version"]: t["weight"] for t in detail["traffic_allocation"]["targets"]}
    assert weights == {"v1": 1.0}


def test_advance_traffic_unknown_deployment_404(client: TestClient) -> None:
    response = client.post("/api/deployments/does-not-exist/advance-traffic")
    assert response.status_code == 404


def test_advance_traffic_marks_failed_when_router_push_fails(
    db_session: Session,
) -> None:
    failing_gateway = FakeRouterGateway(should_fail=False)

    def _get_db() -> Iterator[Session]:
        yield db_session

    async def _get_gateway() -> RouterGateway:
        return failing_gateway  # type: ignore[return-value]

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_router_gateway] = _get_gateway
    try:
        with TestClient(app) as test_client:
            deployment_id = _create_deployment(test_client).json()["id"]
            failing_gateway.should_fail = True
            response = test_client.post(f"/api/deployments/{deployment_id}/advance-traffic")
            assert response.status_code == 200
            assert response.json()["status"] == "FAILED"
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_router_gateway, None)


# --- record-inconclusive & max-retry freeze --------------------------------------


def test_record_inconclusive_increments_counter(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]

    body = client.post(f"/api/deployments/{deployment_id}/record-inconclusive").json()
    assert body["inconclusive_retry_count"] == 1
    assert body["status"] == "CANARY_RUNNING"

    events = [e for e in body["events"] if e["event_type"] == "inconclusive_cycle"]
    assert len(events) == 1
    assert "attempt 1/" in events[0]["message"]


def test_record_inconclusive_freezes_after_max_retries(client: TestClient) -> None:
    deployment_id = _create_deployment(
        client, policy_config={"max_inconclusive_retries": 2}
    ).json()["id"]

    first = client.post(f"/api/deployments/{deployment_id}/record-inconclusive").json()
    assert first["status"] == "CANARY_RUNNING"
    second = client.post(f"/api/deployments/{deployment_id}/record-inconclusive").json()
    assert second["status"] == "CANARY_RUNNING"
    third = client.post(f"/api/deployments/{deployment_id}/record-inconclusive").json()

    assert third["status"] == "INCONCLUSIVE"
    assert third["inconclusive_retry_count"] == 3
    assert third["completed_at"] is not None
    freeze_events = [e for e in third["events"] if "freezing for manual review" in e["message"]]
    assert len(freeze_events) == 1


def test_frozen_deployment_rejects_further_automated_actions(client: TestClient) -> None:
    deployment_id = _create_deployment(
        client, policy_config={"max_inconclusive_retries": 1}
    ).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/record-inconclusive")
    client.post(f"/api/deployments/{deployment_id}/record-inconclusive")  # freezes here

    response = client.post(f"/api/deployments/{deployment_id}/record-inconclusive")
    assert response.status_code == 409

    response = client.post(f"/api/deployments/{deployment_id}/advance-traffic")
    assert response.status_code == 409


def test_record_inconclusive_unknown_deployment_404(client: TestClient) -> None:
    response = client.post("/api/deployments/does-not-exist/record-inconclusive")
    assert response.status_code == 404


def test_manual_promote_after_frozen_inconclusive_still_works(client: TestClient) -> None:
    """INCONCLUSIVE is not a dead end - state_machine.py already allows
    INCONCLUSIVE -> PROMOTING/ROLLING_BACK for a human to make the final call."""
    deployment_id = _create_deployment(
        client, policy_config={"max_inconclusive_retries": 1}
    ).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/record-inconclusive")
    frozen = client.post(f"/api/deployments/{deployment_id}/record-inconclusive").json()
    assert frozen["status"] == "INCONCLUSIVE"

    response = client.post(f"/api/deployments/{deployment_id}/promote")
    assert response.status_code == 200
    assert response.json()["status"] == "PROMOTED"
