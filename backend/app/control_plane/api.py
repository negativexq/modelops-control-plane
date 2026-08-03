from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.control_plane import metrics_service, service
from app.control_plane.models import Deployment
from app.control_plane.router_gateway import RouterGateway, get_router_gateway
from app.control_plane.schemas import (
    ComparisonOut,
    CreateDeploymentRequest,
    DeploymentOut,
    MetricIn,
    MetricsOut,
)
from app.control_plane.service import DeploymentNotFoundError
from app.control_plane.state_machine import InvalidTransitionError
from app.db import get_db

router = APIRouter(prefix="/api/deployments", tags=["deployments"])
router_config_router = APIRouter(prefix="/api/router-config", tags=["router-config"])

DbDep = Annotated[Session, Depends(get_db)]
RouterGatewayDep = Annotated[RouterGateway, Depends(get_router_gateway)]
IdempotencyKeyDep = Annotated[str | None, Header(alias="Idempotency-Key")]


def _not_found(deployment_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Deployment '{deployment_id}' not found")


@router.post("", response_model=DeploymentOut)
async def create_deployment(
    payload: CreateDeploymentRequest,
    response: Response,
    db: DbDep,
    router_gateway: RouterGatewayDep,
    idempotency_key: IdempotencyKeyDep = None,
) -> DeploymentOut:
    deployment, created = await service.create_deployment(
        db=db,
        router_gateway=router_gateway,
        model_name=payload.model_name,
        stable_version=payload.stable_version,
        canary_version=payload.canary_version,
        canary_weight=payload.canary_weight,
        idempotency_key=idempotency_key,
    )
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


@router.post("/{deployment_id}/promote")
async def promote_deployment(
    deployment_id: str, db: DbDep, router_gateway: RouterGatewayDep
) -> DeploymentOut:
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    try:
        deployment = await service.promote_deployment(db, router_gateway, deployment)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return DeploymentOut.model_validate(deployment)


@router.post("/{deployment_id}/rollback")
async def rollback_deployment(
    deployment_id: str, db: DbDep, router_gateway: RouterGatewayDep
) -> DeploymentOut:
    try:
        deployment = service.get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    try:
        deployment = await service.rollback_deployment(db, router_gateway, deployment)
    except InvalidTransitionError as exc:
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


@router_config_router.get("/{model_name}")
def get_router_config(model_name: str, db: DbDep) -> dict[str, Any]:
    """The router's startup-sync source: the currently-active traffic allocation for
    this model (status CANARY_RUNNING or EVALUATING - see
    service.get_active_deployment), so a restarted router doesn't boot with a stale
    default split.

    Deliberately minimal - a plain GET, no push/webhook, no polling loop. The control
    plane remains the source of truth; the router just reads it once at startup.
    """
    deployment = service.get_active_deployment(db, model_name)
    if deployment is None or deployment.traffic_allocation is None:
        raise HTTPException(
            status_code=404, detail=f"No active traffic allocation on record for '{model_name}'"
        )
    return {
        "model_name": model_name,
        "deployment_id": deployment.id,
        "targets": deployment.traffic_allocation.targets,
    }
