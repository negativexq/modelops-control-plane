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
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.control_plane import service, telemetry
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
    model_name: str | None = None


def all_authoritative_deployments(db: Session) -> list[Deployment]:
    """Every model's currently-authoritative deployment (see
    service.get_authoritative_allocation): the active rollout if one is in
    flight, otherwise the most recent PROMOTED/ROLLED_BACK one, whatever its
    status. This is the router's whole desired state - one split per model."""
    model_names = db.execute(select(Deployment.model_name).distinct()).scalars().all()
    deployments = []
    for model_name in sorted(model_names):
        deployment = service.get_authoritative_allocation(db, model_name)
        if deployment is not None:
            deployments.append(deployment)
    return deployments


async def _reconcile_model(
    db: Session,
    router_gateway: RouterGateway,
    desired_deployment: Deployment,
    observed: dict[str, Any] | None,
) -> ReconcileResult:
    assert desired_deployment.traffic_allocation is not None
    model_name = desired_deployment.model_name
    desired_revision = desired_deployment.traffic_allocation.revision
    observed_deployment_id = observed.get("deployment_id") if observed else None
    observed_revision = observed.get("revision", 0) if observed else 0

    if observed_deployment_id == desired_deployment.id and observed_revision == desired_revision:
        telemetry.RECONCILE_RUNS.labels(model_name, "in_sync").inc()
        return ReconcileResult(reconciled=False, reason="already in sync", model_name=model_name)

    targets: list[dict[str, Any]] = desired_deployment.traffic_allocation.targets
    try:
        await router_gateway.push_traffic_allocation(
            model_name, desired_deployment.id, desired_revision, targets
        )
    except StaleRevisionError:
        # Something else (a concurrent promote/rollback, or another reconcile
        # tick) already pushed this same or a newer revision between our GET and
        # this PUT - we're actually in sync now. Not an error, no event: nothing
        # this call did actually changed anything.
        telemetry.RECONCILE_RUNS.labels(model_name, "in_sync").inc()
        return ReconcileResult(
            reconciled=False, reason="already in sync (race)", model_name=model_name
        )
    except RouterUpdateError as exc:
        logger.warning(
            "reconcile push failed for deployment %s, will retry next tick: %s",
            desired_deployment.id,
            exc,
        )
        service.record_router_reachability_change(db, desired_deployment.id, reachable=False)
        telemetry.RECONCILE_RUNS.labels(model_name, "push_failed").inc()
        telemetry.RECONCILE_ERRORS.labels("push_failed").inc()
        return ReconcileResult(reconciled=False, reason="router unreachable", model_name=model_name)

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
    telemetry.RECONCILE_RUNS.labels(model_name, "corrected").inc()

    return ReconcileResult(
        reconciled=True,
        reason="corrected drift",
        deployment_id=desired_deployment.id,
        from_revision=observed_revision,
        to_revision=desired_revision,
        model_name=model_name,
    )


async def reconcile_router_state(
    db: Session, router_gateway: RouterGateway
) -> list[ReconcileResult]:
    """For every model with an authoritative allocation in the DB, compares the
    router's observed (deployment_id, revision) for that model against it and
    re-pushes if they differ - including a model the router doesn't hold at all
    (e.g. right after a restart, when only the default model has a bootstrap
    split).

    Using the authoritative allocation rather than only the active deployment is
    what makes this still work *after* a rollout finishes: a router push that
    fails right after a promote/rollback commit (or a router restart after one)
    would otherwise have nothing left to reconcile against. See
    docs/DESIGN_NOTES.md#desired-observed-reconciliation.

    No push and no DeploymentEvent for a model that's already in sync -
    reconciliation should be silent when there's nothing to reconcile, not spam
    the timeline every tick just for checking.
    """
    telemetry.LAST_RECONCILE.set(time.time())
    desired = [d for d in all_authoritative_deployments(db) if d.traffic_allocation is not None]
    observed_list = await router_gateway.get_observed_configs()
    if observed_list is None:
        for deployment in desired:
            service.record_router_reachability_change(db, deployment.id, reachable=False)
            telemetry.RECONCILE_RUNS.labels(deployment.model_name, "router_unreachable").inc()
        telemetry.RECONCILE_ERRORS.labels("router_unreachable").inc()
        return [ReconcileResult(reconciled=False, reason="router unreachable")]

    observed_by_model = {
        item["model_name"]: item for item in observed_list if item.get("model_name")
    }
    results = []
    for deployment in desired:
        # The GET above succeeded, so the router answered - it's reachable, even if
        # the push below then fails (recorded separately as a fresh transition).
        service.record_router_reachability_change(db, deployment.id, reachable=True)
        results.append(
            await _reconcile_model(
                db, router_gateway, deployment, observed_by_model.get(deployment.model_name)
            )
        )
    if not results:
        return [ReconcileResult(reconciled=False, reason="no authoritative allocation")]
    return results
