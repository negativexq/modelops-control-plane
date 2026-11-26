"""Feature drift: the canary's live inputs against its training profile (see
app/policy/drift.py and the feature_drift check in app/policy/engine.py)."""

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.control_plane.config import control_plane_settings
from app.control_plane.models import Deployment, DeploymentStatus
from app.db import get_db
from app.main import app
from app.policy import drift, service
from app.policy.config import DriftPolicy
from app.policy.engine import (
    FAIL,
    INCONCLUSIVE,
    NO_REFERENCE_PROFILE,
    PASS,
    TOO_FEW_INPUTS,
    DriftInputs,
    Evidence,
    PolicyCheckResult,
    _evaluate_feature_drift,
    _evaluate_prediction_drift,
)

ARTIFACT = "fraud-model/v2-good"


def _training_rows(n: int, seed: int = 0) -> dict[str, list[Any]]:
    rng = np.random.default_rng(seed)
    return {
        "amount": rng.lognormal(3.5, 1.0, n).tolist(),
        "feature_0": rng.normal(0, 1, n).tolist(),
        "previous_declines": rng.choice([0, 1, 2], n, p=[0.85, 0.13, 0.02]).tolist(),
        "merchant_category": rng.choice(["grocery", "travel", "crypto"], n).tolist(),
    }


PROFILE = drift.build_reference_profile(
    _training_rows(20_000),
    numeric_features=["amount", "feature_0", "previous_declines"],
    categorical_features=["merchant_category"],
)


def _scores(n: int, seed: int, fraud_share: float = 0.02) -> list[float]:
    """Fraud scores: mostly near zero, high for the fraud share."""
    rng = np.random.default_rng(seed)
    is_fraud = rng.random(n) < fraud_share
    return np.where(is_fraud, rng.beta(8, 2, n), rng.beta(1, 60, n)).tolist()


PROFILE["prediction"] = drift.build_reference_profile(
    {drift.PREDICTION_SCORE: _scores(20_000, seed=0)}, [drift.PREDICTION_SCORE], []
)["features"][drift.PREDICTION_SCORE]


def _inputs(n: int, seed: int, amount_scale: float = 1.0) -> list[dict[str, Any]]:
    rows = _training_rows(n, seed)
    rows["amount"] = [a * amount_scale for a in rows["amount"]]
    return [dict(zip(rows, values, strict=True)) for values in zip(*rows.values(), strict=True)]


def _check(
    inputs: list[dict[str, Any]], profile: dict[str, Any] | None = PROFILE
) -> PolicyCheckResult:
    return _evaluate_feature_drift(
        DriftInputs(artifact=ARTIFACT, profile=profile, inputs=inputs),
        DriftPolicy(),
        Evidence(0.975, look=1),
    )


def test_profile_records_training_proportions_per_bin() -> None:
    amount = PROFILE["features"]["amount"]
    assert amount["kind"] == "numeric"
    assert len(amount["edges"]) == 9
    assert sum(amount["proportions"]) == pytest.approx(1.0)
    # An integer feature with few values collapses to fewer bins.
    declines = PROFILE["features"]["previous_declines"]
    assert declines["proportions"] == pytest.approx([0.85, 0.13, 0.02], abs=0.01)
    categories = PROFILE["features"]["merchant_category"]
    assert categories["categories"] == ["crypto", "grocery", "travel"]
    # A last slot for categories training never saw.
    assert categories["proportions"][-1] == 0


def test_traffic_like_the_training_data_passes() -> None:
    check = _check(_inputs(300, seed=1))
    assert check.result == PASS
    assert check.observed_value is not None and check.observed_value < 0.1
    assert check.method == "psi+chi_square_bonferroni"


def test_sample_size_correction_keeps_small_healthy_windows_passing() -> None:
    # Raw PSI over 100 requests is inflated by sampling noise alone.
    results = [_check(_inputs(100, seed=s)).result for s in range(40)]
    assert results.count(PASS) >= 38
    assert FAIL not in results


def test_a_shifted_feature_fails_and_is_named() -> None:
    check = _check(_inputs(300, seed=2, amount_scale=3.0))
    assert check.result == FAIL
    assert check.detail == "amount"
    assert check.observed_value is not None and check.observed_value > 0.25
    assert check.p_value is not None and check.p_value < 0.0125


def test_an_unseen_category_counts_as_drift() -> None:
    inputs = [dict(row, merchant_category="weapons") for row in _inputs(300, seed=3)]
    check = _check(inputs)
    assert check.result == FAIL
    assert check.detail == "merchant_category"


def test_too_few_inputs_or_no_profile_is_inconclusive() -> None:
    few = _check(_inputs(drift.MINIMUM_SAMPLES - 1, seed=4, amount_scale=3.0))
    assert (few.result, few.detail) == (INCONCLUSIVE, TOO_FEW_INPUTS)
    missing = _check(_inputs(300, seed=4), profile=None)
    assert missing.result == INCONCLUSIVE
    assert missing.detail == f"{NO_REFERENCE_PROFILE}:{ARTIFACT}"


def _prediction_check(
    scores: list[float], profile: dict[str, Any] | None = PROFILE
) -> PolicyCheckResult:
    return _evaluate_prediction_drift(
        DriftInputs(artifact=ARTIFACT, profile=profile, inputs=[], scores=scores),
        DriftPolicy(),
        Evidence(0.975, look=1),
    )


def test_scores_like_validation_pass() -> None:
    results = [_prediction_check(_scores(150, seed=s)).result for s in range(1, 30)]
    assert FAIL not in results
    assert results.count(PASS) >= 27


def test_a_fraud_wave_shifts_the_scores_and_fails() -> None:
    # Ten times the fraud share: the model's output moves, whatever each input did.
    check = _prediction_check(_scores(300, seed=7, fraud_share=0.25))
    assert check.result == FAIL
    assert check.observed_value is not None and check.observed_value > 0.1
    assert check.detail == drift.PREDICTION_SCORE


def test_prediction_drift_needs_scores_and_a_validation_profile() -> None:
    few = _prediction_check(_scores(drift.MINIMUM_SAMPLES - 1, seed=8, fraud_share=0.5))
    assert (few.result, few.detail) == (INCONCLUSIVE, TOO_FEW_INPUTS)
    old_profile = {k: v for k, v in PROFILE.items() if k != "prediction"}
    missing = _prediction_check(_scores(300, seed=8), profile=old_profile)
    assert missing.result == INCONCLUSIVE
    assert missing.detail == f"{NO_REFERENCE_PROFILE}:{ARTIFACT}"


# --- end to end: metrics in through the API, drift check out of /evaluate ----------


@pytest.fixture
def artifacts_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    version_dir = tmp_path / ARTIFACT
    version_dir.mkdir(parents=True)
    for name in ("model.joblib", "metadata.json", "evaluation.json"):
        (version_dir / name).write_text("{}")
    (version_dir / "reference_profile.json").write_text(json.dumps(PROFILE))
    monkeypatch.setattr(control_plane_settings, "artifacts_dir", tmp_path)
    service._reference_profile.cache_clear()
    yield tmp_path
    service._reference_profile.cache_clear()


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
        canary_version="v2-good-latency-fault",
        status=DeploymentStatus.CANARY_RUNNING,
    )
    db_session.add(d)
    db_session.commit()
    return d


def _post(
    client: TestClient,
    deployment: Deployment,
    version: str,
    inputs: list[dict[str, Any]] | None,
    count: int = 200,
    scores: list[float] | None = None,
) -> None:
    for i in range(count):
        body: dict[str, Any] = {"model_version": version, "latency_ms": 20.0, "status_code": 200}
        if inputs is not None:
            body |= {"features": inputs[i], "artifact": ARTIFACT}
        if scores is not None:
            body["score"] = scores[i]
        response = client.post(f"/api/deployments/{deployment.id}/metrics", json=body)
        assert response.status_code in (200, 201, 202, 204)


def _drift_check(
    client: TestClient, deployment: Deployment, policy: str = "feature_drift", **config: Any
) -> dict[str, Any] | None:
    body = client.post(
        f"/api/deployments/{deployment.id}/evaluate", json={"minimum_requests": 50, **config}
    ).json()
    return next((c for c in body["checks"] if c["policy_name"] == policy), None)


def test_evaluate_fails_a_canary_fed_drifted_inputs(
    client: TestClient, deployment: Deployment, artifacts_dir: Path
) -> None:
    _post(client, deployment, "v1", None)
    # The router label differs from the artifact; the metric's artifact names it.
    _post(client, deployment, "v2-good-latency-fault", _inputs(200, seed=5, amount_scale=4.0))

    check = _drift_check(client, deployment)
    assert check is not None
    assert check["result"] == "FAIL"
    assert check["detail"] == "amount"

    timeline = client.get(f"/api/deployments/{deployment.id}/timeline").json()
    explanation = next(
        item["explanation"] for item in timeline if item.get("policy_name") == "feature_drift"
    )
    assert "'amount'" in explanation and "drifted" in explanation


def test_evaluate_passes_healthy_inputs_and_skips_without_inputs_or_limit(
    client: TestClient, deployment: Deployment, artifacts_dir: Path
) -> None:
    _post(client, deployment, "v1", None)
    _post(client, deployment, "v2-good-latency-fault", None)
    # No metric carried inputs: nothing to compare, so no check at all.
    assert _drift_check(client, deployment) is None

    _post(client, deployment, "v2-good-latency-fault", _inputs(200, seed=6))
    check = _drift_check(client, deployment)
    assert check is not None and check["result"] == "PASS"

    # Switched off by the policy.
    assert _drift_check(client, deployment, drift={"max_feature_psi": None}) is None


def test_evaluate_fails_a_canary_whose_scores_shifted(
    client: TestClient, deployment: Deployment, artifacts_dir: Path
) -> None:
    _post(client, deployment, "v1", None)
    _post(
        client,
        deployment,
        "v2-good-latency-fault",
        _inputs(200, seed=9),
        scores=_scores(200, seed=9, fraud_share=0.3),
    )
    # Inputs unchanged, scores moved: only prediction drift sees it.
    assert (_drift_check(client, deployment) or {}).get("result") == "PASS"
    check = _drift_check(client, deployment, "prediction_drift")
    assert check is not None and check["result"] == "FAIL"
    off = {"drift": {"max_prediction_psi": None}}
    assert _drift_check(client, deployment, "prediction_drift", **off) is None
