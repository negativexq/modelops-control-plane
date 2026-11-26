from dataclasses import dataclass, field
from typing import Any

from app.control_plane.models import PolicyEvaluationResult
from app.control_plane.schemas import MetricsSummary
from app.policy import drift, stats
from app.policy.config import (
    DriftPolicy,
    LatencyPolicy,
    PolicyConfig,
    QualityPolicy,
    ReliabilityPolicy,
)

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
    # Statistical evidence, in the same unit as observed_value (see
    # app/policy/stats.py). None for the data-sufficiency gates, which are counts,
    # and when there's too little data to compute an interval at all.
    ci_low: float | None = None
    ci_high: float | None = None
    p_value: float | None = None
    method: str | None = None
    # The level this check's interval and test were held to: the policy's
    # confidence_level, raised for later looks when sequential testing is on
    # (see stats.look_confidence). None for checks without statistics.
    confidence_level: float | None = None
    # Which evaluation of this rollout produced the check (1 = first), when
    # sequential testing set the level above.
    look: int | None = None
    # What the check was about, when the policy name doesn't say: for
    # feature_drift, the feature that drifted most, or why nothing was compared.
    detail: str | None = None


@dataclass(frozen=True)
class Evidence:
    """The level one evaluation's statistical checks must reach."""

    confidence_level: float
    look: int | None = None

    @property
    def one_sided_alpha(self) -> float:
        """A two-sided interval at confidence_level leaves this much in each
        tail, so it's the matching error rate for a one-sided test."""
        return (1 - self.confidence_level) / 2


@dataclass(frozen=True)
class LatencySamples:
    """Raw per-request latencies (ms) in the reliability window. Without them
    the latency check can only compare point p95s."""

    stable: list[float]
    canary: list[float]


def _significant(p_value: float | None, evidence: Evidence) -> bool:
    return p_value is not None and p_value <= evidence.one_sided_alpha


def _decide_upper_limit(
    observed: float,
    limit: float,
    interval: stats.Interval | None,
    p_value: float | None,
    evidence: Evidence,
) -> PolicyEvaluationResult:
    """For a metric that must stay at or below `limit` (error rate, latency
    increase). FAIL needs the whole confidence interval above the limit and the
    one-sided test of "the limit is breached" to be significant at the same
    level: the breach must be real, not noise. The interval says how big the
    breach is; the test is what keeps the false-FAIL rate at the level promised
    (a Wilson or bootstrap interval alone can be a little optimistic in the far
    tail that late looks use). Above the limit but not confidently so is
    INCONCLUSIVE - wait for more traffic instead of rolling back a release that
    may be fine. At or below the limit passes."""
    if observed <= limit:
        return PASS
    if interval is not None and interval.low > limit and _significant(p_value, evidence):
        return FAIL
    return INCONCLUSIVE


def _decide_lower_limit(
    observed: float,
    limit: float,
    interval: stats.Interval | None,
    p_value: float | None,
    evidence: Evidence,
) -> PolicyEvaluationResult:
    """Mirror of _decide_upper_limit for a metric that must stay at or above
    `limit` (recall)."""
    if observed >= limit:
        return PASS
    if interval is not None and interval.high < limit and _significant(p_value, evidence):
        return FAIL
    return INCONCLUSIVE


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
    stable: MetricsSummary,
    canary: MetricsSummary,
    policy: LatencyPolicy,
    evidence: Evidence,
    samples: LatencySamples | None,
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
    limit = policy.p95_max_increase_percent
    if samples is None:
        # No raw latencies (callers that only have summaries): point estimate only.
        return PolicyCheckResult(
            policy_name="latency_p95_increase",
            metric_name="p95_latency_increase_percent",
            observed_value=increase_percent,
            threshold=limit,
            result=FAIL if increase_percent > limit else PASS,
        )

    interval = stats.p95_increase_interval(
        samples.stable, samples.canary, evidence.confidence_level
    )
    p_value = stats.latency_shift_p_value(samples.stable, samples.canary, limit)
    return PolicyCheckResult(
        policy_name="latency_p95_increase",
        metric_name="p95_latency_increase_percent",
        observed_value=increase_percent,
        threshold=limit,
        result=_decide_upper_limit(increase_percent, limit, interval, p_value, evidence),
        ci_low=interval.low if interval else None,
        ci_high=interval.high if interval else None,
        p_value=p_value,
        method="bootstrap_p95_ratio+mann_whitney",
        confidence_level=evidence.confidence_level,
        look=evidence.look,
    )


def _evaluate_reliability(
    canary: MetricsSummary, policy: ReliabilityPolicy, evidence: Evidence
) -> PolicyCheckResult:
    if canary.error_rate is None:
        return PolicyCheckResult(
            policy_name="max_error_rate",
            metric_name="error_rate_percent",
            observed_value=None,
            threshold=policy.max_error_rate_percent,
            result=INCONCLUSIVE,
        )

    error_rate_percent = canary.error_rate * 100
    limit = policy.max_error_rate_percent
    errors = round(canary.error_rate * canary.sample_count)
    interval = stats.wilson_interval(errors, canary.sample_count, evidence.confidence_level)
    interval_percent = (
        stats.Interval(interval.low * 100, interval.high * 100) if interval else None
    )
    p_value = stats.binomial_p_above(errors, canary.sample_count, limit / 100)
    return PolicyCheckResult(
        policy_name="max_error_rate",
        metric_name="error_rate_percent",
        observed_value=error_rate_percent,
        threshold=limit,
        result=_decide_upper_limit(
            error_rate_percent, limit, interval_percent, p_value, evidence
        ),
        ci_low=interval_percent.low if interval_percent else None,
        ci_high=interval_percent.high if interval_percent else None,
        p_value=p_value,
        method="wilson+binomial",
        confidence_level=evidence.confidence_level,
        look=evidence.look,
    )


@dataclass(frozen=True)
class DriftInputs:
    """The canary's live inputs from the reliability window, and the training
    profile of the artifact that scored them. `profile` is None when that
    artifact has no profile (trained before profiles existed)."""

    artifact: str
    profile: dict[str, Any] | None
    inputs: list[dict[str, Any]]
    # The fraud_probability the canary gave each of those requests.
    scores: list[float] = field(default_factory=list)


# Values of PolicyCheckResult.detail when feature_drift had nothing to compare.
NO_REFERENCE_PROFILE = "no_reference_profile"
TOO_FEW_INPUTS = "too_few_inputs"


def _evaluate_feature_drift(
    data: DriftInputs, policy: DriftPolicy, evidence: Evidence
) -> PolicyCheckResult:
    """Is the canary being asked to score traffic unlike its training data?
    See app/policy/drift.py. PASS when no feature's corrected PSI is past the
    limit. FAIL when one is past it *and* its shift is significant at this
    evaluation's level (chi-square, Bonferroni across features) - the same
    "big enough and not noise" rule as the interval checks. Otherwise
    INCONCLUSIVE."""
    limit = policy.max_feature_psi
    assert limit is not None

    def inconclusive(detail: str) -> PolicyCheckResult:
        return PolicyCheckResult(
            policy_name="feature_drift",
            metric_name="max_feature_psi",
            observed_value=None,
            threshold=limit,
            result=INCONCLUSIVE,
            detail=detail,
        )

    if data.profile is None:
        return inconclusive(f"{NO_REFERENCE_PROFILE}:{data.artifact}")
    if len(data.inputs) < drift.MINIMUM_SAMPLES:
        return inconclusive(TOO_FEW_INPUTS)
    report = drift.drift_report(data.profile, data.inputs)
    largest = report.largest
    if largest is None:
        return inconclusive(TOO_FEW_INPUTS)

    breaching = [f for f in report.features if f.psi_corrected > limit]
    significant = [f for f in breaching if report.adjusted_p_value(f) <= evidence.one_sided_alpha]
    if significant:
        result, shown = FAIL, max(significant, key=lambda f: f.psi_corrected)
    elif breaching:
        result, shown = INCONCLUSIVE, largest
    else:
        result, shown = PASS, largest
    return PolicyCheckResult(
        policy_name="feature_drift",
        metric_name="max_feature_psi",
        observed_value=shown.psi_corrected,
        threshold=limit,
        result=result,
        p_value=report.adjusted_p_value(shown),
        method="psi+chi_square_bonferroni",
        confidence_level=evidence.confidence_level,
        look=evidence.look,
        detail=shown.feature,
    )


def _evaluate_prediction_drift(
    data: DriftInputs, policy: DriftPolicy, evidence: Evidence
) -> PolicyCheckResult:
    """Is the canary scoring live traffic the way it scored its validation
    data? Same statistics as feature_drift (app/policy/drift.py) on a single
    distribution, the model's fraud_probability, so no Bonferroni. Catches a
    shift spread thinly across many features - each within feature_drift's
    limit - that adds up to a real change in what the model outputs."""
    limit = policy.max_prediction_psi
    assert limit is not None

    def inconclusive(detail: str) -> PolicyCheckResult:
        return PolicyCheckResult(
            policy_name="prediction_drift",
            metric_name="prediction_psi",
            observed_value=None,
            threshold=limit,
            result=INCONCLUSIVE,
            detail=detail,
        )

    spec = data.profile.get("prediction") if data.profile is not None else None
    if spec is None:
        return inconclusive(f"{NO_REFERENCE_PROFILE}:{data.artifact}")
    if len(data.scores) < drift.MINIMUM_SAMPLES:
        return inconclusive(TOO_FEW_INPUTS)
    shift = drift.feature_drift(drift.PREDICTION_SCORE, spec, list(data.scores))
    if shift is None:
        return inconclusive(TOO_FEW_INPUTS)
    if shift.psi_corrected <= limit:
        result = PASS
    elif shift.p_value <= evidence.one_sided_alpha:
        result = FAIL
    else:
        result = INCONCLUSIVE
    return PolicyCheckResult(
        policy_name="prediction_drift",
        metric_name="prediction_psi",
        observed_value=shift.psi_corrected,
        threshold=limit,
        result=result,
        p_value=shift.p_value,
        method="psi+chi_square",
        confidence_level=evidence.confidence_level,
        look=evidence.look,
        detail=drift.PREDICTION_SCORE,
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


def _evaluate_quality(
    canary_quality: MetricsSummary, policy: QualityPolicy, evidence: Evidence
) -> PolicyCheckResult:
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

    positives = canary_quality.positive_label_count
    true_positives = round(canary_quality.recall * positives)
    interval = stats.wilson_interval(true_positives, positives, evidence.confidence_level)
    p_value = stats.binomial_p_below(true_positives, positives, policy.minimum_recall)
    return PolicyCheckResult(
        policy_name="minimum_recall",
        metric_name="recall",
        observed_value=canary_quality.recall,
        threshold=policy.minimum_recall,
        result=_decide_lower_limit(
            canary_quality.recall, policy.minimum_recall, interval, p_value, evidence
        ),
        ci_low=interval.low if interval else None,
        ci_high=interval.high if interval else None,
        p_value=p_value,
        method="wilson+binomial",
        confidence_level=evidence.confidence_level,
        look=evidence.look,
    )


def evidence_for(config: PolicyConfig, look: int | None) -> Evidence:
    """The level this evaluation's checks are held to. `look` is which
    evaluation of the rollout this is (1 = first); None, or sequential testing
    switched off, means the policy's confidence_level as configured."""
    if look is None or not config.sequential_testing:
        return Evidence(config.confidence_level)
    return Evidence(stats.look_confidence(config.confidence_level, look), look)


def evaluate_quality_policies(
    canary_quality: MetricsSummary, config: PolicyConfig, look: int | None = None
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

    quality_check = _evaluate_quality(canary_quality, config.quality, evidence_for(config, look))
    return [*gate_checks, quality_check]


def evaluate_policies(
    stable: MetricsSummary,
    canary: MetricsSummary,
    canary_quality: MetricsSummary,
    config: PolicyConfig,
    latency_samples: LatencySamples | None = None,
    look: int | None = None,
    drift_inputs: DriftInputs | None = None,
) -> list[PolicyCheckResult]:
    """Run every policy check for one evaluation.

    `look` is which evaluation of this rollout this is, counting only the ones
    that got past minimum_requests (1 = first). With sequential testing on, it
    sets how much evidence a FAIL needs - see stats.look_confidence.

    `drift_inputs` are the canary's recorded inputs. feature_drift runs when
    there are any and the policy has a limit; a window whose metrics carry no
    inputs at all (a caller posting metrics without them) skips it rather than
    holding the rollout on data nobody sends.

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

    evidence = evidence_for(config, look)
    checks = [
        minimum_requests_check,
        _evaluate_latency(stable, canary, config.latency, evidence, latency_samples),
        _evaluate_reliability(canary, config.reliability, evidence),
    ]
    if drift_inputs is not None and config.drift.max_feature_psi is not None:
        checks.append(_evaluate_feature_drift(drift_inputs, config.drift, evidence))
    if drift_inputs is not None and config.drift.max_prediction_psi is not None:
        checks.append(_evaluate_prediction_drift(drift_inputs, config.drift, evidence))
    return [*checks, *evaluate_quality_policies(canary_quality, config, look)]


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
