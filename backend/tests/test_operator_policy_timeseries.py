from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.metrics_service import choose_bucket_seconds, compute_timeseries
from app.control_plane.models import Deployment, DeploymentStatus, PredictionMetric
from app.control_plane.router_gateway import RouterGateway, get_router_gateway
from app.db import get_db
from app.main import app
from app.policy.config import policy_settings


class FakeRouterGateway:
    async def push_traffic_allocation(self, *args: Any) -> None:
        return None


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    async def _get_gateway() -> RouterGateway:
        return FakeRouterGateway()  # type: ignore[return-value]

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_router_gateway] = _get_gateway
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_router_gateway, None)


def _create(client: TestClient, headers: dict[str, str] | None = None, **extra: Any) -> Any:
    response = client.post(
        "/api/deployments",
        json={
            "model_name": "fraud-model",
            "stable_version": "v1",
            "canary_version": "v2-good",
            "canary_weight": 0.1,
            **extra,
        },
        headers=headers or {},
    )
    assert response.status_code == 201, response.text
    return response.json()


# --- operator identity ----------------------------------------------------------


def test_manual_actions_record_the_operator(client: TestClient) -> None:
    created = _create(client, headers={"X-Operator": "  Ada   Lovelace "})
    assert {e["actor"] for e in created["events"]} == {"Ada Lovelace"}

    rolled = client.post(
        f"/api/deployments/{created['id']}/rollback", headers={"X-Operator": "grace"}
    ).json()
    final = rolled["events"][-1]
    assert final["message"].endswith("(manual)")
    assert final["actor"] == "grace"

    timeline = client.get(f"/api/deployments/{created['id']}/timeline").json()
    assert timeline[-1]["actor"] == "grace"


def test_percent_encoded_operator_names_are_decoded(client: TestClient) -> None:
    created = _create(client, headers={"X-Operator": "%C5%9Eule%20%C3%96z"})
    assert created["events"][0]["actor"] == "Şule Öz"


def test_operator_is_optional(client: TestClient) -> None:
    created = _create(client)
    assert all(e["actor"] is None for e in created["events"])


def test_worker_actions_never_carry_an_operator(client: TestClient) -> None:
    created = _create(client)
    paused = client.post(
        f"/api/deployments/{created['id']}/pause-automation?triggered_by=automatic",
        headers={"X-Operator": "mallory"},
    ).json()
    assert paused["events"][-1]["actor"] is None


@pytest.mark.parametrize("bad", ["x" * 65, "<script>", "a;b"])
def test_operator_name_is_validated(client: TestClient, bad: str) -> None:
    response = client.post(
        "/api/deployments",
        json={"model_name": "m", "stable_version": "v1", "canary_version": "v2"},
        headers={"X-Operator": bad},
    )
    assert response.status_code == 422


# --- policy defaults / custom thresholds ----------------------------------------


def test_policy_defaults_endpoint_matches_settings(client: TestClient) -> None:
    body = client.get("/api/policy/defaults").json()
    assert body == policy_settings.to_policy_config().model_dump()


def test_custom_policy_config_is_stored_on_the_deployment(client: TestClient) -> None:
    config = client.get("/api/policy/defaults").json()
    config["latency"]["p95_max_increase_percent"] = 7.5
    config["quality"]["minimum_recall"] = 0.9
    created = _create(client, policy_config=config)
    assert created["policy_config"]["latency"]["p95_max_increase_percent"] == 7.5
    assert created["policy_config"]["quality"]["minimum_recall"] == 0.9


def test_out_of_range_policy_config_is_rejected(client: TestClient) -> None:
    config = client.get("/api/policy/defaults").json()
    config["quality"]["minimum_recall"] = 1.5
    response = client.post(
        "/api/deployments",
        json={
            "model_name": "fraud-model",
            "stable_version": "v1",
            "canary_version": "v2-good",
            "policy_config": config,
        },
    )
    assert response.status_code == 422


# --- metrics timeseries ---------------------------------------------------------


@pytest.mark.parametrize(
    ("span", "expected"), [(60, 5), (600, 5), (601, 10), (3600, 30), (86400, 900)]
)
def test_bucket_size_keeps_series_short(span: float, expected: int) -> None:
    assert choose_bucket_seconds(span) == expected


def test_timeseries_buckets_latency_and_errors_per_version(db_session: Session) -> None:
    start = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    d = Deployment(
        model_name="fraud-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.CANARY_RUNNING,
    )
    db_session.add(d)
    db_session.flush()

    def metric(version: str, offset: float, latency: float, status_code: int = 200) -> None:
        db_session.add(
            PredictionMetric(
                deployment_id=d.id,
                model_version=version,
                latency_ms=latency,
                status_code=status_code,
                created_at=start + timedelta(seconds=offset),
            )
        )

    metric("v1", 1, 10)
    metric("v1", 2, 20)
    metric("v2-good", 3, 100, 500)
    metric("v2-good", 4, 50)
    metric("v2-good", 55, 40)
    db_session.commit()

    out = compute_timeseries(
        db_session,
        d.id,
        [("v1", "stable"), ("v2-good", "canary")],
        start,
        start + timedelta(seconds=60),
    )
    assert out.bucket_seconds == 5
    assert len(out.series[0].buckets) == 12
    stable, canary = out.series
    assert stable.role == "stable"
    assert stable.buckets[0].count == 2
    assert stable.buckets[0].p50_latency_ms == pytest.approx(15)
    assert stable.buckets[0].error_rate == 0
    assert canary.buckets[0].error_rate == pytest.approx(0.5)
    assert canary.buckets[1].count == 0
    assert canary.buckets[1].p95_latency_ms is None
    assert canary.buckets[11].count == 1


def test_timeseries_endpoint(client: TestClient) -> None:
    created = _create(client)
    body = client.get(f"/api/deployments/{created['id']}/metrics/timeseries").json()
    assert [s["role"] for s in body["series"]] == ["stable", "canary"]
    assert body["bucket_seconds"] == 5

    narrowed = client.get(
        f"/api/deployments/{created['id']}/metrics/timeseries?window_seconds=10"
    )
    assert narrowed.status_code == 200
    assert client.get("/api/deployments/nope/metrics/timeseries").status_code == 404
