import pytest

from app.control_plane.models import PolicyEvaluationResult
from app.control_plane.schemas import MetricsSummary
from app.policy.config import LatencyPolicy, PolicyConfig, QualityPolicy, ReliabilityPolicy
from app.policy.engine import evaluate_policies, overall_result

PASS = PolicyEvaluationResult.PASS
FAIL = PolicyEvaluationResult.FAIL
INCONCLUSIVE = PolicyEvaluationResult.INCONCLUSIVE


def _summary(
    version: str = "v1",
    sample_count: int = 200,
    p50: float | None = 10.0,
    p95: float | None = 20.0,
    p99: float | None = 30.0,
    error_rate: float | None = 0.01,
    precision: float | None = None,
    recall: float | None = None,
    false_positive_rate: float | None = None,
) -> MetricsSummary:
    return MetricsSummary(
        version=version,
        sample_count=sample_count,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        p99_latency_ms=p99,
        error_rate=error_rate,
        precision=precision,
        recall=recall,
        false_positive_rate=false_positive_rate,
    )


def _config(**overrides: object) -> PolicyConfig:
    base = {
        "minimum_requests": 100,
        "evaluation_window_seconds": 300,
        "latency": LatencyPolicy(p95_max_increase_percent=20.0),
        "reliability": ReliabilityPolicy(max_error_rate_percent=5.0),
        "quality": QualityPolicy(minimum_recall=0.8),
    }
    base.update(overrides)
    return PolicyConfig(**base)  # type: ignore[arg-type]


# --- minimum_requests gate ---------------------------------------------------


def test_minimum_requests_not_met_short_circuits_to_single_inconclusive_check() -> None:
    stable = _summary(sample_count=10)
    canary = _summary(sample_count=200)
    checks = evaluate_policies(stable, canary, _config(minimum_requests=100))

    assert len(checks) == 1
    assert checks[0].policy_name == "minimum_requests"
    assert checks[0].result == INCONCLUSIVE
    assert checks[0].observed_value == 10
    assert checks[0].threshold == 100
    assert overall_result(checks) == INCONCLUSIVE


def test_minimum_requests_met_runs_all_other_checks() -> None:
    stable = _summary(sample_count=150)
    canary = _summary(sample_count=120)
    checks = evaluate_policies(stable, canary, _config(minimum_requests=100))

    policy_names = {check.policy_name for check in checks}
    assert policy_names == {
        "minimum_requests",
        "latency_p95_increase",
        "max_error_rate",
        "minimum_recall",
    }
    assert checks[0].result == PASS


def test_minimum_requests_uses_the_smaller_of_the_two_counts() -> None:
    stable = _summary(sample_count=500)
    canary = _summary(sample_count=50)
    checks = evaluate_policies(stable, canary, _config(minimum_requests=100))
    assert checks[0].observed_value == 50


# --- latency policy -----------------------------------------------------------


def test_latency_pass_when_within_threshold() -> None:
    stable = _summary(p95=100.0)
    canary = _summary(p95=110.0)  # +10%
    checks = evaluate_policies(
        stable, canary, _config(latency=LatencyPolicy(p95_max_increase_percent=20.0))
    )
    latency_check = next(c for c in checks if c.policy_name == "latency_p95_increase")
    assert latency_check.result == PASS
    assert latency_check.observed_value == pytest.approx(10.0)


def test_latency_fails_when_increase_exceeds_threshold() -> None:
    stable = _summary(p95=100.0)
    canary = _summary(p95=130.0)  # +30%
    checks = evaluate_policies(
        stable, canary, _config(latency=LatencyPolicy(p95_max_increase_percent=20.0))
    )
    latency_check = next(c for c in checks if c.policy_name == "latency_p95_increase")
    assert latency_check.result == FAIL
    assert latency_check.observed_value == pytest.approx(30.0)


def test_latency_inconclusive_when_stable_p95_missing() -> None:
    stable = _summary(p95=None)
    canary = _summary(p95=50.0)
    checks = evaluate_policies(stable, canary, _config())
    latency_check = next(c for c in checks if c.policy_name == "latency_p95_increase")
    assert latency_check.result == INCONCLUSIVE
    assert latency_check.observed_value is None


# --- reliability (error rate) policy --------------------------------------------


def test_error_rate_pass_within_threshold() -> None:
    canary = _summary(error_rate=0.02)
    checks = evaluate_policies(
        _summary(), canary, _config(reliability=ReliabilityPolicy(max_error_rate_percent=5.0))
    )
    check = next(c for c in checks if c.policy_name == "max_error_rate")
    assert check.result == PASS
    assert check.observed_value == pytest.approx(2.0)


def test_error_rate_fails_above_threshold() -> None:
    canary = _summary(error_rate=0.10)
    checks = evaluate_policies(
        _summary(), canary, _config(reliability=ReliabilityPolicy(max_error_rate_percent=5.0))
    )
    check = next(c for c in checks if c.policy_name == "max_error_rate")
    assert check.result == FAIL
    assert check.observed_value == pytest.approx(10.0)


def test_error_rate_inconclusive_when_missing() -> None:
    canary = _summary(error_rate=None)
    checks = evaluate_policies(_summary(), canary, _config())
    check = next(c for c in checks if c.policy_name == "max_error_rate")
    assert check.result == INCONCLUSIVE


# --- quality (recall) policy ----------------------------------------------------


def test_recall_inconclusive_without_actual_label_data() -> None:
    # This is the expected, common state today - no actual_label source exists yet.
    canary = _summary(recall=None)
    checks = evaluate_policies(
        _summary(), canary, _config(quality=QualityPolicy(minimum_recall=0.8))
    )
    check = next(c for c in checks if c.policy_name == "minimum_recall")
    assert check.result == INCONCLUSIVE
    assert check.observed_value is None


def test_recall_fails_below_threshold() -> None:
    canary = _summary(recall=0.5)
    checks = evaluate_policies(
        _summary(), canary, _config(quality=QualityPolicy(minimum_recall=0.8))
    )
    check = next(c for c in checks if c.policy_name == "minimum_recall")
    assert check.result == FAIL
    assert check.observed_value == 0.5


def test_recall_passes_at_or_above_threshold() -> None:
    canary = _summary(recall=0.8)
    checks = evaluate_policies(
        _summary(), canary, _config(quality=QualityPolicy(minimum_recall=0.8))
    )
    check = next(c for c in checks if c.policy_name == "minimum_recall")
    assert check.result == PASS


# --- overall result logic -------------------------------------------------------


def test_overall_fail_beats_inconclusive() -> None:
    stable = _summary(p95=100.0, error_rate=0.01)
    canary = _summary(p95=200.0, error_rate=0.01, recall=None)  # latency FAIL, recall INCONCLUSIVE
    checks = evaluate_policies(
        stable, canary, _config(latency=LatencyPolicy(p95_max_increase_percent=20.0))
    )
    assert overall_result(checks) == FAIL


def test_overall_inconclusive_does_not_get_overridden_by_pass() -> None:
    stable = _summary(error_rate=0.01)
    canary = _summary(error_rate=0.01, recall=None)  # everything else passes, recall unknown
    checks = evaluate_policies(stable, canary, _config())
    assert overall_result(checks) == INCONCLUSIVE


def test_overall_pass_when_everything_passes() -> None:
    stable = _summary(p95=100.0, error_rate=0.01)
    canary = _summary(p95=105.0, error_rate=0.01, recall=0.9)
    checks = evaluate_policies(stable, canary, _config(quality=QualityPolicy(minimum_recall=0.8)))
    assert overall_result(checks) == PASS


def test_overall_result_of_empty_list_is_pass() -> None:
    assert overall_result([]) == PASS
