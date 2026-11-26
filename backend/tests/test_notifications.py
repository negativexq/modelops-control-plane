"""Decision webhooks: queued with the status change (app/notifications/outbox.py),
sent and retried by the dispatcher (dispatcher.py)."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane import service
from app.control_plane.config import control_plane_settings
from app.control_plane.models import (
    Deployment,
    DeploymentStatus,
    PolicyEvaluation,
    PolicyEvaluationResult,
    WebhookDelivery,
    WebhookDeliveryStatus,
)
from app.db import get_db
from app.main import app
from app.notifications import dispatcher, sink

URL = "https://hooks.example.test/modelops"
SECRET = "s3cret"


@pytest.fixture(autouse=True)
def webhook_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control_plane_settings, "webhook_urls", [URL])
    monkeypatch.setattr(control_plane_settings, "webhook_secret", SECRET)
    monkeypatch.setattr(control_plane_settings, "webhook_max_attempts", 3)


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
    return d


def _fail_latency(db: Session, deployment: Deployment) -> None:
    # One evaluation's rows each get their own timestamp; the latency FAIL is
    # written before a later, merely inconclusive quality gate.
    now = datetime.now(UTC)
    for offset, policy, result in [
        (0, "latency_p95_increase", PolicyEvaluationResult.FAIL),
        (1, "minimum_positive_labels", PolicyEvaluationResult.INCONCLUSIVE),
        (2, "max_error_rate", PolicyEvaluationResult.PASS),
    ]:
        db.add(
            PolicyEvaluation(
                deployment_id=deployment.id,
                policy_name=policy,
                metric_name=policy,
                result=result,
                observed_value=412.0,
                threshold=20.0,
                evaluated_at=now + timedelta(microseconds=offset),
            )
        )
    db.commit()


def _deliveries(db: Session) -> list[WebhookDelivery]:
    return list(db.scalars(select(WebhookDelivery)).all())


def test_a_rollback_queues_a_notification_with_its_reason(
    db_session: Session, deployment: Deployment
) -> None:
    _fail_latency(db_session, deployment)
    deployment.status = DeploymentStatus.ROLLING_BACK
    service._transition(
        db_session, deployment, DeploymentStatus.ROLLED_BACK, "rolled back to stable (automatic)"
    )
    db_session.commit()

    [delivery] = _deliveries(db_session)
    assert (delivery.event, delivery.url, delivery.status) == (
        "deployment.rolled_back",
        URL,
        WebhookDeliveryStatus.PENDING,
    )
    payload = delivery.payload
    assert payload["transition"] == {
        "from": "ROLLING_BACK",
        "to": "ROLLED_BACK",
        "reason": "rolled back to stable (automatic)",
        "decided_by": "automatic",
        "operator": None,
    }
    assert [(c["policy_name"], c["result"]) for c in payload["checks"]] == [
        ("latency_p95_increase", "FAIL"),
        ("minimum_positive_labels", "INCONCLUSIVE"),
    ]
    assert "rolled back to v1" in payload["text"]
    assert "latency_p95_increase" in payload["text"]
    assert payload["deployment"]["url"].endswith(f"/deployments/{deployment.id}")


def test_nothing_is_queued_for_intermediate_statuses_or_without_urls(
    db_session: Session, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    service._transition(db_session, deployment, DeploymentStatus.EVALUATING, "evaluating")
    db_session.commit()
    assert _deliveries(db_session) == []

    monkeypatch.setattr(control_plane_settings, "webhook_urls", [])
    service._transition(db_session, deployment, DeploymentStatus.FAILED, "router gone")
    db_session.commit()
    assert _deliveries(db_session) == []


def test_a_decision_that_does_not_commit_sends_nothing(
    db_session: Session, deployment: Deployment
) -> None:
    deployment.status = DeploymentStatus.PROMOTING
    service._transition(db_session, deployment, DeploymentStatus.PROMOTED, "promoted (manual)")
    db_session.rollback()
    assert _deliveries(db_session) == []


def _queue_promotion(db: Session, deployment: Deployment) -> WebhookDelivery:
    deployment.status = DeploymentStatus.PROMOTING
    service._transition(db, deployment, DeploymentStatus.PROMOTED, "promoted (manual)", "alice")
    db.commit()
    return _deliveries(db)[0]


def test_delivery_is_signed_and_marked_delivered(
    db_session: Session, deployment: Deployment
) -> None:
    delivery = _queue_promotion(db_session, deployment)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert dispatcher.deliver_due(db_session, client) == 1

    [request] = seen
    assert request.headers["X-ModelOps-Event"] == "deployment.promoted"
    assert request.headers["X-ModelOps-Delivery"] == delivery.id
    assert request.headers["X-ModelOps-Signature"] == dispatcher.sign(request.content, SECRET)
    body = json.loads(request.content)
    assert body["transition"]["operator"] == "alice"
    assert body["checks"] == []
    db_session.refresh(delivery)
    assert delivery.status == WebhookDeliveryStatus.DELIVERED
    assert delivery.attempts == 1 and delivery.delivered_at is not None


def test_failures_back_off_then_give_up(db_session: Session, deployment: Deployment) -> None:
    delivery = _queue_promotion(db_session, deployment)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused")
        return httpx.Response(500, text="boom")

    now = datetime.now(UTC)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        dispatcher.deliver_due(db_session, client, now)
        db_session.refresh(delivery)
        assert delivery.status == WebhookDeliveryStatus.PENDING
        assert "ConnectError" in (delivery.last_error or "")
        # Not due again until the backoff (2s after the first failure) passes.
        assert dispatcher.deliver_due(db_session, client, now + timedelta(seconds=1)) == 0
        assert dispatcher.deliver_due(db_session, client, now + timedelta(seconds=3)) == 1
        db_session.refresh(delivery)
        assert delivery.last_status_code == 500
        # Third and last attempt (max 3), after a 4s backoff.
        assert dispatcher.deliver_due(db_session, client, now + timedelta(seconds=8)) == 1

    db_session.refresh(delivery)
    assert delivery.status == WebhookDeliveryStatus.FAILED
    assert delivery.attempts == 3 and calls == 3


def test_deliveries_endpoint_lists_and_filters(
    db_session: Session, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No dispatcher thread here: the test reads the queue as it was left.
    monkeypatch.setattr("app.main.webhook_dispatcher.start", lambda sessions: None)
    _queue_promotion(db_session, deployment)

    def _get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as c:
        try:
            rows = c.get("/api/notifications/deliveries").json()
            assert [r["event"] for r in rows] == ["deployment.promoted"]
            assert (
                c.get("/api/notifications/deliveries", params={"status": "DELIVERED"}).json() == []
            )
            other = c.get("/api/notifications/deliveries", params={"deployment_id": "nope"})
            assert other.json() == []
        finally:
            app.dependency_overrides.pop(get_db, None)


def test_sink_records_and_verifies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    body = json.dumps({"deployment": {"id": "d1"}}).encode()
    with TestClient(sink.app) as c:
        good = c.post(
            "/hooks",
            content=body,
            headers={
                "X-ModelOps-Event": "deployment.promoted",
                "X-ModelOps-Signature": dispatcher.sign(body, SECRET),
            },
        )
        bad = c.post(
            "/hooks",
            content=body,
            headers={"X-ModelOps-Event": "deployment.failed", "X-ModelOps-Signature": "sha256=0"},
        )
        assert good.status_code == bad.status_code == 204
        assert (
            c.get("/received", params={"event": "deployment.promoted"}).json()[0][
                "signature_verified"
            ]
            is True
        )
        assert (
            c.get("/received", params={"event": "deployment.failed"}).json()[0][
                "signature_verified"
            ]
            is False
        )
