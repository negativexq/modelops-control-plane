from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.models import Deployment, DeploymentStatus, TrafficAllocation
from app.control_plane.router_gateway import RouterGateway, get_router_gateway
from app.db import get_db
from app.main import app


class FakeRouterGateway:
    """Router-shaped fake: tracks observed (deployment_id, revision, targets) and
    rejects a same-deployment push whose revision isn't strictly greater, exactly
    like app/router/main.py's real staleness check (see StaleRevisionError) - not
    just a call recorder. `should_fail` simulates the router being completely
    unreachable (RouterUpdateError) regardless of revision - since a push failure
    no longer marks a deployment FAILED (see
    docs/DESIGN_NOTES.md#desired-observed-reconciliation), this now exercises the
    "desired committed, router push best-effort" path, not a FAILED transition.
    """

    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.calls: list[tuple[str, str, int, list[dict[str, Any]]]] = []
        self.observed_model_name: str | None = None
        self.observed_deployment_id: str | None = None
        self.observed_revision: int = 0
        self.observed_targets: list[dict[str, Any]] | None = None

    async def push_traffic_allocation(
        self, model_name: str, deployment_id: str, revision: int, targets: list[dict[str, Any]]
    ) -> None:
        from app.control_plane.router_gateway import RouterUpdateError, StaleRevisionError

        if self.should_fail:
            raise RouterUpdateError("simulated router failure")
        # Model-scoped generation (Sprint 14), not per-deployment - see
        # app/router/main.py's put_config.
        same_model = model_name == self.observed_model_name
        if same_model and revision <= self.observed_revision:
            raise StaleRevisionError(f"stale revision {revision} for {deployment_id}")
        self.observed_model_name = model_name
        self.observed_deployment_id = deployment_id
        self.observed_revision = revision
        self.observed_targets = targets
        self.calls.append((model_name, deployment_id, revision, targets))

    async def get_observed_config(self) -> dict[str, Any] | None:
        if self.observed_deployment_id is None:
            return None
        return {
            "model_name": self.observed_model_name,
            "deployment_id": self.observed_deployment_id,
            "revision": self.observed_revision,
            "targets": self.observed_targets,
        }


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
    model_name, deployment_id, _revision, targets = fake_gateway.calls[0]
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


def test_create_deployment_reaches_canary_running_even_when_router_push_fails(
    db_session: Session,
) -> None:
    """The DB is authoritative desired state, committed BEFORE the router push -
    see docs/DESIGN_NOTES.md#desired-observed-reconciliation. A router that's
    unreachable at creation time no longer marks the deployment FAILED: desired
    state (CANARY_RUNNING) is correct regardless of whether the router has
    caught up to it yet, and the reconciler (POST /api/router/reconcile) is what
    actually closes that gap - not this request.
    """
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
    assert body["status"] == "CANARY_RUNNING"
    # The push was attempted (and failed) but never recorded as a successful call.
    assert failing_gateway.calls == []


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
    # Different model_names: two *active* deployments for the *same* model are
    # rejected regardless of idempotency key (see the 409 test below) - this test
    # is specifically about the idempotency key itself, not about that rule, so it
    # sidesteps it rather than retesting it.
    first = _create_deployment(client, model_name="model-a")
    second = _create_deployment(client, model_name="model-b")
    assert first.json()["id"] != second.json()["id"]


def test_second_active_deployment_for_same_model_is_rejected(client: TestClient) -> None:
    first = _create_deployment(client)
    assert first.status_code == 201

    second = _create_deployment(client, idempotency_key="a-different-key")
    assert second.status_code == 409

    # And no second row was actually created.
    listing = client.get("/api/deployments").json()
    matching = [d for d in listing if d["model_name"] == "fraud-model"]
    assert len(matching) == 1


def test_new_deployment_allowed_once_previous_one_is_terminal(client: TestClient) -> None:
    first_id = _create_deployment(client).json()["id"]
    client.post(f"/api/deployments/{first_id}/rollback")  # -> ROLLED_BACK (terminal)

    second = _create_deployment(client)
    assert second.status_code == 201
    assert second.json()["id"] != first_id


def test_create_deployment_rejects_identical_stable_and_canary_version(
    client: TestClient,
) -> None:
    response = _create_deployment(client, canary_version="v1")  # same as stable_version
    assert response.status_code == 422


def test_create_deployment_rejects_zero_canary_weight_by_default(client: TestClient) -> None:
    response = _create_deployment(client, canary_weight=0.0)
    assert response.status_code == 422


def test_create_deployment_rejects_full_canary_weight_by_default(client: TestClient) -> None:
    response = _create_deployment(client, canary_weight=1.0)
    assert response.status_code == 422


def test_create_deployment_allows_zero_canary_weight_with_explicit_bypass(
    client: TestClient,
) -> None:
    """The benchmark suite's "baseline" scenario deliberately uses canary_weight=0
    (no canary traffic at all) - see scripts/benchmarks/control_plane_client.py."""
    response = _create_deployment(
        client, canary_weight=0.0, allow_degenerate_canary_weight=True
    )
    assert response.status_code == 201


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
    model_name, called_deployment_id, _revision, targets = fake_gateway.calls[-1]
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

    model_name, called_deployment_id, _revision, targets = fake_gateway.calls[-1]
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


def test_create_deployment_paused_from_the_start(client: TestClient) -> None:
    """Lets a caller (see scripts/ci_smoke_test.py's manual scenario) create an
    already-paused deployment in one call, closing the race a create-then-pause
    round trip would otherwise leave open against the worker's own polling."""
    response = _create_deployment(client, automation_paused=True)
    body = response.json()
    assert body["automation_paused"] is True
    event_types = [e["event_type"] for e in body["events"]]
    assert "automation_paused" in event_types


def test_create_deployment_defaults_to_not_paused(client: TestClient) -> None:
    response = _create_deployment(client)
    assert response.json()["automation_paused"] is False


def test_pause_automation_sets_flag_and_logs_event(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]

    response = client.post(f"/api/deployments/{deployment_id}/pause-automation")
    assert response.status_code == 200
    body = response.json()
    assert body["automation_paused"] is True
    event_types = [e["event_type"] for e in body["events"]]
    assert "automation_paused" in event_types


def test_pause_automation_is_idempotent(client: TestClient) -> None:
    """Pausing an already-paused deployment must not write a second event -
    otherwise a double-click or a retried request would spam the timeline."""
    deployment_id = _create_deployment(client).json()["id"]

    first = client.post(f"/api/deployments/{deployment_id}/pause-automation")
    second = client.post(f"/api/deployments/{deployment_id}/pause-automation")
    assert first.status_code == 200
    assert second.status_code == 200
    event_types = [e["event_type"] for e in second.json()["events"]]
    assert event_types.count("automation_paused") == 1


def test_resume_automation_clears_flag_and_logs_event(client: TestClient) -> None:
    deployment_id = _create_deployment(client, automation_paused=True).json()["id"]

    response = client.post(f"/api/deployments/{deployment_id}/resume-automation")
    assert response.status_code == 200
    body = response.json()
    assert body["automation_paused"] is False
    event_types = [e["event_type"] for e in body["events"]]
    assert "automation_resumed" in event_types


def test_pause_automation_on_terminal_deployment_is_rejected(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/promote")

    response = client.post(f"/api/deployments/{deployment_id}/pause-automation")
    assert response.status_code == 409


def test_resume_automation_on_terminal_deployment_is_rejected(client: TestClient) -> None:
    deployment_id = _create_deployment(client, automation_paused=True).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/promote")

    response = client.post(f"/api/deployments/{deployment_id}/resume-automation")
    assert response.status_code == 409


def test_manual_promote_is_unaffected_by_automation_pause(client: TestClient) -> None:
    """Pause only ever stops the automated worker - a human's own /promote and
    /rollback calls must keep working exactly as before."""
    deployment_id = _create_deployment(client, automation_paused=True).json()["id"]

    response = client.post(f"/api/deployments/{deployment_id}/promote")
    assert response.status_code == 200
    assert response.json()["status"] == "PROMOTED"


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


def test_traffic_allocation_out_includes_revision(client: TestClient) -> None:
    body = _create_deployment(client).json()
    assert body["traffic_allocation"]["revision"] == 1


def test_get_observed_router_state_reflects_last_push(client: TestClient) -> None:
    deployment_id = _create_deployment(client).json()["id"]

    response = client.get("/api/router/observed")
    assert response.status_code == 200
    body = response.json()
    assert body["reachable"] is True
    assert body["deployment_id"] == deployment_id
    assert body["revision"] == 1


def test_reconcile_endpoint_is_a_noop_when_already_in_sync(client: TestClient) -> None:
    _create_deployment(client)

    response = client.post("/api/router/reconcile")
    assert response.status_code == 200
    body = response.json()
    assert body["reconciled"] is False
    assert body["reason"] == "already in sync"


def test_router_config_endpoint_serves_final_allocation_after_rollback(
    client: TestClient,
) -> None:
    """Sprint 14: the only deployment for this model is now ROLLED_BACK (a
    terminal state), but its final TrafficAllocation is still authoritative
    (see service.get_authoritative_allocation) - a router restarting after a
    successful rollback must sync to 100% stable, not fall all the way back
    to the router's own bootstrap default. See
    docs/DESIGN_NOTES.md#desired-observed-reconciliation."""
    deployment_id = _create_deployment(client).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/rollback")

    response = client.get("/api/router-config/fraud-model")
    assert response.status_code == 200
    body = response.json()
    assert body["deployment_id"] == deployment_id
    assert body["targets"] == [{"version": "v1", "weight": 1.0}]


def test_router_config_endpoint_serves_final_allocation_after_promote(
    client: TestClient,
) -> None:
    """Same fix as the rollback test above, for the PROMOTED path specifically
    (Section 1's acceptance test #3) - before Sprint 14 this fell all the way
    back to the router's own bootstrap default (README's "even after the
    router restarts" claim was false for exactly this case)."""
    deployment_id = _create_deployment(client).json()["id"]
    client.post(f"/api/deployments/{deployment_id}/promote")

    response = client.get("/api/router-config/fraud-model")
    assert response.status_code == 200
    body = response.json()
    assert body["deployment_id"] == deployment_id
    assert body["targets"] == [{"version": "v2-good", "weight": 1.0}]


def test_router_config_endpoint_serves_frozen_inconclusive_allocation(
    client: TestClient,
) -> None:
    """INCONCLUSIVE is authoritative too (Section 1 of the follow-up fix -
    record_inconclusive freezes the traffic split for manual review, it
    doesn't change it), so a router restarting while a deployment is frozen
    must sync to that frozen split, not fall back to the bootstrap default."""
    deployment = _create_deployment(
        client, policy_config={"max_inconclusive_retries": 1}
    ).json()
    deployment_id = deployment["id"]

    client.post(f"/api/deployments/{deployment_id}/record-inconclusive")  # attempt 1/1
    frozen = client.post(f"/api/deployments/{deployment_id}/record-inconclusive").json()
    assert frozen["status"] == "INCONCLUSIVE"

    response = client.get("/api/router-config/fraud-model")
    assert response.status_code == 200
    body = response.json()
    assert body["deployment_id"] == deployment_id
    assert body["targets"] == [
        {"version": "v1", "weight": 0.9},
        {"version": "v2-good", "weight": 0.1},
    ]


def test_router_config_endpoint_404_when_only_deployment_is_failed(
    client: TestClient, db_session: Session
) -> None:
    """FAILED is deliberately excluded from the authoritative fallback (see
    service.get_authoritative_allocation) - a deployment that never reached a
    legitimate PROMOTED/ROLLED_BACK outcome has no traffic split worth
    restoring, so the router-config endpoint must still 404, leaving the
    router on its own bootstrap default."""
    only_deployment = Deployment(
        model_name="only-failed-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.FAILED,
    )
    db_session.add(only_deployment)
    db_session.flush()
    db_session.add(
        TrafficAllocation(
            deployment_id=only_deployment.id,
            targets=[{"version": "v1", "weight": 0.5}, {"version": "v2-good", "weight": 0.5}],
        )
    )
    db_session.commit()

    response = client.get("/api/router-config/only-failed-model")
    assert response.status_code == 404


def test_router_config_endpoint_prefers_active_over_newer_terminal_deployment(
    client: TestClient, db_session: Session
) -> None:
    """get_active_deployment must filter by status, not just take the newest row -
    constructed directly via the ORM (rather than two /api/deployments calls)
    since an active deployment now blocks a second one for the same model, so this
    exact shape (older active + newer terminal for one model) can no longer arise
    through the API itself - only from data written before that rule existed.
    """
    older_active = Deployment(
        model_name="shared-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.CANARY_RUNNING,
        created_at=datetime.now(UTC) - timedelta(minutes=5),
    )
    db_session.add(older_active)
    db_session.flush()
    db_session.add(
        TrafficAllocation(
            deployment_id=older_active.id,
            targets=[{"version": "v1", "weight": 0.9}, {"version": "v2-good", "weight": 0.1}],
        )
    )

    newer_terminal = Deployment(
        model_name="shared-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.PROMOTED,
        created_at=datetime.now(UTC),
    )
    db_session.add(newer_terminal)
    db_session.commit()

    response = client.get("/api/router-config/shared-model")
    assert response.status_code == 200
    assert response.json()["deployment_id"] == older_active.id


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
    prediction_id: str | None = None,
) -> httpx.Response:
    return client.post(
        f"/api/deployments/{deployment_id}/metrics",
        json={
            "model_version": model_version,
            "latency_ms": latency_ms,
            "status_code": status_code,
            "prediction": prediction,
            "prediction_id": prediction_id,
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
    # tp=1, fp=1, fn=1, tn=1 - ground truth arrives through POST /api/labels, not
    # a direct actual_label field on the metrics endpoint (which has none, see
    # MetricIn's docstring).
    rows = [(1, 1), (1, 0), (0, 1), (0, 0)]
    for i, (prediction, actual_label) in enumerate(rows):
        prediction_id = f"pred-{i}"
        _post_metric(
            client,
            deployment_id,
            "v1",
            latency_ms=10,
            prediction=prediction,
            prediction_id=prediction_id,
        )
        label_response = client.post(
            "/api/labels",
            json={
                "prediction_id": prediction_id,
                "actual_label": actual_label,
                "occurred_at": datetime.now(UTC).isoformat(),
            },
        )
        assert label_response.status_code == 201

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
