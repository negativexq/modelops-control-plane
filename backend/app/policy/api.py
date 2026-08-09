from typing import Annotated

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from app.control_plane.service import (
    DeploymentNotActiveError,
    DeploymentNotFoundError,
    get_deployment,
    get_policy_config,
    require_active,
)
from app.db import get_db
from app.policy import service as policy_service
from app.policy.config import PolicyConfig
from app.policy.schemas import EvaluateResponse, PolicyCheckOut, PolicyEvaluationOut

router = APIRouter(prefix="/api/deployments", tags=["policy"])

DbDep = Annotated[Session, Depends(get_db)]


def _not_found(deployment_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"Deployment '{deployment_id}' not found")


@router.post("/{deployment_id}/evaluate", response_model=EvaluateResponse)
def evaluate_deployment(
    deployment_id: str,
    db: DbDep,
    config: Annotated[PolicyConfig | None, Body()] = None,
) -> EvaluateResponse:
    """Run the policy engine against this deployment's current comparison window and
    record each check as a PolicyEvaluation row. Does not change deployment.status -
    promote/rollback/advance-traffic/record-inconclusive are separate calls (made by
    the worker or a human); this just evaluates and reports. An optional PolicyConfig
    body overrides the deployment's own persisted policy_config for this one call
    only (nothing is re-saved).

    409s if the deployment isn't CANARY_RUNNING or EVALUATING - a terminal
    deployment (PROMOTED/ROLLED_BACK/FAILED/INCONCLUSIVE) has nothing left to
    evaluate, and writing more PolicyEvaluation rows against it would just pollute
    its timeline with checks that can no longer affect anything.
    """
    try:
        deployment = get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    try:
        require_active(deployment)
    except DeploymentNotActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    effective_config = config if config is not None else get_policy_config(deployment)
    checks, overall = policy_service.run_evaluation(db, deployment, effective_config)

    return EvaluateResponse(
        deployment_id=deployment.id,
        overall_result=overall,
        checks=[
            PolicyCheckOut(
                policy_name=check.policy_name,
                metric_name=check.metric_name,
                observed_value=check.observed_value,
                threshold=check.threshold,
                result=check.result,
            )
            for check in checks
        ],
    )


@router.get("/{deployment_id}/policy-evaluations", response_model=list[PolicyEvaluationOut])
def list_policy_evaluations(deployment_id: str, db: DbDep) -> list[PolicyEvaluationOut]:
    try:
        get_deployment(db, deployment_id)
    except DeploymentNotFoundError as exc:
        raise _not_found(deployment_id) from exc

    evaluations = policy_service.list_policy_evaluations(db, deployment_id)
    return [PolicyEvaluationOut.model_validate(evaluation) for evaluation in evaluations]
