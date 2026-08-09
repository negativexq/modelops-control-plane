"""Merges a deployment's DeploymentEvent (state transitions, worker actions) and
PolicyEvaluation (per-check PASS/FAIL/INCONCLUSIVE results) rows into one
chronological narrative - see GET /api/deployments/{id}/timeline.

Two plain queries, merged and sorted in Python, rather than one UNION query: the two
tables have different columns, and the policy side needs a derived `explanation`
string computed in Python regardless (see app/policy/explain.py) - a UNION would only
obscure what's actually two small, independent, single-deployment reads.
"""

from sqlalchemy.orm import Session

from app.control_plane.models import Deployment
from app.control_plane.schemas import TimelineEventItem, TimelineItem, TimelinePolicyItem
from app.policy.explain import explain_policy_check
from app.policy.service import list_policy_evaluations


def _canary_weight(deployment: Deployment) -> float | None:
    """The canary's *current* share of traffic, or None if no allocation has ever
    been pushed. Used only to make a minimum_requests explanation concrete when the
    canary is at/near 100% (see explain_policy_check) - this reads the live
    allocation, which may differ from what was in effect when an older
    PolicyEvaluation was actually recorded. Acceptable for a display-only hint.
    """
    if deployment.traffic_allocation is None:
        return None
    for target in deployment.traffic_allocation.targets:
        if target["version"] == deployment.canary_version:
            return float(target["weight"])
    return None


def build_timeline(db: Session, deployment: Deployment) -> list[TimelineItem]:
    current_canary_weight = _canary_weight(deployment)

    items: list[TimelineItem] = [
        TimelineEventItem(
            id=event.id,
            timestamp=event.created_at,
            event_type=event.event_type,
            message=event.message,
        )
        for event in deployment.events
    ]
    for evaluation in list_policy_evaluations(db, deployment.id):
        # Prefer the snapshot recorded at evaluation time (PolicyEvaluation.
        # canary_weight - see policy/service.py's run_evaluation); only fall back
        # to the deployment's *current* traffic split for rows written before that
        # snapshot column existed, and flag the fallback explicitly rather than
        # silently presenting a guess as recorded fact.
        has_snapshot = evaluation.canary_weight is not None
        weight_for_explanation = (
            evaluation.canary_weight if has_snapshot else current_canary_weight
        )
        items.append(
            TimelinePolicyItem(
                id=evaluation.id,
                timestamp=evaluation.evaluated_at,
                policy_name=evaluation.policy_name,
                metric_name=evaluation.metric_name,
                observed_value=evaluation.observed_value,
                threshold=evaluation.threshold,
                result=evaluation.result,
                explanation=explain_policy_check(
                    policy_name=evaluation.policy_name,
                    observed_value=evaluation.observed_value,
                    threshold=evaluation.threshold,
                    result=evaluation.result,
                    canary_weight=weight_for_explanation,
                    is_estimated=not has_snapshot,
                ),
                is_estimated=not has_snapshot,
            )
        )
    items.sort(key=lambda item: item.timestamp)
    return items
