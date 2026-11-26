"""Sequential testing: a rollout is evaluated over and over, and the error budget
of the policy's confidence level is spent across those looks (see
app/policy/stats.py's look_confidence)."""

from collections.abc import Iterator

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.models import Deployment, DeploymentStatus, PolicyEvaluation
from app.control_plane.schemas import MetricsSummary
from app.db import get_db
from app.main import app
from app.policy import stats
from app.policy.config import PolicyConfig, ReliabilityPolicy
from app.policy.engine import FAIL, evaluate_policies
from tests.test_policy_api import _add_metrics


def _summary(version: str, sample_count: int, error_rate: float) -> MetricsSummary:
    return MetricsSummary(
        version=version,
        sample_count=sample_count,
        p50_latency_ms=20.0,
        p95_latency_ms=20.0,
        p99_latency_ms=20.0,
        error_rate=error_rate,
        precision=None,
        recall=None,
        false_positive_rate=None,
        labeled_sample_count=0,
        label_coverage=0.0,
        positive_label_count=0,
        label_delay_p50_seconds=None,
        label_delay_p95_seconds=None,
    )


def test_look_confidence_spends_exactly_the_budget() -> None:
    assert stats.look_confidence(0.95, 1) == pytest.approx(0.975)
    assert stats.look_confidence(0.95, 2) == pytest.approx(1 - 0.05 / 6)
    spent = sum(1 - stats.look_confidence(0.95, k) for k in range(1, 100_000))
    assert spent == pytest.approx(0.05, rel=1e-4)
    with pytest.raises(ValueError):
        stats.look_confidence(0.95, 0)


def _false_rollback_rate(*, sequential: bool, rollouts: int = 600) -> float:
    """A canary whose true error rate sits exactly at the 5% limit - as bad as a
    canary can be while still deserving to pass - evaluated 40 times over a
    sliding window of 200 requests that moves 10 requests per look, the way the
    worker re-reads an overlapping window every cycle."""
    config = PolicyConfig(
        minimum_requests=1,
        sequential_testing=sequential,
        reliability=ReliabilityPolicy(max_error_rate_percent=5.0),
    )
    rng = np.random.default_rng(7)
    window, step, looks = 200, 10, 40
    stable = _summary("v1", window, 0.0)
    failed = 0
    for _ in range(rollouts):
        stream = rng.random(window + step * looks) < 0.05
        for look in range(1, looks + 1):
            end = window + step * (look - 1)
            errors = int(stream[end - window : end].sum())
            canary = _summary("v2", window, errors / window)
            checks = evaluate_policies(stable, canary, canary, config, None, look=look)
            if next(c for c in checks if c.policy_name == "max_error_rate").result == FAIL:
                failed += 1
                break
    return failed / rollouts


def test_repeated_looks_without_correction_roll_back_healthy_canaries() -> None:
    # Each look alone is a 2.5% one-sided test; forty of them are not.
    assert _false_rollback_rate(sequential=False) > 0.10


def test_sequential_testing_keeps_false_rollbacks_within_the_level() -> None:
    assert _false_rollback_rate(sequential=True) <= 0.025


def test_a_clearly_broken_canary_still_fails_on_a_late_look() -> None:
    config = PolicyConfig(minimum_requests=1)
    stable = _summary("v1", 400, 0.0)
    canary = _summary("v2", 400, 0.30)
    checks = evaluate_policies(stable, canary, canary, config, None, look=50)
    error_check = next(c for c in checks if c.policy_name == "max_error_rate")
    assert error_check.result == FAIL
    assert error_check.look == 50
    assert error_check.confidence_level == pytest.approx(stats.look_confidence(0.95, 50))


def test_without_a_look_or_with_sequential_testing_off_the_level_is_unchanged() -> None:
    stable = _summary("v1", 200, 0.0)
    canary = _summary("v2", 200, 0.01)
    for config, look in ((PolicyConfig(minimum_requests=1), None),
                         (PolicyConfig(minimum_requests=1, sequential_testing=False), 9)):
        checks = evaluate_policies(stable, canary, canary, config, None, look=look)
        error_check = next(c for c in checks if c.policy_name == "max_error_rate")
        assert error_check.confidence_level == pytest.approx(0.95)
        assert error_check.look is None


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    def _get_db() -> Iterator[Session]:
        yield db_session

    app.dependency_overrides[get_db] = _get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


def test_looks_count_only_evaluations_that_ran_a_test(
    client: TestClient, db_session: Session
) -> None:
    deployment = Deployment(
        model_name="fraud-model",
        stable_version="v1",
        canary_version="v2-good",
        status=DeploymentStatus.CANARY_RUNNING,
    )
    db_session.add(deployment)
    db_session.commit()
    url = f"/api/deployments/{deployment.id}/evaluate"

    # Too little traffic: only minimum_requests runs, nothing is tested, no look spent.
    _add_metrics(db_session, deployment.id, "v1", count=5)
    _add_metrics(db_session, deployment.id, "v2-good", count=5)
    client.post(url, json={"minimum_requests": 20})

    _add_metrics(db_session, deployment.id, "v1", count=20)
    _add_metrics(db_session, deployment.id, "v2-good", count=20)
    first = client.post(url, json={"minimum_requests": 20}).json()
    second = client.post(url, json={"minimum_requests": 20}).json()

    def error_check(body: dict[str, object]) -> dict[str, object]:
        checks = body["checks"]
        assert isinstance(checks, list)
        return next(c for c in checks if c["policy_name"] == "max_error_rate")

    assert error_check(first)["sequential_look"] == 1
    assert error_check(first)["confidence_level"] == pytest.approx(0.975)
    assert error_check(second)["sequential_look"] == 2
    assert error_check(second)["confidence_level"] == pytest.approx(1 - 0.05 / 6)

    stored = db_session.scalars(
        select(PolicyEvaluation.sequential_look).where(
            PolicyEvaluation.deployment_id == deployment.id,
            PolicyEvaluation.policy_name == "max_error_rate",
        )
    ).all()
    assert sorted(look for look in stored if look is not None) == [1, 2]

    timeline = client.get(f"/api/deployments/{deployment.id}/timeline").json()
    explanations = [
        item["explanation"]
        for item in timeline
        if item.get("policy_name") == "max_error_rate"
    ]
    assert any("look 2" in text and "99.2%" in text for text in explanations)
