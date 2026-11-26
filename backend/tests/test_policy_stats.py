"""The statistical decision rule: a breach must be outside the noise to FAIL."""

import random

import pytest

from app.control_plane.models import PolicyEvaluationResult
from app.control_plane.schemas import MetricsSummary
from app.policy import stats
from app.policy.config import LatencyPolicy, PolicyConfig, QualityPolicy, ReliabilityPolicy
from app.policy.engine import LatencySamples, evaluate_policies
from app.policy.explain import explain_policy_check

PASS = PolicyEvaluationResult.PASS
FAIL = PolicyEvaluationResult.FAIL
INCONCLUSIVE = PolicyEvaluationResult.INCONCLUSIVE


def _summary(
    sample_count: int,
    error_rate: float,
    p95: float = 20.0,
    recall: float | None = 0.9,
    positives: int = 40,
) -> MetricsSummary:
    return MetricsSummary(
        version="v",
        sample_count=sample_count,
        p50_latency_ms=10.0,
        p95_latency_ms=p95,
        p99_latency_ms=p95,
        error_rate=error_rate,
        precision=None,
        recall=recall,
        false_positive_rate=None,
        labeled_sample_count=50,
        label_coverage=0.9,
        positive_label_count=positives,
        label_delay_p50_seconds=None,
        label_delay_p95_seconds=None,
    )


def _config(**overrides: object) -> PolicyConfig:
    base: dict[str, object] = {
        "minimum_requests": 20,
        "minimum_labeled_samples": 30,
        "minimum_label_coverage": 0.5,
        "minimum_positive_labels": 30,
        "latency": LatencyPolicy(p95_max_increase_percent=20.0),
        "reliability": ReliabilityPolicy(max_error_rate_percent=5.0),
        "quality": QualityPolicy(minimum_recall=0.8),
    }
    base.update(overrides)
    return PolicyConfig(**base)  # type: ignore[arg-type]


def _check(checks, name):  # type: ignore[no-untyped-def]
    return next(c for c in checks if c.policy_name == name)


# --- the primitives ----------------------------------------------------------


def test_wilson_interval_matches_the_textbook_value() -> None:
    # 2 of 25: Wilson 95% interval is about [2.2%, 25.0%].
    interval = stats.wilson_interval(2, 25, 0.95)
    assert interval is not None
    assert interval.low == pytest.approx(0.0222, abs=1e-3)
    assert interval.high == pytest.approx(0.2497, abs=1e-3)


def test_wilson_interval_stays_inside_zero_one_at_the_edges() -> None:
    zero = stats.wilson_interval(0, 30, 0.95)
    every = stats.wilson_interval(30, 30, 0.95)
    assert zero is not None and every is not None
    assert zero.low == 0.0 and 0 < zero.high < 0.15
    assert every.high == pytest.approx(1.0) and 0.85 < every.low < 1


def test_wilson_interval_is_none_without_trials() -> None:
    assert stats.wilson_interval(0, 0, 0.95) is None


def test_binomial_p_values_point_the_right_way() -> None:
    assert stats.binomial_p_above(40, 100, 0.05) < 1e-10  # 40% when the limit is 5%
    assert stats.binomial_p_above(0, 100, 0.05) == pytest.approx(1.0)
    assert stats.binomial_p_below(10, 40, 0.8) < 1e-10  # recall 0.25 vs 0.8


def test_p95_interval_is_deterministic_and_brackets_the_truth() -> None:
    rng = random.Random(1)
    stable = [rng.gauss(100, 10) for _ in range(400)]
    canary = [rng.gauss(150, 15) for _ in range(400)]
    first = stats.p95_increase_interval(stable, canary, 0.95)
    second = stats.p95_increase_interval(stable, canary, 0.95)
    assert first == second
    assert first is not None
    # True p95s: 100+1.645*10 = 116.4, 150+1.645*15 = 174.7 -> +50%.
    assert first.low < 50 < first.high


def test_latency_shift_test_asks_the_policys_question() -> None:
    rng = random.Random(2)
    stable = [rng.gauss(100, 5) for _ in range(300)]
    # 5% slower: significant if the question were "slower at all?", not when it's
    # "slower by more than 20%?".
    slightly = [x * 1.05 for x in stable]
    much = [x * 1.6 for x in stable]
    assert stats.latency_shift_p_value(stable, slightly, 20.0) > 0.5
    assert stats.latency_shift_p_value(stable, much, 20.0) < 1e-6


# --- the decision rule -------------------------------------------------------


def test_error_rate_over_the_limit_by_noise_is_inconclusive_not_fail() -> None:
    # 2 errors in 25 = 8% > 5%, but a healthy 2-3% model does that by chance.
    checks = evaluate_policies(
        _summary(200, 0.0), _summary(25, 0.08), _summary(25, 0.08), _config()
    )
    check = _check(checks, "max_error_rate")
    assert check.result == INCONCLUSIVE
    assert check.ci_low is not None and check.ci_low < 5.0 < check.ci_high
    assert check.p_value is not None and check.p_value > 0.05


def test_error_rate_clearly_over_the_limit_fails_with_evidence() -> None:
    checks = evaluate_policies(_summary(200, 0.0), _summary(60, 0.5), _summary(60, 0.5), _config())
    check = _check(checks, "max_error_rate")
    assert check.result == FAIL
    assert check.ci_low is not None and check.ci_low > 5.0
    assert check.p_value is not None and check.p_value < 1e-6
    assert check.method == "wilson+binomial"


def test_error_rate_within_the_limit_passes() -> None:
    checks = evaluate_policies(
        _summary(200, 0.0), _summary(100, 0.01), _summary(100, 0.01), _config()
    )
    assert _check(checks, "max_error_rate").result == PASS


def test_a_higher_confidence_level_needs_more_evidence_to_fail() -> None:
    canary = _summary(60, 0.12)  # 7 of 60
    at_90 = _check(
        evaluate_policies(_summary(200, 0), canary, canary, _config(confidence_level=0.9)),
        "max_error_rate",
    )
    at_999 = _check(
        evaluate_policies(_summary(200, 0), canary, canary, _config(confidence_level=0.999)),
        "max_error_rate",
    )
    assert at_90.result == FAIL
    assert at_999.result == INCONCLUSIVE


def test_latency_regression_with_samples_fails_with_an_interval() -> None:
    rng = random.Random(3)
    stable = [rng.uniform(5, 15) for _ in range(150)]
    canary = [400 + rng.uniform(5, 15) for _ in range(40)]
    samples = LatencySamples(stable=stable, canary=canary)
    checks = evaluate_policies(
        _summary(150, 0.0, p95=15),
        _summary(40, 0.0, p95=415),
        _summary(40, 0.0),
        _config(),
        samples,
    )
    check = _check(checks, "latency_p95_increase")
    assert check.result == FAIL
    assert check.ci_low is not None and check.ci_low > 20
    assert check.p_value is not None and check.p_value < 1e-6


def test_latency_over_the_limit_on_a_handful_of_requests_is_inconclusive() -> None:
    # Same distribution on both sides; with 25 canary requests a p95 25% higher is noise.
    rng = random.Random(4)
    stable = [rng.lognormvariate(3, 0.6) for _ in range(120)]
    canary = [rng.lognormvariate(3, 0.6) for _ in range(25)]
    samples = LatencySamples(stable=stable, canary=canary)
    checks = evaluate_policies(
        _summary(120, 0.0, p95=20), _summary(25, 0.0, p95=25), _summary(25, 0.0), _config(), samples
    )
    check = _check(checks, "latency_p95_increase")
    assert check.result == INCONCLUSIVE
    assert check.ci_low is not None and check.ci_low < 20 < check.ci_high


def test_recall_clearly_below_the_minimum_fails_and_borderline_waits() -> None:
    bad = _summary(100, 0.0, recall=0.3, positives=40)
    borderline = _summary(100, 0.0, recall=0.75, positives=40)
    assert (
        _check(evaluate_policies(_summary(100, 0), bad, bad, _config()), "minimum_recall").result
        == FAIL
    )
    assert (
        _check(
            evaluate_policies(_summary(100, 0), borderline, borderline, _config()), "minimum_recall"
        ).result
        == INCONCLUSIVE
    )


def test_explanation_quotes_the_interval_and_says_why_it_waits() -> None:
    text = explain_policy_check(
        policy_name="max_error_rate",
        observed_value=8.0,
        threshold=5.0,
        result=INCONCLUSIVE,
        ci_low=2.2,
        ci_high=25.0,
        confidence_level=0.95,
    )
    assert "95% CI 2.20% to 25.00%" in text
    assert "could be noise" in text
