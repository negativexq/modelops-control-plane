import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.control_plane.models import (
    TERMINAL_STATUSES,
    Deployment,
    DeploymentEvent,
    DeploymentStatus,
    RoutingGeneration,
    TrafficAllocation,
)
from app.control_plane.router_gateway import RouterGateway, RouterUpdateError, StaleRevisionError
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


class DeploymentTerminalError(Exception):
    """Raised by pause_automation/resume_automation when a deployment is already
    PROMOTED/ROLLED_BACK/FAILED - there's nothing left to pause or resume once a
    deployment is done. Deliberately broader than DeploymentNotActiveError's
    CANARY_RUNNING/EVALUATING-only check: pause/resume are meaningful in any
    non-terminal status (including PENDING, DEPLOYING, and INCONCLUSIVE, where the
    worker isn't currently touching the deployment but an operator may still want
    to record intent before it becomes active again).
    """

    def __init__(self, deployment_id: str, status: DeploymentStatus) -> None:
        self.deployment_id = deployment_id
        self.status = status
        super().__init__(
            f"deployment {deployment_id} is terminal (status={status.value}) - "
            "nothing to pause or resume"
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
    this model. Used by exclusivity checks (create_deployment) and the automation
    hold - both need this narrower question ("is there a live rollout right now"),
    not "what should the router be serving" - see get_authoritative_allocation for
    that broader question and why the two are kept deliberately separate.
    """
    stmt = (
        select(Deployment)
        .where(Deployment.model_name == model_name, Deployment.status.in_(ACTIVE_STATUSES))
        .order_by(Deployment.created_at.desc())
    )
    return db.execute(stmt).scalars().first()


# Terminal statuses whose final TrafficAllocation remains the router's desired
# state once the rollout that produced it ends - see get_authoritative_allocation.
# Deliberately narrower than TERMINAL_STATUSES: FAILED is excluded on purpose.
_AUTHORITATIVE_TERMINAL_STATUSES = (DeploymentStatus.PROMOTED, DeploymentStatus.ROLLED_BACK)


def get_authoritative_allocation(db: Session, model_name: str) -> Deployment | None:
    """The deployment whose TrafficAllocation the router should currently be
    serving for this model - "what should the router be serving right now",
    not "is this deployment active from automation's point of view" (see
    get_active_deployment for that narrower, load-bearing question -
    exclusivity checks and the automation hold depend on its exact scope, so
    this is a separate function rather than a widened get_active_deployment;
    widening that one would widen those too).

    Checks, in order:

    1. An in-flight rollout (CANARY_RUNNING/EVALUATING) or a frozen one
       (INCONCLUSIVE) - all three leave the deployment's TrafficAllocation as
       the correct desired routing state, just for different reasons (a live
       rollout vs. record_inconclusive's own "freeze the traffic split for
       manual review" contract - see that function's docstring). Grouped
       together (not ACTIVE_STATUSES) because uq_deployments_active_per_model
       already guarantees at most one of these three exists per model at a
       time, so there's nothing to prefer between them.
    2. Otherwise, the most recent PROMOTED/ROLLED_BACK deployment's *final*
       allocation - promote_deployment/rollback_deployment's own commit
       already made it the correct, durable desired state (see
       docs/DESIGN_NOTES.md#desired-observed-reconciliation), and it doesn't
       stop being authoritative just because the deployment is no longer
       "active" or "frozen".

    Before this function existed, the reconciler and the router's startup
    sync only ever looked at ACTIVE_STATUSES, so a router push failure (or
    restart) after a successful promote/rollback - or after a rollout froze
    into INCONCLUSIVE - had nothing left to reconcile against: the drift
    became permanent instead of closing on the next tick. This is the fix for
    that gap.

    FAILED is deliberately excluded from both steps: a FAILED deployment
    never reached a legitimate outcome, so its incomplete traffic split has
    no claim to being authoritative - falling back further (to the most
    recent genuinely-terminal deployment, or the router's own bootstrap
    default if none exists) is more correct. See docs/DESIGN_NOTES.md for the
    full reasoning.
    """
    routing = db.execute(
        select(Deployment)
        .where(
            Deployment.model_name == model_name,
            Deployment.status.in_(
                (*ACTIVE_STATUSES, DeploymentStatus.INCONCLUSIVE),
            ),
        )
        .order_by(Deployment.created_at.desc())
    ).scalars().first()
    if routing is not None:
        return routing
    stmt = (
        select(Deployment)
        .where(
            Deployment.model_name == model_name,
            Deployment.status.in_(_AUTHORITATIVE_TERMINAL_STATUSES),
        )
        .order_by(Deployment.completed_at.desc(), Deployment.created_at.desc())
        .limit(1)
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


def _next_routing_generation(db: Session, model_name: str) -> int:
    """Bumps and returns this model's routing generation counter (see
    RoutingGeneration) - the monotonic source TrafficAllocation.revision is now
    assigned from. Always called from inside the same transaction as the
    Deployment/TrafficAllocation write it's stamping, so it commits atomically
    with them - there's nothing else that ever bumps this row concurrently for
    the same model (uq_deployments_active_per_model already guarantees at most
    one non-terminal deployment per model, and the reconciler only ever
    re-pushes an existing revision, never mints a new one).
    """
    row = db.get(RoutingGeneration, model_name)
    if row is None:
        row = RoutingGeneration(model_name=model_name, generation=1)
        db.add(row)
        return 1
    row.generation += 1
    return row.generation


def _set_traffic_allocation(
    db: Session, deployment: Deployment, targets: list[dict[str, float | str]]
) -> int:
    """Writes `targets` as this deployment's desired TrafficAllocation, stamped
    with the model's next routing generation (see _next_routing_generation) -
    in the same transaction as whatever else the caller is doing (a
    Deployment.status transition, an event log write), so both land in one
    commit together. Returns the new revision, for the caller to pass to the
    router push that follows the commit - see docs/DESIGN_NOTES.md
    #desired-observed-reconciliation.
    """
    generation = _next_routing_generation(db, deployment.model_name)
    if deployment.traffic_allocation is not None:
        deployment.traffic_allocation.targets = targets
        deployment.traffic_allocation.revision = generation
        return generation
    allocation = TrafficAllocation(
        deployment_id=deployment.id, targets=targets, revision=generation
    )
    db.add(allocation)
    return generation


def require_active(deployment: Deployment) -> None:
    """Raises DeploymentNotActiveError unless `deployment` is CANARY_RUNNING or
    EVALUATING. Public (used by policy/api.py's /evaluate guard too, not just the
    worker-only action endpoints in this module) - see DeploymentNotActiveError."""
    if deployment.status not in ACTIVE_STATUSES:
        raise DeploymentNotActiveError(deployment.id, deployment.status)


def require_not_terminal(deployment: Deployment) -> None:
    """Raises DeploymentTerminalError if `deployment` is PROMOTED/ROLLED_BACK/
    FAILED. Used by pause_automation/resume_automation - see require_active for
    the narrower CANARY_RUNNING/EVALUATING-only guard other actions use."""
    if deployment.status in TERMINAL_STATUSES:
        raise DeploymentTerminalError(deployment.id, deployment.status)


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


ROUTER_UNREACHABLE_EVENT = "router_unreachable"
ROUTER_RECOVERED_EVENT = "router_recovered"


def record_router_reachability_change(db: Session, deployment_id: str, *, reachable: bool) -> None:
    """Writes a one-time router_unreachable/router_recovered DeploymentEvent when
    the router's reachability for this deployment actually *changes* -
    never on every failed push or every successful one, only on the
    transition, so a sustained outage produces one event on the way down and
    one on the way back up instead of spamming the timeline every tick.

    "Did it change" is derived from this deployment's own event history
    (the most recent of these two event types) rather than a new column -
    reachability is an observability signal here, not part of the
    desired/observed state the reconciler enforces, so it doesn't need its
    own durable field. See docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    """
    stmt = (
        select(DeploymentEvent)
        .where(
            DeploymentEvent.deployment_id == deployment_id,
            DeploymentEvent.event_type.in_((ROUTER_UNREACHABLE_EVENT, ROUTER_RECOVERED_EVENT)),
        )
        .order_by(DeploymentEvent.created_at.desc())
        .limit(1)
    )
    last = db.execute(stmt).scalars().first()
    was_unreachable = last is not None and last.event_type == ROUTER_UNREACHABLE_EVENT
    if reachable != was_unreachable:
        return  # no change in reachability state - stay silent

    if reachable:
        event_type, message = (
            ROUTER_RECOVERED_EVENT,
            "router is reachable again - traffic state will be reconciled",
        )
    else:
        event_type, message = (
            ROUTER_UNREACHABLE_EVENT,
            "router became unreachable - actual traffic state is unknown until it recovers",
        )
    db.add(DeploymentEvent(deployment_id=deployment_id, event_type=event_type, message=message))
    logger.info("deployment %s: %s", deployment_id, message)
    db.commit()


async def _push_best_effort(
    db: Session,
    router_gateway: RouterGateway,
    model_name: str,
    deployment_id: str,
    revision: int,
    targets: list[dict[str, float | str]],
) -> None:
    """Pushes `targets` to the router as `revision`, called ONLY after the same
    desired state has already been committed to the DB (see
    docs/DESIGN_NOTES.md#desired-observed-reconciliation for why that ordering
    matters). Never raises: a stale-revision rejection or router unreachability
    just leaves DB (desired) and router (observed) temporarily diverged, which
    `reconcile.reconcile_router_state` (driven by the worker's own poll loop, via
    POST /api/router/reconcile) closes on its own next tick. This function's job
    is best-effort, immediate convergence, not lossless delivery - the reconciler
    is what actually guarantees convergence.
    """
    try:
        await router_gateway.push_traffic_allocation(model_name, deployment_id, revision, targets)
    except StaleRevisionError as exc:
        logger.info(
            "deployment %s: router already at revision >= %s, not applying: %s",
            deployment_id,
            revision,
            exc,
        )
        # A 409 means the router answered - it's reachable, just already ahead.
        record_router_reachability_change(db, deployment_id, reachable=True)
        return
    except RouterUpdateError as exc:
        logger.warning(
            "deployment %s: router push failed for revision %s - the reconciler "
            "will retry on its next tick: %s",
            deployment_id,
            revision,
            exc,
        )
        record_router_reachability_change(db, deployment_id, reachable=False)
        return
    record_router_reachability_change(db, deployment_id, reachable=True)


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
    automation_paused: bool = False,
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
        automation_paused=automation_paused,
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
    if automation_paused:
        _log_event(
            db,
            deployment,
            "automation_paused",
            "automation paused at creation (manual) - the worker will not act on "
            "this deployment until it's resumed",
        )

    targets: list[dict[str, float | str]] = [
        {"version": stable_version, "weight": round(1 - canary_weight, 6)},
        {"version": canary_version, "weight": round(canary_weight, 6)},
    ]

    _transition(db, deployment, DeploymentStatus.DEPLOYING, "starting canary rollout")
    deployment.started_at = datetime.now(UTC)

    # Desired state (DB) is committed FIRST, router push happens after - see
    # docs/DESIGN_NOTES.md#desired-observed-reconciliation for why the reverse
    # order (push-then-commit, this project's original design) was wrong: a
    # push that lands but whose commit then loses a race left the router
    # observing state the DB never actually agreed to. Pushing after commit can
    # still fail or land stale, but that only ever leaves DB and router
    # temporarily diverged - never inconsistent about what's *authoritative* -
    # and the reconciler closes that gap on its own next tick. A router push
    # failure here no longer transitions the deployment to FAILED for the same
    # reason: the desired state this deployment now has (CANARY_RUNNING) is
    # correct regardless of whether the router has caught up to it yet.
    revision = _set_traffic_allocation(db, deployment, targets)
    _transition(db, deployment, DeploymentStatus.CANARY_RUNNING, "canary receiving traffic")

    db.commit()
    db.refresh(deployment)

    await _push_best_effort(db, router_gateway, model_name, deployment.id, revision, targets)
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
    # Commit desired state before pushing to the router - see create_deployment's
    # comment and docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    revision = _set_traffic_allocation(db, deployment, targets)
    _transition(
        db,
        deployment,
        DeploymentStatus.PROMOTED,
        f"canary promoted to 100% traffic ({triggered_by})",
    )
    deployment.completed_at = datetime.now(UTC)

    _commit(db, deployment)
    db.refresh(deployment)

    await _push_best_effort(
        db, router_gateway, deployment.model_name, deployment.id, revision, targets
    )
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
    # Commit desired state before pushing to the router - see create_deployment's
    # comment and docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    revision = _set_traffic_allocation(db, deployment, targets)
    _transition(
        db,
        deployment,
        DeploymentStatus.ROLLED_BACK,
        f"traffic rolled back to stable ({triggered_by})",
    )
    deployment.completed_at = datetime.now(UTC)

    _commit(db, deployment)
    db.refresh(deployment)

    await _push_best_effort(
        db, router_gateway, deployment.model_name, deployment.id, revision, targets
    )
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
    # Commit desired state before pushing to the router - see create_deployment's
    # comment and docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    revision = _set_traffic_allocation(db, deployment, targets)
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

    await _push_best_effort(
        db, router_gateway, deployment.model_name, deployment.id, revision, targets
    )
    return deployment


def record_inconclusive(db: Session, deployment: Deployment, max_retries: int) -> Deployment:
    """Called by the worker when an automated evaluation comes back INCONCLUSIVE.
    Increments the retry counter; once it exceeds `max_retries`, freezes the
    deployment into INCONCLUSIVE status so the worker stops retrying and a human can
    look at it - a canary whose quality window never matures enough labeled data
    (see app/policy/engine.py's quality data-sufficiency gate) otherwise stays
    INCONCLUSIVE forever, which is the expected (not buggy) outcome.
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


def pause_automation(
    db: Session, deployment: Deployment, triggered_by: str = "manual"
) -> Deployment:
    """Sets automation_paused=True - the worker's next sweep (app/worker/loop.py's
    run_once) will skip this deployment entirely, before it ever calls /evaluate.
    Manual /evaluate, /promote, /rollback remain unaffected; this only stops the
    automated actor.

    Idempotent: pausing an already-paused deployment is a silent no-op (no new
    event, no version bump) rather than an error, so a double-click or a retried
    request doesn't spam the timeline with duplicate "automation paused" entries.
    """
    require_not_terminal(deployment)
    if deployment.automation_paused:
        return deployment

    _touch(deployment)
    deployment.automation_paused = True
    _log_event(db, deployment, "automation_paused", f"automation paused ({triggered_by})")

    _commit(db, deployment)
    db.refresh(deployment)
    return deployment


def resume_automation(
    db: Session, deployment: Deployment, triggered_by: str = "manual"
) -> Deployment:
    """Sets automation_paused=False - the worker will pick this deployment back up
    on its next sweep, same as any other active deployment. Idempotent, same
    reasoning as pause_automation."""
    require_not_terminal(deployment)
    if not deployment.automation_paused:
        return deployment

    _touch(deployment)
    deployment.automation_paused = False
    _log_event(db, deployment, "automation_resumed", f"automation resumed ({triggered_by})")

    _commit(db, deployment)
    db.refresh(deployment)
    return deployment
