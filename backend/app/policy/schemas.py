from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.control_plane.models import PolicyEvaluationResult


class StatisticalEvidence(BaseModel):
    """Fields shared by every view of a policy check. `ci_low`/`ci_high` are a
    two-sided confidence interval in observed_value's unit; `p_value` is the
    one-sided p-value for "the limit is breached". All None for count gates."""

    # Always present in responses, so the API schema marks it required there.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    ci_low: float | None = None
    ci_high: float | None = None
    p_value: float | None = None
    confidence_level: float | None = None
    stat_method: str | None = None
    # Which evaluation of the rollout this check came from (1 = first) when
    # sequential testing raised confidence_level above the policy's own.
    sequential_look: int | None = None


class PolicyCheckOut(StatisticalEvidence):
    policy_name: str
    metric_name: str
    observed_value: float | None
    threshold: float | None
    result: PolicyEvaluationResult


class EvaluateResponse(BaseModel):
    deployment_id: str
    overall_result: PolicyEvaluationResult
    checks: list[PolicyCheckOut]


class PolicyEvaluationOut(StatisticalEvidence):
    model_config = ConfigDict(
        from_attributes=True, json_schema_serialization_defaults_required=True
    )

    id: str
    policy_name: str
    metric_name: str
    observed_value: float | None
    threshold: float | None
    result: PolicyEvaluationResult
    evaluated_at: datetime
