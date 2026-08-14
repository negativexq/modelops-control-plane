"""Turns a PolicyEvaluation row's raw facts (policy_name, observed_value, threshold,
result) into a human-readable sentence - what GET /api/deployments/{id}/timeline
shows next to each policy check.

Deliberately pure and derived at read time rather than persisted: it works
identically for a check evaluated a second ago or one from months back, needs no
migration, and can never drift out of sync with the policy engine's own wording
since there's only one place this text is generated.
"""

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
    if policy_name == "latency_p95_increase":
        return _explain_latency(observed_value, threshold, result)
    if policy_name == "max_error_rate":
        return _explain_error_rate(observed_value, threshold, result)
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
        return _explain_recall(observed_value, threshold, result)
    return f"{policy_name}: {result.value.lower()}."


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
    observed: float | None, threshold: float | None, result: PolicyEvaluationResult
) -> str:
    if observed is None or threshold is None:
        return "insufficient data: no p95 latency could be computed yet for one or both sides."
    if result == FAIL:
        return (
            f"canary p95 latency is {observed:.1f}% higher than stable's, "
            f"above the {threshold:.0f}% threshold."
        )
    return (
        f"canary p95 latency increase ({observed:.1f}%) is within the "
        f"{threshold:.0f}% threshold."
    )


def _explain_error_rate(
    observed: float | None, threshold: float | None, result: PolicyEvaluationResult
) -> str:
    if observed is None or threshold is None:
        return "insufficient data: no canary requests observed yet to compute an error rate."
    if result == FAIL:
        return f"canary error rate ({observed:.2f}%) exceeds the {threshold:.2f}% threshold."
    return f"canary error rate ({observed:.2f}%) is within the {threshold:.2f}% threshold."


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
    observed: float | None, threshold: float | None, result: PolicyEvaluationResult
) -> str:
    if observed is None or threshold is None:
        return (
            "insufficient data: the quality data-sufficiency gate passed, but no "
            "labeled canary predictions landed in this specific window regardless - "
            "an edge case (e.g. every labeled prediction in the window happened to "
            "belong to the stable version instead), not the common case now that "
            "labels flow through POST /api/labels."
        )
    if result == FAIL:
        return f"canary recall ({observed:.2f}) is below the required {threshold:.2f} threshold."
    return f"canary recall ({observed:.2f}) meets the {threshold:.2f} threshold."
