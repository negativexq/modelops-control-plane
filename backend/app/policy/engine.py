from dataclasses import dataclass

from app.control_plane.models import PolicyEvaluationResult
from app.control_plane.schemas import MetricsSummary
from app.policy.config import LatencyPolicy, PolicyConfig, QualityPolicy, ReliabilityPolicy

PASS = PolicyEvaluationResult.PASS
FAIL = PolicyEvaluationResult.FAIL
INCONCLUSIVE = PolicyEvaluationResult.INCONCLUSIVE


@dataclass(frozen=True)
class PolicyCheckResult:
    policy_name: str
    metric_name: str
    observed_value: float | None
    threshold: float | None
    result: PolicyEvaluationResult


def _evaluate_minimum_requests(
    stable: MetricsSummary, canary: MetricsSummary, minimum_requests: int
) -> PolicyCheckResult:
    observed = min(stable.sample_count, canary.sample_count)
    result = PASS if observed >= minimum_requests else INCONCLUSIVE
    return PolicyCheckResult(
        policy_name="minimum_requests",
        metric_name="sample_count",
        observed_value=float(observed),
        threshold=float(minimum_requests),
        result=result,
    )


def _evaluate_latency(
    stable: MetricsSummary, canary: MetricsSummary, policy: LatencyPolicy
) -> PolicyCheckResult:
    if not stable.p95_latency_ms or canary.p95_latency_ms is None:
        return PolicyCheckResult(
            policy_name="latency_p95_increase",
            metric_name="p95_latency_increase_percent",
            observed_value=None,
            threshold=policy.p95_max_increase_percent,
            result=INCONCLUSIVE,
        )

    increase_percent = (canary.p95_latency_ms - stable.p95_latency_ms) / stable.p95_latency_ms * 100
    result = FAIL if increase_percent > policy.p95_max_increase_percent else PASS
    return PolicyCheckResult(
        policy_name="latency_p95_increase",
        metric_name="p95_latency_increase_percent",
        observed_value=increase_percent,
        threshold=policy.p95_max_increase_percent,
        result=result,
    )


def _evaluate_reliability(canary: MetricsSummary, policy: ReliabilityPolicy) -> PolicyCheckResult:
    if canary.error_rate is None:
        return PolicyCheckResult(
            policy_name="max_error_rate",
            metric_name="error_rate_percent",
            observed_value=None,
            threshold=policy.max_error_rate_percent,
            result=INCONCLUSIVE,
        )

    error_rate_percent = canary.error_rate * 100
    result = FAIL if error_rate_percent > policy.max_error_rate_percent else PASS
    return PolicyCheckResult(
        policy_name="max_error_rate",
        metric_name="error_rate_percent",
        observed_value=error_rate_percent,
        threshold=policy.max_error_rate_percent,
        result=result,
    )


def _evaluate_minimum_labeled_samples(
    canary_quality: MetricsSummary, minimum_labeled_samples: int
) -> PolicyCheckResult:
    result = (
        PASS if canary_quality.labeled_sample_count >= minimum_labeled_samples else INCONCLUSIVE
    )
    return PolicyCheckResult(
        policy_name="minimum_labeled_samples",
        metric_name="labeled_sample_count",
        observed_value=float(canary_quality.labeled_sample_count),
        threshold=float(minimum_labeled_samples),
        result=result,
    )


def _evaluate_minimum_label_coverage(
    canary_quality: MetricsSummary, minimum_label_coverage: float
) -> PolicyCheckResult:
    coverage = canary_quality.label_coverage
    result = (
        INCONCLUSIVE if coverage is None or coverage < minimum_label_coverage else PASS
    )
    return PolicyCheckResult(
        policy_name="minimum_label_coverage",
        metric_name="label_coverage",
        observed_value=coverage,
        threshold=minimum_label_coverage,
        result=result,
    )


def _evaluate_minimum_positive_labels(
    canary_quality: MetricsSummary, minimum_positive_labels: int
) -> PolicyCheckResult:
    result = (
        PASS
        if canary_quality.positive_label_count >= minimum_positive_labels
        else INCONCLUSIVE
    )
    return PolicyCheckResult(
        policy_name="minimum_positive_labels",
        metric_name="positive_label_count",
        observed_value=float(canary_quality.positive_label_count),
        threshold=float(minimum_positive_labels),
        result=result,
    )


def _evaluate_quality(canary_quality: MetricsSummary, policy: QualityPolicy) -> PolicyCheckResult:
    if canary_quality.recall is None:
        # No labeled canary predictions in the quality window - genuinely nothing
        # to check, not a failure. In practice this shouldn't be reached once the
        # minimum_labeled_samples/minimum_label_coverage gate below has already
        # passed, but stays defensive (e.g. all labeled predictions happened to be
        # for the *other* version, an edge case that gate doesn't itself rule out).
        return PolicyCheckResult(
            policy_name="minimum_recall",
            metric_name="recall",
            observed_value=None,
            threshold=policy.minimum_recall,
            result=INCONCLUSIVE,
        )

    result = FAIL if canary_quality.recall < policy.minimum_recall else PASS
    return PolicyCheckResult(
        policy_name="minimum_recall",
        metric_name="recall",
        observed_value=canary_quality.recall,
        threshold=policy.minimum_recall,
        result=result,
    )


def evaluate_quality_policies(
    canary_quality: MetricsSummary, config: PolicyConfig
) -> list[PolicyCheckResult]:
    """The quality-window half of one evaluation: does the canary have enough
    *labeled* data - and, within that, enough *positive-class* data - in its
    (older, matured) window to say anything about recall at all, and if so, what
    does recall actually say? Mirrors _evaluate_minimum_requests's
    gate-then-proceed shape on the reliability side: if any data-sufficiency
    check fails, minimum_recall does NOT run - "not enough data yet" and "recall
    is bad" are different findings, and the former must never silently become the
    latter (see docs/DESIGN_NOTES.md).

    Three data-sufficiency checks run in order - minimum_labeled_samples,
    minimum_label_coverage, minimum_positive_labels - because each catches a
    distinct failure mode: a low-positive-rate dataset (e.g. ~2% fraud) can
    clear the first two while the window still holds only 1-3 positive examples,
    which makes recall (TP/(TP+FN), denominator = positives) statistically
    meaningless even though "enough labeled samples" and "enough coverage" both
    technically passed.
    """
    labeled_samples_check = _evaluate_minimum_labeled_samples(
        canary_quality, config.minimum_labeled_samples
    )
    coverage_check = _evaluate_minimum_label_coverage(canary_quality, config.minimum_label_coverage)
    positive_labels_check = _evaluate_minimum_positive_labels(
        canary_quality, config.minimum_positive_labels
    )
    gate_checks = [labeled_samples_check, coverage_check, positive_labels_check]
    if any(check.result != PASS for check in gate_checks):
        return gate_checks

    quality_check = _evaluate_quality(canary_quality, config.quality)
    return [*gate_checks, quality_check]


def evaluate_policies(
    stable: MetricsSummary,
    canary: MetricsSummary,
    canary_quality: MetricsSummary,
    config: PolicyConfig,
) -> list[PolicyCheckResult]:
    """Run every policy check for one evaluation.

    `stable`/`canary` are the *reliability* window (now-window, now) -
    minimum_requests, latency, error rate. `canary_quality` is the *quality*
    window (now-window-maturity, now-maturity) - see evaluate_quality_policies and
    docs/DESIGN_NOTES.md for why quality reads an older slice of history than
    everything else.

    If minimum_requests isn't met, that is the ONLY check returned - nothing else
    (reliability or quality) gets evaluated on too little fresh traffic, per design.
    """
    minimum_requests_check = _evaluate_minimum_requests(stable, canary, config.minimum_requests)
    if minimum_requests_check.result != PASS:
        return [minimum_requests_check]

    return [
        minimum_requests_check,
        _evaluate_latency(stable, canary, config.latency),
        _evaluate_reliability(canary, config.reliability),
        *evaluate_quality_policies(canary_quality, config),
    ]


def overall_result(checks: list[PolicyCheckResult]) -> PolicyEvaluationResult:
    """FAIL beats INCONCLUSIVE beats PASS - a single failing policy fails the whole
    evaluation regardless of what else is inconclusive; an inconclusive check can
    never be quietly outvoted into a PASS."""
    outcomes = {check.result for check in checks}
    if FAIL in outcomes:
        return FAIL
    if INCONCLUSIVE in outcomes:
        return INCONCLUSIVE
    return PASS
