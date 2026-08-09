from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.models import (
    Deployment,
    DeploymentEvent,
    DeploymentStatus,
    PredictionMetric,
)
from app.control_plane.router_gateway import RouterGateway, get_router_gateway
from app.db import get_db
from app.main import app


class FakeRouterGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, list[dict[str, Any]]]] = []

    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, targets: list[dict[str, Any]]
    ) -> None:
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
) -> None:
    for _ in range(count):
        db_session.add(
            PredictionMetric(
                deployment_id=deployment_id,
                model_version=version,
                latency_ms=latency_ms,
                status_code=200,
                created_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        )
    db_session.commit()


def test_timeline_unknown_deployment_returns_404(client: TestClient) -> None:
    response = client.get("/api/deployments/does-not-exist/timeline")
    assert response.status_code == 404


def test_timeline_includes_manually_logged_events(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    db_session.add(
        DeploymentEvent(deployment_id=deployment.id, event_type="created", message="created it")
    )
    db_session.commit()

    response = client.get(f"/api/deployments/{deployment.id}/timeline")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["type"] == "event"
    assert body[0]["event_type"] == "created"


def test_timeline_merges_events_and_policy_evaluations_in_chronological_order(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    db_session.add(
        DeploymentEvent(deployment_id=deployment.id, event_type="created", message="created it")
    )
    db_session.commit()

    _add_metrics(db_session, deployment.id, "v1", count=20)
    _add_metrics(db_session, deployment.id, "v2-good", count=20)

    client.post(f"/api/deployments/{deployment.id}/evaluate", json={"minimum_requests": 10})

    response = client.get(f"/api/deployments/{deployment.id}/timeline")
    assert response.status_code == 200
    body = response.json()

    types = {item["type"] for item in body}
    assert types == {"event", "policy_evaluation"}

    timestamps = [item["timestamp"] for item in body]
    assert timestamps == sorted(timestamps)  # ascending / chronological


def test_timeline_policy_items_carry_an_explanation(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    _add_metrics(db_session, deployment.id, "v1", count=5)
    _add_metrics(db_session, deployment.id, "v2-good", count=5)

    client.post(f"/api/deployments/{deployment.id}/evaluate", json={"minimum_requests": 100})

    body = client.get(f"/api/deployments/{deployment.id}/timeline").json()
    policy_items = [item for item in body if item["type"] == "policy_evaluation"]
    assert len(policy_items) == 1
    assert policy_items[0]["policy_name"] == "minimum_requests"
    assert policy_items[0]["result"] == "INCONCLUSIVE"
    assert "insufficient data" in policy_items[0]["explanation"]


def test_timeline_explains_stable_starvation_when_canary_is_fully_promoted(
    client: TestClient, db_session: Session, deployment: Deployment
) -> None:
    """The known platform limit: a canary at 100% traffic starves the stable side of
    requests, so minimum_requests can never pass - the timeline should say so plainly
    rather than leaving a bare INCONCLUSIVE for a human to puzzle over."""
    client.post(f"/api/deployments/{deployment.id}/promote")

    _add_metrics(db_session, deployment.id, "v2-good", count=5)  # canary only, stable starved

    client.post(f"/api/deployments/{deployment.id}/evaluate", json={"minimum_requests": 100})

    body = client.get(f"/api/deployments/{deployment.id}/timeline").json()
    policy_items = [item for item in body if item["type"] == "policy_evaluation"]
    assert len(policy_items) == 1
    explanation = policy_items[0]["explanation"]
    assert "stable side" in explanation
    assert "100% of traffic" in explanation
    assert "not a bug" in explanation


def test_deployment_out_marks_benchmark_deployments(
    client: TestClient, db_session: Session
) -> None:
    real = Deployment(
        model_name="fraud-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.CANARY_RUNNING,
    )
    benchmark = Deployment(
        model_name="benchmark-baseline",
        stable_version="v1",
        canary_version="v1",
        status=DeploymentStatus.CANARY_RUNNING,
    )
    db_session.add_all([real, benchmark])
    db_session.commit()

    listing = {d["id"]: d for d in client.get("/api/deployments").json()}
    assert listing[real.id]["is_benchmark"] is False
    assert listing[benchmark.id]["is_benchmark"] is True
