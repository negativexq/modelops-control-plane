from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.control_plane.models import PolicyEvaluationResult


class PolicyCheckOut(BaseModel):
    policy_name: str
    metric_name: str
    observed_value: float | None
    threshold: float | None
    result: PolicyEvaluationResult


class EvaluateResponse(BaseModel):
    deployment_id: str
    overall_result: PolicyEvaluationResult
    checks: list[PolicyCheckOut]


class PolicyEvaluationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    policy_name: str
    metric_name: str
    observed_value: float | None
    threshold: float | None
    result: PolicyEvaluationResult
    evaluated_at: datetime
