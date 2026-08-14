import pytest

from app.control_plane.models import PolicyEvaluationResult
from app.control_plane.schemas import MetricsSummary
from app.policy.config import LatencyPolicy, PolicyConfig, QualityPolicy, ReliabilityPolicy
from app.policy.engine import evaluate_policies, evaluate_quality_policies, overall_result

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
    # Defaults deliberately "well labeled" (comfortably above PolicyConfig's own
    # defaults of 30 / 0.5 / 30) so a test that isn't about the quality gate
    # itself doesn't accidentally trip it just by reusing this helper for
    # canary_quality - tests that ARE about the gate override these explicitly.
    labeled_sample_count: int = 50,
    label_coverage: float | None = 0.9,
    positive_label_count: int = 40,
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
        labeled_sample_count=labeled_sample_count,
        label_coverage=label_coverage,
        positive_label_count=positive_label_count,
        label_delay_p50_seconds=None,
        label_delay_p95_seconds=None,
    )


def _config(**overrides: object) -> PolicyConfig:
    base = {
        "minimum_requests": 100,
        "evaluation_window_seconds": 300,
        "label_maturity_seconds": 60,
        "minimum_labeled_samples": 30,
        "minimum_label_coverage": 0.5,
        "minimum_positive_labels": 30,
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
    checks = evaluate_policies(stable, canary, canary, _config(minimum_requests=100))

    assert len(checks) == 1
    assert checks[0].policy_name == "minimum_requests"
    assert checks[0].result == INCONCLUSIVE
    assert checks[0].observed_value == 10
    assert checks[0].threshold == 100
    assert overall_result(checks) == INCONCLUSIVE


def test_minimum_requests_met_runs_all_other_checks() -> None:
    stable = _summary(sample_count=150)
    canary = _summary(sample_count=120)
    checks = evaluate_policies(stable, canary, canary, _config(minimum_requests=100))

    policy_names = {check.policy_name for check in checks}
    assert policy_names == {
        "minimum_requests",
        "latency_p95_increase",
        "max_error_rate",
        "minimum_labeled_samples",
        "minimum_label_coverage",
        "minimum_positive_labels",
        "minimum_recall",
    }
    assert checks[0].result == PASS


def test_minimum_requests_uses_the_smaller_of_the_two_counts() -> None:
    stable = _summary(sample_count=500)
    canary = _summary(sample_count=50)
    checks = evaluate_policies(stable, canary, canary, _config(minimum_requests=100))
    assert checks[0].observed_value == 50


# --- latency policy -----------------------------------------------------------


def test_latency_pass_when_within_threshold() -> None:
    stable = _summary(p95=100.0)
    canary = _summary(p95=110.0)  # +10%
    checks = evaluate_policies(
        stable, canary, canary, _config(latency=LatencyPolicy(p95_max_increase_percent=20.0))
    )
    latency_check = next(c for c in checks if c.policy_name == "latency_p95_increase")
    assert latency_check.result == PASS
    assert latency_check.observed_value == pytest.approx(10.0)


def test_latency_fails_when_increase_exceeds_threshold() -> None:
    stable = _summary(p95=100.0)
    canary = _summary(p95=130.0)  # +30%
    checks = evaluate_policies(
        stable, canary, canary, _config(latency=LatencyPolicy(p95_max_increase_percent=20.0))
    )
    latency_check = next(c for c in checks if c.policy_name == "latency_p95_increase")
    assert latency_check.result == FAIL
    assert latency_check.observed_value == pytest.approx(30.0)


def test_latency_inconclusive_when_stable_p95_missing() -> None:
    stable = _summary(p95=None)
    canary = _summary(p95=50.0)
    checks = evaluate_policies(stable, canary, canary, _config())
    latency_check = next(c for c in checks if c.policy_name == "latency_p95_increase")
    assert latency_check.result == INCONCLUSIVE
    assert latency_check.observed_value is None


# --- reliability (error rate) policy --------------------------------------------


def test_error_rate_pass_within_threshold() -> None:
    canary = _summary(error_rate=0.02)
    reliability = ReliabilityPolicy(max_error_rate_percent=5.0)
    checks = evaluate_policies(_summary(), canary, canary, _config(reliability=reliability))
    check = next(c for c in checks if c.policy_name == "max_error_rate")
    assert check.result == PASS
    assert check.observed_value == pytest.approx(2.0)


def test_error_rate_fails_above_threshold() -> None:
    canary = _summary(error_rate=0.10)
    reliability = ReliabilityPolicy(max_error_rate_percent=5.0)
    checks = evaluate_policies(_summary(), canary, canary, _config(reliability=reliability))
    check = next(c for c in checks if c.policy_name == "max_error_rate")
    assert check.result == FAIL
    assert check.observed_value == pytest.approx(10.0)


def test_error_rate_inconclusive_when_missing() -> None:
    canary = _summary(error_rate=None)
    checks = evaluate_policies(_summary(), canary, canary, _config())
    check = next(c for c in checks if c.policy_name == "max_error_rate")
    assert check.result == INCONCLUSIVE


# --- quality data-sufficiency gate (minimum_labeled_samples / minimum_label_coverage /
# minimum_positive_labels) -------------------------------------------------------

_GATE_POLICY_NAMES = {
    "minimum_labeled_samples",
    "minimum_label_coverage",
    "minimum_positive_labels",
}


def test_quality_gate_inconclusive_when_too_few_labeled_samples() -> None:
    canary_quality = _summary(
        recall=0.95, labeled_sample_count=5, label_coverage=0.9, positive_label_count=5
    )
    checks = evaluate_quality_policies(
        canary_quality, _config(minimum_labeled_samples=30, minimum_label_coverage=0.5)
    )
    # minimum_recall must NOT run - "not enough labeled data" and "recall is bad"
    # are different findings, and a PASS-worthy recall=0.95 must never leak through
    # a gate that hasn't itself been satisfied.
    assert {c.policy_name for c in checks} == _GATE_POLICY_NAMES
    labeled_check = next(c for c in checks if c.policy_name == "minimum_labeled_samples")
    assert labeled_check.result == INCONCLUSIVE
    assert labeled_check.observed_value == 5


def test_quality_gate_inconclusive_when_coverage_below_threshold() -> None:
    """The headline scenario: plenty of raw predictions, but too few of them are
    labeled relative to the total - 100 predictions / 5 labeled / recall=1.0 on
    those 5 must stay INCONCLUSIVE, not PASS."""
    canary_quality = _summary(
        recall=1.0, labeled_sample_count=5, label_coverage=0.05, positive_label_count=5
    )
    checks = evaluate_quality_policies(
        canary_quality, _config(minimum_labeled_samples=1, minimum_label_coverage=0.5)
    )
    assert {c.policy_name for c in checks} == _GATE_POLICY_NAMES
    coverage_check = next(c for c in checks if c.policy_name == "minimum_label_coverage")
    assert coverage_check.result == INCONCLUSIVE
    assert coverage_check.observed_value == pytest.approx(0.05)


def test_quality_gate_inconclusive_when_too_few_positive_labels() -> None:
    """The bug this gate exists to catch: a low-positive-rate dataset (e.g. ~2%
    fraud) can clear minimum_labeled_samples and minimum_label_coverage while the
    window still holds only a handful of positive examples - 71 labeled / 3
    positive, which if evaluated directly would compute a recall of 1/3 (FAIL
    territory), must instead stay INCONCLUSIVE with minimum_recall never run.
    """
    canary_quality = _summary(
        recall=0.333, labeled_sample_count=71, label_coverage=0.9, positive_label_count=3
    )
    checks = evaluate_quality_policies(
        canary_quality,
        _config(minimum_labeled_samples=30, minimum_label_coverage=0.5, minimum_positive_labels=30),
    )
    assert {c.policy_name for c in checks} == _GATE_POLICY_NAMES
    positive_check = next(c for c in checks if c.policy_name == "minimum_positive_labels")
    assert positive_check.result == INCONCLUSIVE
    assert positive_check.observed_value == 3
    assert positive_check.threshold == 30
    labeled_check = next(c for c in checks if c.policy_name == "minimum_labeled_samples")
    coverage_check = next(c for c in checks if c.policy_name == "minimum_label_coverage")
    assert labeled_check.result == PASS
    assert coverage_check.result == PASS


def test_quality_gate_passes_and_runs_minimum_recall_when_data_is_sufficient() -> None:
    canary_quality = _summary(
        recall=0.9, labeled_sample_count=50, label_coverage=0.9, positive_label_count=40
    )
    checks = evaluate_quality_policies(
        canary_quality,
        _config(minimum_labeled_samples=30, minimum_label_coverage=0.5, minimum_positive_labels=30),
    )
    assert {c.policy_name for c in checks} == {*_GATE_POLICY_NAMES, "minimum_recall"}
    assert all(c.result == PASS for c in checks if c.policy_name != "minimum_recall")


# --- quality (recall) policy ----------------------------------------------------


def test_recall_inconclusive_without_actual_label_data() -> None:
    # Data-sufficiency gate passes (well-labeled defaults) but recall itself is
    # still None - e.g. every labeled sample happened to land for the other
    # version, an edge case the gate alone doesn't rule out.
    canary_quality = _summary(recall=None)
    checks = evaluate_policies(
        _summary(), _summary(), canary_quality, _config(quality=QualityPolicy(minimum_recall=0.8))
    )
    check = next(c for c in checks if c.policy_name == "minimum_recall")
    assert check.result == INCONCLUSIVE
    assert check.observed_value is None


def test_recall_fails_below_threshold() -> None:
    canary_quality = _summary(recall=0.5)
    checks = evaluate_policies(
        _summary(), _summary(), canary_quality, _config(quality=QualityPolicy(minimum_recall=0.8))
    )
    check = next(c for c in checks if c.policy_name == "minimum_recall")
    assert check.result == FAIL
    assert check.observed_value == 0.5


def test_recall_passes_at_or_above_threshold() -> None:
    canary_quality = _summary(recall=0.8)
    checks = evaluate_policies(
        _summary(), _summary(), canary_quality, _config(quality=QualityPolicy(minimum_recall=0.8))
    )
    check = next(c for c in checks if c.policy_name == "minimum_recall")
    assert check.result == PASS


# --- overall result logic -------------------------------------------------------


def test_overall_fail_beats_inconclusive() -> None:
    stable = _summary(p95=100.0, error_rate=0.01)
    canary = _summary(p95=200.0, error_rate=0.01, recall=None)  # latency FAIL, recall INCONCLUSIVE
    checks = evaluate_policies(
        stable, canary, canary, _config(latency=LatencyPolicy(p95_max_increase_percent=20.0))
    )
    assert overall_result(checks) == FAIL


def test_overall_inconclusive_does_not_get_overridden_by_pass() -> None:
    stable = _summary(error_rate=0.01)
    canary = _summary(error_rate=0.01, recall=None)  # everything else passes, recall unknown
    checks = evaluate_policies(stable, canary, canary, _config())
    assert overall_result(checks) == INCONCLUSIVE


def test_overall_pass_when_everything_passes() -> None:
    stable = _summary(p95=100.0, error_rate=0.01)
    canary = _summary(p95=105.0, error_rate=0.01, recall=0.9)
    checks = evaluate_policies(
        stable, canary, canary, _config(quality=QualityPolicy(minimum_recall=0.8))
    )
    assert overall_result(checks) == PASS


def test_overall_result_of_empty_list_is_pass() -> None:
    assert overall_result([]) == PASS
