"""Closes drift between desired state (DB: Deployment + TrafficAllocation - the
durable source of truth) and the router's observed state (in-memory, applied
best-effort, lost on restart). No outbox table: desired state is already
durable in tables that exist for other reasons, so a second durable copy of the
same information would be redundant, not more correct - see
docs/DESIGN_NOTES.md#desired-observed-reconciliation.

Deliberately lives in the control plane, not the worker: the worker never talks
to infrastructure directly (see docs/DESIGN_NOTES.md#automated-promotion--
rollback), only the control plane's own REST API - it has no RouterGateway and
can't compare desired vs. observed on its own. POST /api/router/reconcile
(app/control_plane/api.py) is what lets the worker's poll loop trigger this
without crossing that boundary.
"""

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane import service
from app.control_plane.models import Deployment, DeploymentEvent
from app.control_plane.router_gateway import RouterGateway, RouterUpdateError, StaleRevisionError

logger = logging.getLogger("control_plane")


@dataclass(frozen=True)
class ReconcileResult:
    reconciled: bool
    reason: str
    deployment_id: str | None = None
    from_revision: int | None = None
    to_revision: int | None = None


def _all_active_deployments(db: Session) -> list[Deployment]:
    """Used only when the router is fully unreachable (GET itself failed), so
    there's no observed model_name to look up a specific deployment by -
    marks every currently-active deployment unreachable instead. Under this
    project's single-router-instance assumption (see docs/DESIGN_NOTES.md
    #desired-observed-reconciliation) there's realistically at most one."""
    stmt = select(Deployment).where(Deployment.status.in_(service.ACTIVE_STATUSES))
    return list(db.execute(stmt).scalars().all())


async def reconcile_router_state(db: Session, router_gateway: RouterGateway) -> ReconcileResult:
    """Compares the router's observed (model_name, deployment_id, revision) against
    the DB's authoritative desired state for that model (see
    service.get_authoritative_allocation - the active rollout if one is in
    flight, otherwise the most recent PROMOTED/ROLLED_BACK deployment's final
    allocation), and re-pushes if they differ.

    Using the authoritative allocation rather than only the active deployment is
    what makes this still work *after* a rollout finishes: a router push that
    fails right after a promote/rollback commit (or a router restart after one)
    used to have nothing left to reconcile against once that deployment left
    CANARY_RUNNING/EVALUATING - the drift became permanent instead of closing
    on the next tick. See docs/DESIGN_NOTES.md#desired-observed-reconciliation.

    No-op (no push, no DeploymentEvent) when they already match, or when there's
    nothing to compare against (router unreachable, or no deployment at all yet
    for the router's model) - reconciliation should be silent when there's
    nothing to reconcile, not spam the timeline every tick just for checking.
    """
    observed = await router_gateway.get_observed_config()
    if observed is None:
        for deployment in _all_active_deployments(db):
            service.record_router_reachability_change(db, deployment.id, reachable=False)
        return ReconcileResult(reconciled=False, reason="router unreachable")

    model_name = observed.get("model_name")
    if not model_name:
        return ReconcileResult(reconciled=False, reason="router reported no model_name")

    desired_deployment = service.get_authoritative_allocation(db, model_name)
    if desired_deployment is None or desired_deployment.traffic_allocation is None:
        return ReconcileResult(reconciled=False, reason="no authoritative allocation")

    # The GET above succeeded, so the router answered - it's reachable, even if
    # the push below then fails (that's recorded separately as a fresh
    # transition, since it's a distinct signal at a distinct moment).
    service.record_router_reachability_change(db, desired_deployment.id, reachable=True)

    desired_revision = desired_deployment.traffic_allocation.revision
    observed_deployment_id = observed.get("deployment_id")
    observed_revision = observed.get("revision", 0)

    if observed_deployment_id == desired_deployment.id and observed_revision == desired_revision:
        return ReconcileResult(reconciled=False, reason="already in sync")

    targets: list[dict[str, Any]] = desired_deployment.traffic_allocation.targets
    try:
        await router_gateway.push_traffic_allocation(
            desired_deployment.model_name, desired_deployment.id, desired_revision, targets
        )
    except StaleRevisionError:
        # Something else (a concurrent promote/rollback, or another reconcile
        # tick) already pushed this same or a newer revision between our GET and
        # this PUT - we're actually in sync now. Not an error, no event: nothing
        # this call did actually changed anything.
        return ReconcileResult(reconciled=False, reason="already in sync (race)")
    except RouterUpdateError as exc:
        logger.warning("reconcile push failed for deployment %s, will retry next tick: %s",
                        desired_deployment.id, exc)
        service.record_router_reachability_change(db, desired_deployment.id, reachable=False)
        return ReconcileResult(reconciled=False, reason="router unreachable")

    db.add(
        DeploymentEvent(
            deployment_id=desired_deployment.id,
            event_type="router_reconciled",
            message=(
                f"reconciled router config: deployment={observed_deployment_id} "
                f"revision={observed_revision} -> deployment={desired_deployment.id} "
                f"revision={desired_revision}"
            ),
        )
    )
    logger.info(
        "deployment %s: reconciled router from (deployment=%s, revision=%s) to revision %s",
        desired_deployment.id,
        observed_deployment_id,
        observed_revision,
        desired_revision,
    )
    db.commit()

    return ReconcileResult(
        reconciled=True,
        reason="corrected drift",
        deployment_id=desired_deployment.id,
        from_revision=observed_revision,
        to_revision=desired_revision,
    )
