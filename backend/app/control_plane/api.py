import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.control_plane import metrics_service, reconcile, service
from app.control_plane.models import Deployment
from app.control_plane.router_gateway import RouterGateway, get_router_gateway
from app.control_plane.schemas import (
    ComparisonOut,
    CreateDeploymentRequest,
    DeploymentOut,
    MetricIn,
    MetricsOut,
    MetricsTimeseriesOut,
    TimelineItem,
)
from app.control_plane.service import (
    ActiveDeploymentExistsError,
    AlreadyAtFinalStageError,
    ConcurrentUpdateError,
    DeploymentNotActiveError,
    DeploymentNotFoundError,
    DeploymentTerminalError,
)
from app.control_plane.state_machine import InvalidTransitionError
from app.control_plane.timeline import build_timeline
from app.db import get_db

TriggeredByDep = Literal["manual", "automatic"]

router = APIRouter(prefix="/api/deployments", tags=["deployments"])
router_config_router = APIRouter(prefix="/api/router-config", tags=["router-config"])
reconcile_router = APIRouter(prefix="/api/router", tags=["router"])

DbDep = Annotated[Session, Depends(get_db)]
RouterGatewayDep = Annotated[RouterGateway, Depends(get_router_gateway)]
IdempotencyKeyDep = Annotated[str | None, Header(alias="Idempotency-Key")]

_OPERATOR_RE = re.compile(r"^[\w .@'-]{1,64}$")


def operator_name(x_operator: Annotated[str | None, Header()] = None) -> str | None:
    """The operator's self-reported name (X-Operator header), recorded on the events
    a manual action writes. Not authentication: it answers "who clicked this" in the
    audit trail, nothing more. Missing or blank means unknown, not an error.

    HTTP header values can't carry most non-Latin-1 text, so clients percent-encode
    names like "Ömer" or "Şule"; plain ASCII names arrive as-is."""
    if x_operator is None or not x_operator.strip():
        return None
    name = " ".join(unquote(x_operator).split())
    if not _OPERATOR_RE.fullmatch(name):
        raise HTTPException(
            status_code=422,
            detail="X-Operator must be 1-64 characters: letters, digits, space, . @ ' - _",
        )
    return name


OperatorDep = Annotated[str | None, Depends(operator_name)]


def _manual_actor(triggered_by: str, operator: str | None) -> str | None:
    # The worker never acts on someone's behalf; ignore a stray header on its calls.
    return operator if triggered_by == "manual" else None


def _not_found(deployment_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Deployment '{deployment_id}' not found")


@router.post("", response_model=DeploymentOut)
async def create_deployment(
    payload: CreateDeploymentRequest,
    response: Response,
    db: DbDep,
    router_gateway: RouterGatewayDep,
    operator: OperatorDep,
    idempotency_key: IdempotencyKeyDep = None,
) -> DeploymentOut:
    try:
        deployment, created = await service.create_deployment(
            db=db,
            router_gateway=router_gateway,
            model_name=payload.model_name,
            stable_version=payload.stable_version,
            canary_version=payload.canary_version,
            canary_weight=payload.canary_weight,
            idempotency_key=idempotency_key,
            policy_config=payload.policy_config,
            automation_paused=payload.automation_paused,
            actor=operator,
        )
    except ActiveDeploymentExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return DeploymentOut.model_validate(deployment)


@router.get("")
def list_deployments(db: DbDep) -> list[DeploymentOut]:
    deployments = service.list_deployments(db)
    return [DeploymentOut.model_validate(d) for d in deployments]


@router.get("/{deployment_id}")
def get_deployment(deployment_id: str, db: DbDep) -> DeploymentOut:
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc
    return DeploymentOut.model_validate(deployment)


@router.get("/{deployment_id}/timeline", response_model=list[TimelineItem])
def get_deployment_timeline(deployment_id: str, db: DbDep) -> list[TimelineItem]:
    """DeploymentEvent (state transitions, worker actions) and PolicyEvaluation
    (per-check results, with a derived human-readable explanation) merged into one
    chronological list - see app/control_plane/timeline.py for how."""
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    return build_timeline(db, deployment)


@router.post("/{deployment_id}/promote")
async def promote_deployment(
    deployment_id: str,
    db: DbDep,
    router_gateway: RouterGatewayDep,
    operator: OperatorDep,
    triggered_by: TriggeredByDep = "manual",
) -> DeploymentOut:
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    try:
        deployment = await service.promote_deployment(
            db,
            router_gateway,
            deployment,
            triggered_by=triggered_by,
            actor=_manual_actor(triggered_by, operator),
        )
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeploymentOut.model_validate(deployment)


@router.post("/{deployment_id}/rollback")
async def rollback_deployment(
    deployment_id: str,
    db: DbDep,
    router_gateway: RouterGatewayDep,
    operator: OperatorDep,
    triggered_by: TriggeredByDep = "manual",
) -> DeploymentOut:
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    try:
        deployment = await service.rollback_deployment(
            db,
            router_gateway,
            deployment,
            triggered_by=triggered_by,
            actor=_manual_actor(triggered_by, operator),
        )
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeploymentOut.model_validate(deployment)


@router.post("/{deployment_id}/pause-automation")
def pause_automation(
    deployment_id: str, db: DbDep, operator: OperatorDep, triggered_by: TriggeredByDep = "manual"
) -> DeploymentOut:
    """Stops the automated worker from touching this deployment - see
    Deployment.automation_paused and docs/DESIGN_NOTES.md#manual-automation-hold.
    Manual /evaluate, /promote, /rollback are unaffected. 409 if the deployment is
    already terminal (nothing left to pause)."""
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    try:
        deployment = service.pause_automation(
            db, deployment, triggered_by=triggered_by, actor=_manual_actor(triggered_by, operator)
        )
    except DeploymentTerminalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeploymentOut.model_validate(deployment)


@router.post("/{deployment_id}/resume-automation")
def resume_automation(
    deployment_id: str, db: DbDep, operator: OperatorDep, triggered_by: TriggeredByDep = "manual"
) -> DeploymentOut:
    """Lets the automated worker act on this deployment again. 409 if the
    deployment is already terminal."""
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    try:
        deployment = service.resume_automation(
            db, deployment, triggered_by=triggered_by, actor=_manual_actor(triggered_by, operator)
        )
    except DeploymentTerminalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeploymentOut.model_validate(deployment)


@router.post("/{deployment_id}/advance-traffic")
async def advance_traffic(
    deployment_id: str, db: DbDep, router_gateway: RouterGatewayDep
) -> DeploymentOut:
    """Used exclusively by the automated worker (app/worker/) to move the canary to
    the next traffic stage after a PASS evaluation. Rejects (409) if the deployment
    isn't CANARY_RUNNING/EVALUATING (e.g. a human already acted on it) or if the
    canary is already at 100% (use /promote instead)."""
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    try:
        deployment = await service.advance_traffic(db, router_gateway, deployment)
    except DeploymentNotActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AlreadyAtFinalStageError as exc:
        raise HTTPException(
            status_code=409, detail=f"{exc} - use /promote instead"
        ) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeploymentOut.model_validate(deployment)


@router.post("/{deployment_id}/record-inconclusive")
def record_inconclusive(deployment_id: str, db: DbDep) -> DeploymentOut:
    """Used exclusively by the automated worker when an evaluation comes back
    INCONCLUSIVE: bumps the retry counter and freezes the deployment into
    INCONCLUSIVE status once policy_config.max_inconclusive_retries is exceeded."""
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    max_retries = service.get_policy_config(deployment).max_inconclusive_retries
    try:
        deployment = service.record_inconclusive(db, deployment, max_retries)
    except DeploymentNotActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConcurrentUpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeploymentOut.model_validate(deployment)


@router.post("/{deployment_id}/metrics", status_code=status.HTTP_202_ACCEPTED)
def record_metric(deployment_id: str, payload: MetricIn, db: DbDep) -> dict[str, bool]:
    """Hot path: the router calls this once per forwarded /predict, fire-and-forget.

    Deliberately minimal - a single PK existence check (no relationship loading, no
    join) plus a plain insert. No state-machine involvement, no event logging.
    """
    if db.get(Deployment, deployment_id) is None:
        raise _not_found(deployment_id)
    metrics_service.record_metric(db, deployment_id, payload)
    return {"recorded": True}


@router.get("/{deployment_id}/metrics/timeseries", response_model=MetricsTimeseriesOut)
def get_deployment_metrics_timeseries(
    deployment_id: str,
    db: DbDep,
    window_seconds: Annotated[int | None, Query(ge=10, le=7 * 86400)] = None,
) -> MetricsTimeseriesOut:
    """Latency and error rate over time, stable vs canary, bucketed. Covers the
    whole rollout (start to decision, or to now while live) unless window_seconds
    narrows it to the most recent part."""
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    end = deployment.completed_at or datetime.now(UTC)
    start = deployment.started_at or deployment.created_at
    if window_seconds is not None:
        start = max(
            metrics_service.as_utc(start),
            metrics_service.as_utc(end) - timedelta(seconds=window_seconds),
        )
    return metrics_service.compute_timeseries(
        db,
        deployment.id,
        [(deployment.stable_version, "stable"), (deployment.canary_version, "canary")],
        start,
        end,
    )


@router.get("/{deployment_id}/metrics", response_model=MetricsOut)
def get_deployment_metrics(
    deployment_id: str, db: DbDep, window_seconds: int = 300
) -> MetricsOut:
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    return MetricsOut(
        window_seconds=window_seconds,
        stable=metrics_service.compute_version_summary(
            db, deployment_id, deployment.stable_version, window_seconds
        ),
        canary=metrics_service.compute_version_summary(
            db, deployment_id, deployment.canary_version, window_seconds
        ),
    )


@router.get("/{deployment_id}/comparison", response_model=ComparisonOut)
def get_deployment_comparison(
    deployment_id: str, db: DbDep, window_seconds: int = 300
) -> ComparisonOut:
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    stable = metrics_service.compute_version_summary(
        db, deployment_id, deployment.stable_version, window_seconds
    )
    canary = metrics_service.compute_version_summary(
        db, deployment_id, deployment.canary_version, window_seconds
    )
    return ComparisonOut(
        window_seconds=window_seconds,
        stable=stable,
        canary=canary,
        deltas=metrics_service.compute_deltas(stable, canary),
    )


def _router_config_payload(deployment: Deployment) -> dict[str, Any]:
    assert deployment.traffic_allocation is not None
    return {
        "model_name": deployment.model_name,
        "deployment_id": deployment.id,
        "targets": deployment.traffic_allocation.targets,
    }


@router_config_router.get("")
def list_router_configs(db: DbDep) -> list[dict[str, Any]]:
    """The router's startup-sync source: every model's authoritative traffic
    allocation (see get_router_config below for what "authoritative" means).
    Deliberately without revision, same as the single-model form: the router
    boots with the right split, and the reconciler stamps the revision on its
    next tick."""
    return [
        _router_config_payload(d)
        for d in reconcile.all_authoritative_deployments(db)
        if d.traffic_allocation is not None
    ]


@router_config_router.get("/{model_name}")
def get_router_config(model_name: str, db: DbDep) -> dict[str, Any]:
    """The authoritative traffic allocation for one model (see
    service.get_authoritative_allocation - the active rollout if one is in
    flight, otherwise the most recent PROMOTED/ROLLED_BACK deployment's final
    allocation), so a restarted router doesn't boot with a stale default split -
    including after a rollout has already *finished*. See
    docs/DESIGN_NOTES.md#desired-observed-reconciliation.

    Deliberately minimal - a plain GET, no push/webhook, no polling loop. The control
    plane remains the source of truth; the router just reads it once at startup.
    """
    deployment = service.get_authoritative_allocation(db, model_name)
    if deployment is None or deployment.traffic_allocation is None:
        raise HTTPException(
            status_code=404, detail=f"No active traffic allocation on record for '{model_name}'"
        )
    return _router_config_payload(deployment)


@reconcile_router.get("/observed")
async def get_observed_router_state(router_gateway: RouterGatewayDep) -> dict[str, Any]:
    """Read-only passthrough of every model's observed split on the router (GET
    /router/configs) - never mutates anything, unlike POST /reconcile. Exists so
    the dashboard (which only ever talks to the control plane, not the router
    directly) can show desired-vs-observed drift without triggering a
    correction just by loading a page.

    `reachable` is the field the dashboard must check first: a router that's
    down and one that's up but doesn't hold a split for some model both leave
    that model out of `configs`, so only whether the GET itself succeeded can
    tell them apart. See docs/DESIGN_NOTES.md#desired-observed-reconciliation.
    """
    observed = await router_gateway.get_observed_configs()
    if observed is None:
        return {"reachable": False, "configs": []}
    return {"reachable": True, "configs": observed}


@reconcile_router.post("/reconcile")
async def reconcile_router_state(db: DbDep, router_gateway: RouterGatewayDep) -> dict[str, Any]:
    """Compares the router's observed (deployment_id, revision) for every model
    against the DB's desired state and re-pushes where they differ - see
    app/control_plane/reconcile.py and
    docs/DESIGN_NOTES.md#desired-observed-reconciliation. Called once per sweep by
    the worker's poll loop (app/worker/loop.py); never raises on its own account -
    a genuinely unreachable router or "nothing to reconcile" are both reported as
    `reconciled: false` with a `reason`, not an error.

    The top-level fields summarise the sweep (the first correction made, if any);
    `models` has one entry per model checked.
    """
    results = await reconcile.reconcile_router_state(db, router_gateway)
    first = next((r for r in results if r.reconciled), results[0])
    return {
        "reconciled": any(r.reconciled for r in results),
        "reason": first.reason,
        "deployment_id": first.deployment_id,
        "from_revision": first.from_revision,
        "to_revision": first.to_revision,
        "models": [
            {
                "model_name": r.model_name,
                "reconciled": r.reconciled,
                "reason": r.reason,
                "deployment_id": r.deployment_id,
                "from_revision": r.from_revision,
                "to_revision": r.to_revision,
            }
            for r in results
        ],
    }
