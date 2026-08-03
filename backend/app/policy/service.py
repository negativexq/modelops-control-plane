from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane import metrics_service
from app.control_plane.models import Deployment, PolicyEvaluation, PolicyEvaluationResult
from app.policy.config import PolicyConfig
from app.policy.engine import PolicyCheckResult, evaluate_policies, overall_result


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

    checks = evaluate_policies(stable, canary, config)
    for check in checks:
        db.add(
            PolicyEvaluation(
                deployment_id=deployment.id,
                policy_name=check.policy_name,
                metric_name=check.metric_name,
                observed_value=check.observed_value,
                threshold=check.threshold,
                result=check.result,
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
