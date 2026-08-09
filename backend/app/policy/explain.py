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
) -> str:
    """Human-readable explanation of one policy check. `canary_weight` (the
    deployment's *current* canary traffic share, 0-1) is optional context used only
    to make a minimum_requests INCONCLUSIVE concrete when the canary is already at
    100% - see _explain_minimum_requests.
    """
    if policy_name == "minimum_requests":
        return _explain_minimum_requests(observed_value, threshold, result, canary_weight)
    if policy_name == "latency_p95_increase":
        return _explain_latency(observed_value, threshold, result)
    if policy_name == "max_error_rate":
        return _explain_error_rate(observed_value, threshold, result)
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
) -> str:
    counts = f"({_fmt_count(observed)}/{_fmt_count(threshold)} requests)"
    if result == PASS:
        return f"both sides have received enough traffic to evaluate {counts}."
    if canary_weight is not None and canary_weight >= _FULLY_PROMOTED_CANARY_WEIGHT:
        return (
            f"stable side has not received enough traffic to evaluate {counts} - the "
            "canary is already at 100% of traffic, so the stable side will not "
            "accumulate more requests until traffic is rebalanced. This is an "
            "expected platform limit, not a bug."
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


def _explain_recall(
    observed: float | None, threshold: float | None, result: PolicyEvaluationResult
) -> str:
    if observed is None or threshold is None:
        return (
            "actual_label not available: recall cannot be computed until ground-truth "
            "labels are backfilled for canary predictions - this is expected until a "
            "label source is wired up (see README's Known limitations)."
        )
    if result == FAIL:
        return f"canary recall ({observed:.2f}) is below the required {threshold:.2f} threshold."
    return f"canary recall ({observed:.2f}) meets the {threshold:.2f} threshold."
