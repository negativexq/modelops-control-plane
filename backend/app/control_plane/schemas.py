from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.control_plane.models import DeploymentStatus
from app.policy.config import PolicyConfig


class TargetWeight(BaseModel):
    version: str
    weight: float = Field(ge=0)


class CreateDeploymentRequest(BaseModel):
    model_name: str
    stable_version: str
    canary_version: str
    canary_weight: float = Field(default=0.1, ge=0, le=1)
    # If omitted, the environment-configured PolicySettings defaults are resolved and
    # stored on the deployment at creation time (see service.create_deployment) - the
    # deployment's policy_config is always a fully-resolved snapshot, never None.
    policy_config: PolicyConfig | None = None


class DeploymentEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    event_type: str
    message: str
    created_at: datetime


class TrafficAllocationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    targets: list[TargetWeight]
    updated_at: datetime


class DeploymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    model_name: str
    stable_version: str
    canary_version: str
    status: DeploymentStatus
    policy_config: dict[str, Any] | None
    inconclusive_retry_count: int
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    traffic_allocation: TrafficAllocationOut | None
    events: list[DeploymentEventOut]


class MetricIn(BaseModel):
    """What the router POSTs after each forward. Kept small on purpose - this is the
    hot path."""

    model_version: str
    latency_ms: float = Field(ge=0)
    status_code: int
    prediction: int | None = None
    actual_label: int | None = None


class MetricsSummary(BaseModel):
    version: str
    sample_count: int
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    p99_latency_ms: float | None
    error_rate: float | None
    # precision/recall/false_positive_rate are only meaningful once actual_label has
    # been backfilled for at least one sample in the window - there's no label source
    # yet, so these are None until something starts populating actual_label.
    precision: float | None
    recall: float | None
    false_positive_rate: float | None


class MetricsOut(BaseModel):
    window_seconds: int
    stable: MetricsSummary
    canary: MetricsSummary


class MetricsDeltas(BaseModel):
    p95_latency_ms: float | None
    error_rate: float | None
    recall: float | None


class ComparisonOut(BaseModel):
    window_seconds: int
    stable: MetricsSummary
    canary: MetricsSummary
    deltas: MetricsDeltas
