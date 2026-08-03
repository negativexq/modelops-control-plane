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


def _evaluate_quality(canary: MetricsSummary, policy: QualityPolicy) -> PolicyCheckResult:
    if canary.recall is None:
        # No actual_label backfilled yet - there is genuinely nothing to check, not a
        # failure. See Sprint 5 notes: this is the expected, common case today.
        return PolicyCheckResult(
            policy_name="minimum_recall",
            metric_name="recall",
            observed_value=None,
            threshold=policy.minimum_recall,
            result=INCONCLUSIVE,
        )

    result = FAIL if canary.recall < policy.minimum_recall else PASS
    return PolicyCheckResult(
        policy_name="minimum_recall",
        metric_name="recall",
        observed_value=canary.recall,
        threshold=policy.minimum_recall,
        result=result,
    )


def evaluate_policies(
    stable: MetricsSummary, canary: MetricsSummary, config: PolicyConfig
) -> list[PolicyCheckResult]:
    """Run every policy check for one evaluation. If minimum_requests isn't met, that
    is the ONLY check returned - metric-based policies (latency/reliability/quality)
    don't get evaluated on too little traffic, per design.
    """
    minimum_requests_check = _evaluate_minimum_requests(stable, canary, config.minimum_requests)
    if minimum_requests_check.result != PASS:
        return [minimum_requests_check]

    return [
        minimum_requests_check,
        _evaluate_latency(stable, canary, config.latency),
        _evaluate_reliability(canary, config.reliability),
        _evaluate_quality(canary, config.quality),
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
