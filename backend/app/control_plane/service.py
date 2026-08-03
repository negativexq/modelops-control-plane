import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane.models import (
    Deployment,
    DeploymentEvent,
    DeploymentStatus,
    TrafficAllocation,
)
from app.control_plane.router_gateway import RouterGateway, RouterUpdateError
from app.control_plane.state_machine import validate_transition
from app.policy.config import PolicyConfig, policy_settings

logger = logging.getLogger("control_plane")

# A deployment is "active" (its traffic allocation is the one that matters right now)
# while it's still rolling out or being judged. Terminal states (PROMOTED,
# ROLLED_BACK, FAILED) are excluded even though their traffic_allocation row still
# exists - it's history at that point, not the live split.
ACTIVE_STATUSES = (DeploymentStatus.CANARY_RUNNING, DeploymentStatus.EVALUATING)

# The canary traffic ramp an automated rollout climbs through: 10% -> 25% -> 50% ->
# 100%. Deliberately not a column on Deployment - "what's the next stage" is always
# derived from the deployment's current TrafficAllocation (the smallest stage weight
# strictly greater than the canary's current weight), so it's correct after a worker
# restart and unaffected by whatever custom canary_weight a deployment started at.
TRAFFIC_STAGES: tuple[float, ...] = (0.10, 0.25, 0.50, 1.0)


class DeploymentNotFoundError(Exception):
    pass


class DeploymentNotActiveError(Exception):
    """Raised when an automated action targets a deployment that is no longer
    CANARY_RUNNING/EVALUATING - most often because a human (or another cycle) already
    promoted/rolled it back concurrently. Not a bug - the caller should just drop it.
    """

    def __init__(self, deployment_id: str, status: DeploymentStatus) -> None:
        self.deployment_id = deployment_id
        self.status = status
        super().__init__(f"deployment {deployment_id} is not active (status={status.value})")


class AlreadyAtFinalStageError(Exception):
    """Raised by advance_traffic when the canary is already at 100% - the caller
    should promote instead of advancing further."""

    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id
        super().__init__(f"deployment {deployment_id} canary is already at the final stage")


def get_active_deployment(db: Session, model_name: str) -> Deployment | None:
    """The one deployment (if any) whose traffic allocation is currently live for
    this model. Used by both the router-config sync endpoint and anything else that
    needs "what's actually running now" rather than "the most recent record".
    """
    stmt = (
        select(Deployment)
        .where(Deployment.model_name == model_name, Deployment.status.in_(ACTIVE_STATUSES))
        .order_by(Deployment.created_at.desc())
    )
    return db.execute(stmt).scalars().first()


def _log_event(db: Session, deployment: Deployment, event_type: str, message: str) -> None:
    db.add(DeploymentEvent(deployment_id=deployment.id, event_type=event_type, message=message))
    logger.info("deployment %s: %s - %s", deployment.id, event_type, message)


def _transition(
    db: Session, deployment: Deployment, target: DeploymentStatus, message: str
) -> None:
    """Validate and apply a state transition, logging it as a DeploymentEvent.

    Raises InvalidTransitionError (uncaught here) if the transition isn't allowed -
    callers decide how to surface that (e.g. HTTP 409 at the API layer).
    """
    validate_transition(deployment.status, target)
    previous = deployment.status
    deployment.status = target
    _log_event(db, deployment, "status_changed", f"{previous.value} -> {target.value}: {message}")


def _set_traffic_allocation(
    db: Session, deployment: Deployment, targets: list[dict[str, float | str]]
) -> None:
    if deployment.traffic_allocation is not None:
        deployment.traffic_allocation.targets = targets
    else:
        db.add(TrafficAllocation(deployment_id=deployment.id, targets=targets))


def _require_active(deployment: Deployment) -> None:
    if deployment.status not in ACTIVE_STATUSES:
        raise DeploymentNotActiveError(deployment.id, deployment.status)


def get_deployment(db: Session, deployment_id: str) -> Deployment:
    deployment = db.get(Deployment, deployment_id)
    if deployment is None:
        raise DeploymentNotFoundError(deployment_id)
    return deployment


def get_policy_config(deployment: Deployment) -> PolicyConfig:
    """The deployment's own resolved PolicyConfig, or environment defaults if (for
    some pre-Sprint-8 deployment) none was ever stored."""
    if deployment.policy_config:
        return PolicyConfig(**deployment.policy_config)
    return policy_settings.to_policy_config()


def find_by_idempotency_key(db: Session, key: str) -> Deployment | None:
    stmt = select(Deployment).where(Deployment.idempotency_key == key)
    return db.execute(stmt).scalar_one_or_none()


def list_deployments(db: Session) -> list[Deployment]:
    stmt = select(Deployment).order_by(Deployment.created_at.desc())
    return list(db.execute(stmt).scalars().all())


async def create_deployment(
    db: Session,
    router_gateway: RouterGateway,
    model_name: str,
    stable_version: str,
    canary_version: str,
    canary_weight: float,
    idempotency_key: str | None,
    policy_config: PolicyConfig | None = None,
) -> tuple[Deployment, bool]:
    """Start a new canary deployment.

    Returns (deployment, created). If idempotency_key matches a deployment created by
    an earlier call, that existing deployment is returned unchanged with created=False -
    a retried request never creates a second row.
    """
    if idempotency_key:
        existing = find_by_idempotency_key(db, idempotency_key)
        if existing is not None:
            return existing, False

    effective_policy_config = (
        policy_config if policy_config is not None else policy_settings.to_policy_config()
    )

    deployment = Deployment(
        model_name=model_name,
        stable_version=stable_version,
        canary_version=canary_version,
        status=DeploymentStatus.PENDING,
        idempotency_key=idempotency_key,
        policy_config=effective_policy_config.model_dump(),
    )
    db.add(deployment)
    db.flush()
    _log_event(
        db,
        deployment,
        "created",
        f"deployment created for {model_name}: stable={stable_version} canary={canary_version}",
    )

    targets: list[dict[str, float | str]] = [
        {"version": stable_version, "weight": round(1 - canary_weight, 6)},
        {"version": canary_version, "weight": round(canary_weight, 6)},
    ]

    _transition(db, deployment, DeploymentStatus.DEPLOYING, "starting canary rollout")
    deployment.started_at = datetime.now(UTC)

    try:
        await router_gateway.push_traffic_allocation(model_name, deployment.id, targets)
    except RouterUpdateError as exc:
        _transition(db, deployment, DeploymentStatus.FAILED, f"router update failed: {exc}")
        deployment.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(deployment)
        return deployment, True

    _set_traffic_allocation(db, deployment, targets)
    _transition(db, deployment, DeploymentStatus.CANARY_RUNNING, "canary receiving traffic")

    db.commit()
    db.refresh(deployment)
    return deployment, True


async def promote_deployment(
    db: Session, router_gateway: RouterGateway, deployment: Deployment, triggered_by: str = "manual"
) -> Deployment:
    """Promote the canary to 100% of traffic. `triggered_by` ("manual" or
    "automatic") is recorded in the event message so the audit trail can tell a human
    click apart from a worker decision."""
    if deployment.status == DeploymentStatus.CANARY_RUNNING:
        _transition(
            db,
            deployment,
            DeploymentStatus.EVALUATING,
            f"auto-advancing before {triggered_by} promote",
        )

    _transition(db, deployment, DeploymentStatus.PROMOTING, f"{triggered_by} promote requested")

    targets: list[dict[str, float | str]] = [{"version": deployment.canary_version, "weight": 1.0}]
    try:
        await router_gateway.push_traffic_allocation(deployment.model_name, deployment.id, targets)
    except RouterUpdateError as exc:
        _transition(
            db, deployment, DeploymentStatus.FAILED, f"router update failed during promote: {exc}"
        )
        deployment.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(deployment)
        return deployment

    _set_traffic_allocation(db, deployment, targets)
    _transition(
        db,
        deployment,
        DeploymentStatus.PROMOTED,
        f"canary promoted to 100% traffic ({triggered_by})",
    )
    deployment.completed_at = datetime.now(UTC)

    db.commit()
    db.refresh(deployment)
    return deployment


async def rollback_deployment(
    db: Session, router_gateway: RouterGateway, deployment: Deployment, triggered_by: str = "manual"
) -> Deployment:
    """Roll back all traffic to the stable version. `triggered_by` distinguishes a
    manual dashboard click from a worker's automated FAIL decision in the event log.
    """
    if deployment.status == DeploymentStatus.CANARY_RUNNING:
        _transition(
            db,
            deployment,
            DeploymentStatus.EVALUATING,
            f"auto-advancing before {triggered_by} rollback",
        )

    _transition(db, deployment, DeploymentStatus.ROLLING_BACK, f"{triggered_by} rollback requested")

    targets: list[dict[str, float | str]] = [{"version": deployment.stable_version, "weight": 1.0}]
    try:
        await router_gateway.push_traffic_allocation(deployment.model_name, deployment.id, targets)
    except RouterUpdateError as exc:
        _transition(
            db,
            deployment,
            DeploymentStatus.FAILED,
            f"router update failed during rollback: {exc}",
        )
        deployment.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(deployment)
        return deployment

    _set_traffic_allocation(db, deployment, targets)
    _transition(
        db,
        deployment,
        DeploymentStatus.ROLLED_BACK,
        f"traffic rolled back to stable ({triggered_by})",
    )
    deployment.completed_at = datetime.now(UTC)

    db.commit()
    db.refresh(deployment)
    return deployment


def _current_canary_weight(deployment: Deployment) -> float:
    if deployment.traffic_allocation is None:
        return 0.0
    for target in deployment.traffic_allocation.targets:
        if target["version"] == deployment.canary_version:
            return float(target["weight"])
    return 0.0


async def advance_traffic(
    db: Session, router_gateway: RouterGateway, deployment: Deployment
) -> Deployment:
    """Move the canary to the next step of TRAFFIC_STAGES. Used exclusively by the
    automated worker (app/worker/) when a policy evaluation comes back PASS and the
    canary isn't at 100% yet - stays in whatever status the deployment is already in
    (CANARY_RUNNING or EVALUATING); ramping up traffic isn't itself a state-machine
    transition.
    """
    _require_active(deployment)

    current_weight = _current_canary_weight(deployment)
    next_weight = next((w for w in TRAFFIC_STAGES if w > current_weight + 1e-9), None)
    if next_weight is None:
        raise AlreadyAtFinalStageError(deployment.id)

    targets: list[dict[str, float | str]] = [
        {"version": deployment.stable_version, "weight": round(1 - next_weight, 6)},
        {"version": deployment.canary_version, "weight": round(next_weight, 6)},
    ]
    try:
        await router_gateway.push_traffic_allocation(deployment.model_name, deployment.id, targets)
    except RouterUpdateError as exc:
        _transition(
            db,
            deployment,
            DeploymentStatus.FAILED,
            f"router update failed during automatic traffic advance: {exc}",
        )
        deployment.completed_at = datetime.now(UTC)
        db.commit()
        db.refresh(deployment)
        return deployment

    _set_traffic_allocation(db, deployment, targets)
    from_pct = current_weight * 100
    to_pct = next_weight * 100
    _log_event(
        db,
        deployment,
        "traffic_advanced",
        f"auto: advanced canary traffic from {from_pct:.0f}% to {to_pct:.0f}%",
    )

    db.commit()
    db.refresh(deployment)
    return deployment


def record_inconclusive(db: Session, deployment: Deployment, max_retries: int) -> Deployment:
    """Called by the worker when an automated evaluation comes back INCONCLUSIVE.
    Increments the retry counter; once it exceeds `max_retries`, freezes the
    deployment into INCONCLUSIVE status so the worker stops retrying and a human can
    look at it - an unlabeled canary otherwise stays INCONCLUSIVE forever, which is
    the expected (not buggy) outcome while there's no actual_label source (Sprint 5).
    """
    _require_active(deployment)

    deployment.inconclusive_retry_count += 1
    attempt = deployment.inconclusive_retry_count
    _log_event(
        db,
        deployment,
        "inconclusive_cycle",
        f"auto: evaluation inconclusive (attempt {attempt}/{max_retries})",
    )

    if deployment.inconclusive_retry_count > max_retries:
        if deployment.status == DeploymentStatus.CANARY_RUNNING:
            _transition(
                db,
                deployment,
                DeploymentStatus.EVALUATING,
                "auto-advancing before freezing on max inconclusive retries",
            )
        _transition(
            db,
            deployment,
            DeploymentStatus.INCONCLUSIVE,
            f"auto: exceeded max inconclusive retries ({max_retries}), freezing for manual review",
        )
        deployment.completed_at = datetime.now(UTC)

    db.commit()
    db.refresh(deployment)
    return deployment
