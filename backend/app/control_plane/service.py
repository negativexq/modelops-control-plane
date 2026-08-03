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

logger = logging.getLogger("control_plane")

# A deployment is "active" (its traffic allocation is the one that matters right now)
# while it's still rolling out or being judged. Terminal states (PROMOTED,
# ROLLED_BACK, FAILED) are excluded even though their traffic_allocation row still
# exists - it's history at that point, not the live split.
ACTIVE_STATUSES = (DeploymentStatus.CANARY_RUNNING, DeploymentStatus.EVALUATING)


class DeploymentNotFoundError(Exception):
    pass


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


def get_deployment(db: Session, deployment_id: str) -> Deployment:
    deployment = db.get(Deployment, deployment_id)
    if deployment is None:
        raise DeploymentNotFoundError(deployment_id)
    return deployment


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

    deployment = Deployment(
        model_name=model_name,
        stable_version=stable_version,
        canary_version=canary_version,
        status=DeploymentStatus.PENDING,
        idempotency_key=idempotency_key,
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
    db: Session, router_gateway: RouterGateway, deployment: Deployment
) -> Deployment:
    """Manually promote the canary to 100% of traffic."""
    if deployment.status == DeploymentStatus.CANARY_RUNNING:
        _transition(
            db, deployment, DeploymentStatus.EVALUATING, "auto-advancing before manual promote"
        )

    _transition(db, deployment, DeploymentStatus.PROMOTING, "manual promote requested")

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
    _transition(db, deployment, DeploymentStatus.PROMOTED, "canary promoted to 100% traffic")
    deployment.completed_at = datetime.now(UTC)

    db.commit()
    db.refresh(deployment)
    return deployment


async def rollback_deployment(
    db: Session, router_gateway: RouterGateway, deployment: Deployment
) -> Deployment:
    """Manually roll back all traffic to the stable version."""
    if deployment.status == DeploymentStatus.CANARY_RUNNING:
        _transition(
            db, deployment, DeploymentStatus.EVALUATING, "auto-advancing before manual rollback"
        )

    _transition(db, deployment, DeploymentStatus.ROLLING_BACK, "manual rollback requested")

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
    _transition(db, deployment, DeploymentStatus.ROLLED_BACK, "traffic rolled back to stable")
    deployment.completed_at = datetime.now(UTC)

    db.commit()
    db.refresh(deployment)
    return deployment
