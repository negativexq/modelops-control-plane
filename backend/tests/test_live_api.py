import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.feed import FeedCursor, poll_changes, prime_cursor
from app.control_plane.fleet import build_summary, decision_actor
from app.control_plane.live_api import change_stream
from app.control_plane.models import (
    Deployment,
    DeploymentEvent,
    DeploymentStatus,
    PolicyEvaluation,
    PolicyEvaluationResult,
)
from app.db import get_db
from app.main import app

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _deployment(
    db: Session,
    *,
    status: DeploymentStatus,
    model_name: str = "fraud-model",
    started: datetime | None = None,
    completed: datetime | None = None,
    final_message: str | None = None,
) -> Deployment:
    d = Deployment(
        model_name=model_name,
        stable_version="v1",
        canary_version="v2-good",
        status=status,
        started_at=started,
        completed_at=completed,
    )
    db.add(d)
    db.flush()
    if final_message is not None:
        db.add(
            DeploymentEvent(
                deployment_id=d.id,
                event_type="status_changed",
                message=final_message,
                created_at=completed or NOW,
            )
        )
    db.commit()
    db.refresh(d)
    return d


# --- change feed ----------------------------------------------------------------


def test_feed_returns_new_events_and_policy_results_once(db_session: Session) -> None:
    d = _deployment(db_session, status=DeploymentStatus.CANARY_RUNNING)
    cursor = prime_cursor(db_session)

    db_session.add(DeploymentEvent(deployment_id=d.id, event_type="traffic_advanced", message="x"))
    db_session.add(
        PolicyEvaluation(
            deployment_id=d.id,
            policy_name="max_error_rate",
            metric_name="error_rate",
            result=PolicyEvaluationResult.FAIL,
        )
    )
    db_session.commit()

    changes = poll_changes(db_session, cursor)
    assert sorted(c["kind"] for c in changes) == ["event", "policy"]
    assert {c["deployment_id"] for c in changes} == {d.id}
    event = next(c for c in changes if c["kind"] == "event")
    assert event["event_type"] == "traffic_advanced"
    policy = next(c for c in changes if c["kind"] == "policy")
    assert policy["result"] == "FAIL"

    assert poll_changes(db_session, cursor) == []


def test_feed_primed_cursor_skips_history(db_session: Session) -> None:
    d = _deployment(db_session, status=DeploymentStatus.CANARY_RUNNING)
    db_session.add(DeploymentEvent(deployment_id=d.id, event_type="created", message="x"))
    db_session.commit()

    assert poll_changes(db_session, prime_cursor(db_session)) == []


def test_feed_catches_rows_stamped_before_the_cursor_but_committed_after(
    db_session: Session,
) -> None:
    """created_at is set before commit, so a slow transaction can land with a
    timestamp older than rows already sent. The lookback window must still pick
    it up."""
    d = _deployment(db_session, status=DeploymentStatus.CANARY_RUNNING)
    cursor = FeedCursor(since=datetime.now(UTC))
    db_session.add(
        DeploymentEvent(
            deployment_id=d.id,
            event_type="late",
            message="x",
            created_at=cursor.since - timedelta(seconds=3),
        )
    )
    db_session.commit()

    assert [c["event_type"] for c in poll_changes(db_session, cursor)] == ["late"]


def test_change_stream_emits_ready_then_changes(db_session: Session) -> None:
    d = _deployment(db_session, status=DeploymentStatus.CANARY_RUNNING)

    class FakeRequest:
        async def is_disconnected(self) -> bool:
            return False

    async def collect() -> list[str]:
        chunks: list[str] = []
        stream = change_stream(
            FakeRequest(),  # type: ignore[arg-type]
            lambda: db_session,
            poll_seconds=0.01,
            max_seconds=0.2,
        )
        async for chunk in stream:
            chunks.append(chunk)
            if chunk.startswith("event: ready"):
                db_session.add(
                    DeploymentEvent(deployment_id=d.id, event_type="paused", message="x")
                )
                db_session.commit()
        return chunks

    # change_stream closes the session after each poll; keep the shared test
    # session usable by making close() a no-op for this test.
    original_close = db_session.close
    db_session.close = lambda: None  # type: ignore[method-assign]
    try:
        chunks = asyncio.run(collect())
    finally:
        db_session.close = original_close  # type: ignore[method-assign]

    assert chunks[0].startswith("retry:")
    assert chunks[1].startswith("event: ready")
    changes = [c for c in chunks if c.startswith("event: change")]
    assert len(changes) == 1
    assert '"event_type":"paused"' in changes[0]


# --- fleet summary --------------------------------------------------------------


def test_decision_actor_reads_the_final_status_message(db_session: Session) -> None:
    auto = _deployment(
        db_session,
        status=DeploymentStatus.ROLLED_BACK,
        final_message="ROLLING_BACK -> ROLLED_BACK: traffic rolled back to stable (automatic)",
    )
    manual = _deployment(
        db_session,
        status=DeploymentStatus.PROMOTED,
        final_message="PROMOTING -> PROMOTED: canary promoted to 100% traffic (manual)",
    )
    untagged = _deployment(db_session, status=DeploymentStatus.FAILED)
    inconclusive = _deployment(
        db_session, status=DeploymentStatus.INCONCLUSIVE, model_name="churn-model"
    )

    assert decision_actor(auto) == "automatic"
    assert decision_actor(manual) == "manual"
    assert decision_actor(untagged) == "unknown"
    assert decision_actor(inconclusive) == "automatic"


def test_summary_counts_outcomes_actors_and_median(db_session: Session) -> None:
    start = NOW - timedelta(hours=2)
    _deployment(
        db_session,
        status=DeploymentStatus.PROMOTED,
        started=start,
        completed=start + timedelta(seconds=60),
        final_message="PROMOTING -> PROMOTED: canary promoted to 100% traffic (automatic)",
    )
    _deployment(
        db_session,
        status=DeploymentStatus.ROLLED_BACK,
        started=start,
        completed=start + timedelta(seconds=120),
        final_message="ROLLING_BACK -> ROLLED_BACK: traffic rolled back to stable (manual)",
    )
    _deployment(
        db_session,
        status=DeploymentStatus.ROLLED_BACK,
        model_name="benchmark-latency-failure",
        started=start,
        completed=start + timedelta(seconds=30),
        final_message="ROLLING_BACK -> ROLLED_BACK: traffic rolled back to stable (automatic)",
    )
    _deployment(db_session, status=DeploymentStatus.CANARY_RUNNING, started=start)
    _deployment(
        db_session, status=DeploymentStatus.INCONCLUSIVE, model_name="churn-model", started=start
    )

    summary = build_summary(db_session, now=NOW)
    assert summary.total == 5
    assert summary.live == 1
    assert summary.by_status["ROLLED_BACK"] == 2
    assert summary.outcomes.ROLLED_BACK.automatic == 1
    assert summary.outcomes.ROLLED_BACK.manual == 1
    assert summary.outcomes.INCONCLUSIVE.total == 1
    assert summary.median_decision_seconds == pytest.approx(60.0)
    assert summary.automation_rate == pytest.approx(2 / 3)
    assert len(summary.daily) == 14
    assert summary.daily[-1].date == NOW.date()
    assert summary.daily[-1].ROLLED_BACK == 2

    without_benchmarks = build_summary(db_session, include_benchmarks=False, now=NOW)
    assert without_benchmarks.total == 4
    assert without_benchmarks.outcomes.ROLLED_BACK.total == 1


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def test_summary_endpoint(client: TestClient, db_session: Session) -> None:
    _deployment(db_session, status=DeploymentStatus.CANARY_RUNNING)
    body: dict[str, Any] = client.get("/api/fleet/summary?days=7").json()
    assert body["live"] == 1
    assert len(body["daily"]) == 7
    assert set(body["outcomes"]) == {"PROMOTED", "ROLLED_BACK", "INCONCLUSIVE", "FAILED"}

    assert client.get("/api/fleet/summary?days=0").status_code == 422
