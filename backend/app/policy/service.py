from collections import Counter
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.control_plane import metrics_service, telemetry
from app.control_plane.config import control_plane_settings
from app.control_plane.models import Deployment, PolicyEvaluation, PolicyEvaluationResult
from app.policy.config import PolicyConfig
from app.policy.engine import (
    DriftInputs,
    LatencySamples,
    PolicyCheckResult,
    evaluate_policies,
    overall_result,
)
from app.registry.exceptions import RegistryError
from app.registry.local import LocalModelRegistry

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


@lru_cache(maxsize=64)
def _reference_profile(artifacts_dir: Path, artifact: str) -> dict[str, Any] | None:
    """A trained artifact's profile never changes once written, so it's read
    once per process."""
    model_name, _, version = artifact.partition("/")
    try:
        return LocalModelRegistry(artifacts_dir).get_reference_profile(model_name, version)
    except RegistryError:
        return None


def _drift_inputs(
    db: Session, deployment: Deployment, window_seconds: int, now: datetime
) -> DriftInputs | None:
    """The canary's recorded inputs in the reliability window, with the
    training profile of the artifact that scored them. None when no metric in
    the window carried inputs. If more than one artifact served the canary
    version in the window (a container swapped mid-rollout), the one that
    served most of it is compared, on its own requests only."""
    rows = metrics_service.window_inputs(
        db, deployment.id, deployment.canary_version, window_seconds, now=now
    )
    if not rows:
        return None
    artifact = Counter(a for a, _ in rows).most_common(1)[0][0]
    return DriftInputs(
        artifact=artifact,
        profile=_reference_profile(control_plane_settings.artifacts_dir, artifact),
        inputs=[features for a, features in rows if a == artifact],
    )


def _next_look(db: Session, deployment_id: str) -> int:
    """Which evaluation of this rollout the next one is, for sequential testing
    (app/policy/stats.py's look_confidence). Only evaluations that got past
    minimum_requests ran a test, so only they count: max_error_rate is written
    by every one of those and by no other."""
    previous = db.scalar(
        select(func.count())
        .select_from(PolicyEvaluation)
        .where(
            PolicyEvaluation.deployment_id == deployment_id,
            PolicyEvaluation.policy_name == "max_error_rate",
        )
    )
    return (previous or 0) + 1


def run_evaluation(
    db: Session, deployment: Deployment, config: PolicyConfig
) -> tuple[list[PolicyCheckResult], PolicyEvaluationResult]:
    """Evaluate every policy for `deployment` and persist each check as its own
    PolicyEvaluation row. Does NOT touch deployment.status - promotion/rollback stay
    manual (see Sprint 4); wiring this result into the state machine is Sprint 8.
    """
    # One clock for every window read below, so the summaries and the raw
    # latencies the statistics run on describe exactly the same rows.
    now = datetime.now(UTC)
    window = config.evaluation_window_seconds
    stable = metrics_service.compute_version_summary(
        db, deployment.id, deployment.stable_version, window, now=now
    )
    canary = metrics_service.compute_version_summary(
        db, deployment.id, deployment.canary_version, window, now=now
    )
    # Quality checks read an older, matured window - see
    # app/policy/engine.py::evaluate_quality_policies and docs/DESIGN_NOTES.md.
    canary_quality = metrics_service.compute_version_summary(
        db,
        deployment.id,
        deployment.canary_version,
        config.evaluation_window_seconds,
        window_end_offset_seconds=config.label_maturity_seconds,
        now=now,
    )
    samples = LatencySamples(
        stable=metrics_service.window_latencies(
            db, deployment.id, deployment.stable_version, window, now=now
        ),
        canary=metrics_service.window_latencies(
            db, deployment.id, deployment.canary_version, window, now=now
        ),
    )

    checks = evaluate_policies(
        stable,
        canary,
        canary_quality,
        config,
        samples,
        look=_next_look(db, deployment.id),
        drift_inputs=_drift_inputs(db, deployment, window, now),
    )
    # Snapshot the deployment's own context right now, once, for every check in this
    # evaluation - not derivable later from the deployment's *current* state, which
    # will keep changing after this call returns (traffic ramps, gets promoted,
    # rolled back, ...). See PolicyEvaluation's docstring and app/policy/explain.py.
    stable_weight = _weight_for(deployment, deployment.stable_version)
    canary_weight = _weight_for(deployment, deployment.canary_version)

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
                ci_low=check.ci_low,
                ci_high=check.ci_high,
                p_value=check.p_value,
                confidence_level=check.confidence_level,
                stat_method=check.method,
                sequential_look=check.look,
                detail=check.detail,
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

    overall = overall_result(checks)
    telemetry.POLICY_EVALUATIONS.labels(overall.value).inc()
    for check in checks:
        telemetry.POLICY_CHECKS.labels(check.policy_name, check.result.value).inc()
    return checks, overall


def list_policy_evaluations(db: Session, deployment_id: str) -> list[PolicyEvaluation]:
    stmt = (
        select(PolicyEvaluation)
        .where(PolicyEvaluation.deployment_id == deployment_id)
        .order_by(PolicyEvaluation.evaluated_at.desc())
    )
    return list(db.execute(stmt).scalars().all())
