"""Turns a PolicyEvaluation row's raw facts (policy_name, observed_value, threshold,
result) into a human-readable sentence - what GET /api/deployments/{id}/timeline
shows next to each policy check.

Deliberately pure and derived at read time rather than persisted: it works
identically for a check evaluated a second ago or one from months back, needs no
migration, and can never drift out of sync with the policy engine's own wording
since there's only one place this text is generated.
"""

from dataclasses import dataclass

from app.control_plane.models import PolicyEvaluationResult

PASS = PolicyEvaluationResult.PASS
FAIL = PolicyEvaluationResult.FAIL
INCONCLUSIVE = PolicyEvaluationResult.INCONCLUSIVE

# A canary at (or effectively at) 100% of traffic leaves the stable side with ~no
# incoming requests, so minimum_requests can permanently fail to accumulate enough
# stable-side samples - a real platform limit (see README's "Known limitations"),
# not a bug. This is a display heuristic for wording the explanation, not a policy
# input: minimum_requests itself only ever sees the combined min() of both sides.
_FULLY_PROMOTED_CANARY_WEIGHT = 0.999


def explain_policy_check(
    *,
    policy_name: str,
    observed_value: float | None,
    threshold: float | None,
    result: PolicyEvaluationResult,
    canary_weight: float | None = None,
    is_estimated: bool = False,
    labeled_sample_count: int | None = None,
    ci_low: float | None = None,
    ci_high: float | None = None,
    confidence_level: float | None = None,
    sequential_look: int | None = None,
    detail: str | None = None,
) -> str:
    """Human-readable explanation of one policy check. `canary_weight` is context
    used only to make a minimum_requests INCONCLUSIVE concrete when the canary is
    already at 100% - see _explain_minimum_requests. `labeled_sample_count` is
    context used by minimum_label_coverage (to phrase coverage as "X of Y
    labeled" rather than a bare fraction) and by minimum_positive_labels (to
    phrase "X of Y labeled predictions are positive") - see
    _explain_minimum_label_coverage / _explain_minimum_positive_labels.

    Callers should pass the canary weight *as it was recorded on the
    PolicyEvaluation row itself* (PolicyEvaluation.canary_weight, snapshotted at
    evaluation time - see policy/service.py's run_evaluation) whenever that
    snapshot exists. Only for pre-snapshot rows (evaluated before that column
    existed, so it's NULL) should a caller fall back to the deployment's *current*
    traffic weight - and when it does, it must pass `is_estimated=True`, which this
    function calls out explicitly in the explanation text rather than silently
    presenting a guess as recorded fact.
    """
    if policy_name == "minimum_requests":
        return _explain_minimum_requests(
            observed_value, threshold, result, canary_weight, is_estimated
        )
    ci = _Ci(ci_low, ci_high, confidence_level, sequential_look)
    if policy_name == "latency_p95_increase":
        return _explain_latency(observed_value, threshold, result, ci)
    if policy_name == "max_error_rate":
        return _explain_error_rate(observed_value, threshold, result, ci)
    if policy_name == "feature_drift":
        return _explain_feature_drift(observed_value, threshold, result, detail)
    if policy_name == "minimum_labeled_samples":
        return _explain_minimum_labeled_samples(observed_value, threshold, result)
    if policy_name == "minimum_label_coverage":
        return _explain_minimum_label_coverage(
            observed_value, threshold, result, labeled_sample_count
        )
    if policy_name == "minimum_positive_labels":
        return _explain_minimum_positive_labels(
            observed_value, threshold, result, labeled_sample_count
        )
    if policy_name == "minimum_recall":
        return _explain_recall(observed_value, threshold, result, ci)
    return f"{policy_name}: {result.value.lower()}."


def _fmt_level(level: float) -> str:
    """95% -> "95%", 99.17% -> "99.2%", 99.988% -> "99.99%": enough digits that
    a raised sequential level never rounds up to a meaningless "100%"."""
    percent = level * 100
    gap = 100 - percent
    for digits in range(5):
        text = f"{percent:.{digits}f}"
        if float(text) < 100 and abs(float(text) - percent) <= gap * 0.1:
            return f"{text}%"
    return f"{percent:.5f}%"


@dataclass(frozen=True)
class _Ci:
    low: float | None
    high: float | None
    level: float | None
    look: int | None = None

    def text(self, fmt: str) -> str:
        """" (95% CI 3.1 to 9.8%)", or "" for rows without an interval. A level
        raised by sequential testing says which look it was for."""
        if self.low is None or self.high is None:
            return ""
        level = f"{_fmt_level(self.level)} " if self.level is not None else ""
        look = f", look {self.look}" if self.look is not None else ""
        return f" ({level}CI {fmt.format(self.low)} to {fmt.format(self.high)}{look})"


_NOISE = (
    "past the limit, but the confidence interval still reaches back inside it, so "
    "the breach could be noise - waiting for more traffic before deciding."
)


def _fmt_count(value: float | None) -> str:
    return "?" if value is None else f"{value:.0f}"


def _explain_minimum_requests(
    observed: float | None,
    threshold: float | None,
    result: PolicyEvaluationResult,
    canary_weight: float | None,
    is_estimated: bool,
) -> str:
    counts = f"({_fmt_count(observed)}/{_fmt_count(threshold)} requests)"
    if result == PASS:
        return f"both sides have received enough traffic to evaluate {counts}."
    if canary_weight is not None and canary_weight >= _FULLY_PROMOTED_CANARY_WEIGHT:
        estimate_note = (
            " (estimated from the deployment's current traffic split - no traffic "
            "snapshot was recorded for this older check, so this may not exactly "
            "match what was true at evaluation time)"
            if is_estimated
            else ""
        )
        return (
            f"stable side has not received enough traffic to evaluate {counts} - the "
            "canary is already at 100% of traffic, so the stable side will not "
            "accumulate more requests until traffic is rebalanced. This is an "
            f"expected platform limit, not a bug{estimate_note}."
        )
    return f"insufficient data: at least one side has not received enough traffic yet {counts}."


def _explain_latency(
    observed: float | None,
    threshold: float | None,
    result: PolicyEvaluationResult,
    ci: _Ci,
) -> str:
    if observed is None or threshold is None:
        return "insufficient data: no p95 latency could be computed yet for one or both sides."
    interval = ci.text("{:.1f}%")
    if result == FAIL:
        return (
            f"canary p95 latency is {observed:.1f}% higher than stable's{interval}, "
            f"above the {threshold:.0f}% threshold."
        )
    if result == INCONCLUSIVE:
        return f"canary p95 latency is {observed:.1f}% higher than stable's{interval}: {_NOISE}"
    return (
        f"canary p95 latency increase ({observed:.1f}%){interval} is within the "
        f"{threshold:.0f}% threshold."
    )


def _explain_error_rate(
    observed: float | None,
    threshold: float | None,
    result: PolicyEvaluationResult,
    ci: _Ci,
) -> str:
    if observed is None or threshold is None:
        return "insufficient data: no canary requests observed yet to compute an error rate."
    interval = ci.text("{:.2f}%")
    if result == FAIL:
        return (
            f"canary error rate ({observed:.2f}%){interval} exceeds the "
            f"{threshold:.2f}% threshold."
        )
    if result == INCONCLUSIVE:
        return f"canary error rate is {observed:.2f}%{interval}: {_NOISE}"
    return (
        f"canary error rate ({observed:.2f}%){interval} is within the "
        f"{threshold:.2f}% threshold."
    )


def _explain_feature_drift(
    observed: float | None,
    threshold: float | None,
    result: PolicyEvaluationResult,
    detail: str | None,
) -> str:
    limit = f"{threshold:.2f}" if threshold is not None else "?"
    if observed is None:
        if detail and detail.startswith("no_reference_profile:"):
            artifact = detail.partition(":")[2]
            return (
                f"no training profile for {artifact}, so its live inputs can't be "
                "compared to what it was trained on - retrain to write "
                "reference_profile.json next to the model."
            )
        return (
            "insufficient data: too few canary predictions with recorded inputs in the "
            "window to tell drift from sampling noise."
        )
    feature = f"'{detail}'" if detail else "the most-shifted feature"
    if result == FAIL:
        return (
            f"canary traffic has drifted from its training data: {feature} shows a PSI of "
            f"{observed:.2f}, above the {limit} limit, and the shift is significant - the "
            "canary is scoring inputs unlike anything it was trained on."
        )
    if result == INCONCLUSIVE:
        return (
            f"{feature} shows a PSI of {observed:.2f}, above the {limit} limit, but on this "
            "many requests the shift could still be sampling noise - waiting for more "
            "traffic before deciding."
        )
    return (
        f"canary inputs match its training data: the most-shifted feature, {feature}, has "
        f"a PSI of {observed:.2f}, within the {limit} limit."
    )


def _explain_minimum_labeled_samples(
    observed: float | None, threshold: float | None, result: PolicyEvaluationResult
) -> str:
    counts = f"({_fmt_count(observed)}/{_fmt_count(threshold)} labeled predictions)"
    if result == PASS:
        return (
            f"the quality window has enough labeled canary predictions {counts} "
            "to evaluate recall."
        )
    return (
        f"insufficient labeled data in the quality window {counts} - recall was not "
        "evaluated. Labels arrive delayed, so this is expected while the canary is "
        "still young; see minimum_label_coverage for how that compares to the "
        "window's overall traffic."
    )


def _explain_minimum_label_coverage(
    observed: float | None,
    threshold: float | None,
    result: PolicyEvaluationResult,
    labeled_sample_count: int | None,
) -> str:
    if observed is None or threshold is None:
        return (
            "insufficient data: no canary predictions in the quality window yet, so "
            "label coverage could not be computed; recall was not evaluated."
        )
    coverage_pct = f"{observed * 100:.0f}%"
    threshold_pct = f"{threshold * 100:.0f}%"
    if labeled_sample_count is not None and observed > 0:
        total = round(labeled_sample_count / observed)
        detail = f"{labeled_sample_count} of {total} predictions in the quality window are labeled"
    elif labeled_sample_count is not None:
        detail = f"{labeled_sample_count} predictions in the quality window are labeled"
    else:
        detail = f"label coverage is {coverage_pct}"
    if result == PASS:
        return f"{detail} (coverage {coverage_pct}), meeting the {threshold_pct} threshold."
    return (
        f"{detail} (coverage {coverage_pct}, threshold {threshold_pct}); "
        "recall was not evaluated."
    )


def _explain_minimum_positive_labels(
    observed: float | None,
    threshold: float | None,
    result: PolicyEvaluationResult,
    labeled_sample_count: int | None,
) -> str:
    positive_count = _fmt_count(observed)
    threshold_count = _fmt_count(threshold)
    of_total = f" of {labeled_sample_count}" if labeled_sample_count is not None else ""
    if result == PASS:
        return (
            f"{positive_count}{of_total} labeled predictions in the quality window are the "
            f"positive class (threshold {threshold_count}) - enough to make recall "
            "statistically meaningful."
        )
    return (
        f"only {positive_count}{of_total} labeled predictions in the quality window are "
        f"the positive class (threshold {threshold_count}); recall was not evaluated. A "
        "low-positive-rate dataset can clear minimum_labeled_samples/"
        "minimum_label_coverage while the window still rests on too few positive "
        "examples to trust a recall estimate."
    )


def _explain_recall(
    observed: float | None,
    threshold: float | None,
    result: PolicyEvaluationResult,
    ci: _Ci,
) -> str:
    if observed is None or threshold is None:
        return (
            "insufficient data: the quality data-sufficiency gate passed, but no "
            "labeled canary predictions landed in this specific window regardless - "
            "an edge case (e.g. every labeled prediction in the window happened to "
            "belong to the stable version instead), not the common case now that "
            "labels flow through POST /api/labels."
        )
    interval = ci.text("{:.2f}")
    if result == FAIL:
        return (
            f"canary recall ({observed:.2f}){interval} is below the required "
            f"{threshold:.2f} threshold."
        )
    if result == INCONCLUSIVE:
        return f"canary recall is {observed:.2f}{interval}, below {threshold:.2f}: {_NOISE}"
    return f"canary recall ({observed:.2f}){interval} meets the {threshold:.2f} threshold."
