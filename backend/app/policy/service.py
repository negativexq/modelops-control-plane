from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane import metrics_service
from app.control_plane.models import Deployment, PolicyEvaluation, PolicyEvaluationResult
from app.policy.config import PolicyConfig
from app.policy.engine import PolicyCheckResult, evaluate_policies, overall_result

_QUALITY_POLICY_NAMES = frozenset(
    {
        "minimum_labeled_samples",
        "minimum_label_coverage",
        "minimum_positive_labels",
        "minimum_recall",
    }
)


def _weight_for(deployment: Deployment, version: str) -> float | None:
    if deployment.traffic_allocation is None:
        return None
    for target in deployment.traffic_allocation.targets:
        if target["version"] == version:
            return float(target["weight"])
    return None


def run_evaluation(
    db: Session, deployment: Deployment, config: PolicyConfig
) -> tuple[list[PolicyCheckResult], PolicyEvaluationResult]:
    """Evaluate every policy for `deployment` and persist each check as its own
    PolicyEvaluation row. Does NOT touch deployment.status - promotion/rollback stay
    manual (see Sprint 4); wiring this result into the state machine is Sprint 8.
    """
    stable = metrics_service.compute_version_summary(
        db, deployment.id, deployment.stable_version, config.evaluation_window_seconds
    )
    canary = metrics_service.compute_version_summary(
        db, deployment.id, deployment.canary_version, config.evaluation_window_seconds
    )
    # Quality checks read an older, matured window - see
    # app/policy/engine.py::evaluate_quality_policies and docs/DESIGN_NOTES.md.
    canary_quality = metrics_service.compute_version_summary(
        db,
        deployment.id,
        deployment.canary_version,
        config.evaluation_window_seconds,
        window_end_offset_seconds=config.label_maturity_seconds,
    )

    checks = evaluate_policies(stable, canary, canary_quality, config)
    # Snapshot the deployment's own context right now, once, for every check in this
    # evaluation - not derivable later from the deployment's *current* state, which
    # will keep changing after this call returns (traffic ramps, gets promoted,
    # rolled back, ...). See PolicyEvaluation's docstring and app/policy/explain.py.
    stable_weight = _weight_for(deployment, deployment.stable_version)
    canary_weight = _weight_for(deployment, deployment.canary_version)

    now = datetime.now(UTC)
    quality_window_end = now - timedelta(seconds=config.label_maturity_seconds)
    quality_window_start = quality_window_end - timedelta(seconds=config.evaluation_window_seconds)

    for check in checks:
        is_quality_check = check.policy_name in _QUALITY_POLICY_NAMES
        db.add(
            PolicyEvaluation(
                deployment_id=deployment.id,
                policy_name=check.policy_name,
                metric_name=check.metric_name,
                observed_value=check.observed_value,
                threshold=check.threshold,
                result=check.result,
                evaluation_window_seconds=config.evaluation_window_seconds,
                stable_weight=stable_weight,
                canary_weight=canary_weight,
                stable_sample_count=stable.sample_count,
                canary_sample_count=canary.sample_count,
                label_maturity_seconds=config.label_maturity_seconds if is_quality_check else None,
                quality_window_start=quality_window_start if is_quality_check else None,
                quality_window_end=quality_window_end if is_quality_check else None,
                labeled_sample_count=(
                    canary_quality.labeled_sample_count if is_quality_check else None
                ),
                label_coverage=canary_quality.label_coverage if is_quality_check else None,
                positive_label_count=(
                    canary_quality.positive_label_count if is_quality_check else None
                ),
            )
        )
    db.commit()

    return checks, overall_result(checks)


def list_policy_evaluations(db: Session, deployment_id: str) -> list[PolicyEvaluation]:
    stmt = (
        select(PolicyEvaluation)
        .where(PolicyEvaluation.deployment_id == deployment_id)
        .order_by(PolicyEvaluation.evaluated_at.desc())
    )
    return list(db.execute(stmt).scalars().all())
