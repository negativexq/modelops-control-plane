import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

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


class ConcurrentUpdateError(Exception):
    """Raised when a commit lost a race against another write to the same
    deployment row - SQLAlchemy's version_id_col (see models.Deployment) caught a
    stale write: this session read the row, someone else (a human clicking
    promote/rollback, or the worker's own poll cycle) committed a change to it
    first, and now this session's WHERE version_id=<old value> matched zero rows.

    Unlike DeploymentNotActiveError (a *sequential* stale-status check - the row
    settled into a new status before this request even started), this is a genuine
    *concurrent* write race caught at commit time - both requests could have read
    the same "active" status and passed every earlier check. Neither write silently
    overwrote the other. The caller should treat this like a 409 and, if it still
    wants to act, re-fetch and retry against the new state.
    """

    def __init__(self, deployment_id: str) -> None:
        self.deployment_id = deployment_id
        super().__init__(
            f"deployment {deployment_id} was concurrently modified by another "
            "request - retry against its current state"
        )


class ActiveDeploymentExistsError(Exception):
    """Raised by create_deployment when `model_name` already has a deployment in
    CANARY_RUNNING/EVALUATING - the router holds exactly one traffic split per
    model, so a second concurrent rollout would silently fight the first one over
    it. A caller that wants to replace an in-flight rollout must promote/roll it
    back first, not start a second one out from under it.
    """

    def __init__(self, model_name: str, existing_deployment_id: str) -> None:
        self.model_name = model_name
        self.existing_deployment_id = existing_deployment_id
        super().__init__(
            f"model '{model_name}' already has an active deployment "
            f"({existing_deployment_id}) - promote or roll it back first"
        )


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


def require_active(deployment: Deployment) -> None:
    """Raises DeploymentNotActiveError unless `deployment` is CANARY_RUNNING or
    EVALUATING. Public (used by policy/api.py's /evaluate guard too, not just the
    worker-only action endpoints in this module) - see DeploymentNotActiveError."""
    if deployment.status not in ACTIVE_STATUSES:
        raise DeploymentNotActiveError(deployment.id, deployment.status)


def _touch(deployment: Deployment) -> None:
    """Explicitly dirties the Deployment row so its version_id_col (optimistic
    lock) is checked and bumped on every action that goes through this module -
    some actions (e.g. advance_traffic's success path) only otherwise mutate
    TrafficAllocation, a *different* table, which wouldn't by itself put Deployment
    in that flush's UPDATE and would silently skip the stale-version check.
    """
    deployment.updated_at = datetime.now(UTC)


def _commit(db: Session, deployment: Deployment) -> None:
    """db.commit(), translating a lost optimistic-lock race into
    ConcurrentUpdateError instead of letting StaleDataError (a SQLAlchemy-internal
    exception type) leak past this module."""
    try:
        db.commit()
    except StaleDataError as exc:
        db.rollback()
        raise ConcurrentUpdateError(deployment.id) from exc


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

    existing_active = get_active_deployment(db, model_name)
    if existing_active is not None:
        raise ActiveDeploymentExistsError(model_name, existing_active.id)

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
    try:
        db.flush()
    except IntegrityError as exc:
        # Defense-in-depth against the TOCTOU race the pre-check above can't close
        # on its own: two concurrent requests can both see "no active deployment"
        # before either commits. The DB-level partial unique index
        # (uq_deployments_active_per_model - see the migration and
        # docs/DESIGN_NOTES.md) is what actually makes this exclusive; this is just
        # translating its violation into the same ActiveDeploymentExistsError the
        # pre-check raises, so callers don't need to know two different mechanisms
        # exist. Must not swallow an idempotency_key collision here - that's a
        # different constraint, on the same statement, with a different meaning.
        db.rollback()
        orig_message = str(exc.orig) if exc.orig is not None else str(exc)
        if "idempotency_key" in orig_message:
            raise
        winner = get_active_deployment(db, model_name)
        raise ActiveDeploymentExistsError(
            model_name, winner.id if winner is not None else "<unknown - lost the race>"
        ) from exc
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
    _touch(deployment)
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
        _commit(db, deployment)
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

    _commit(db, deployment)
    db.refresh(deployment)
    return deployment


async def rollback_deployment(
    db: Session, router_gateway: RouterGateway, deployment: Deployment, triggered_by: str = "manual"
) -> Deployment:
    """Roll back all traffic to the stable version. `triggered_by` distinguishes a
    manual dashboard click from a worker's automated FAIL decision in the event log.
    """
    _touch(deployment)
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
        _commit(db, deployment)
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

    _commit(db, deployment)
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
    require_active(deployment)
    _touch(deployment)

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
        _commit(db, deployment)
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

    _commit(db, deployment)
    db.refresh(deployment)
    return deployment


def record_inconclusive(db: Session, deployment: Deployment, max_retries: int) -> Deployment:
    """Called by the worker when an automated evaluation comes back INCONCLUSIVE.
    Increments the retry counter; once it exceeds `max_retries`, freezes the
    deployment into INCONCLUSIVE status so the worker stops retrying and a human can
    look at it - an unlabeled canary otherwise stays INCONCLUSIVE forever, which is
    the expected (not buggy) outcome while there's no actual_label source (Sprint 5).
    """
    require_active(deployment)
    _touch(deployment)

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

    _commit(db, deployment)
    db.refresh(deployment)
    return deployment
