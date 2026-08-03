from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LatencyPolicy(BaseModel):
    p95_max_increase_percent: float = Field(default=20.0, ge=0)


class ReliabilityPolicy(BaseModel):
    max_error_rate_percent: float = Field(default=5.0, ge=0)


class QualityPolicy(BaseModel):
    minimum_recall: float = Field(default=0.8, ge=0, le=1)


class PolicyConfig(BaseModel):
    """A named set of promotion-gate thresholds for one evaluation run.

    `evaluation_window_seconds` is part of the policy definition itself, not a
    caller-supplied query parameter (unlike the comparison endpoint's
    `window_seconds`) - a policy's outcome should be reproducible from the policy
    alone, not depend on whatever window a client happened to pass that call.
    """

    minimum_requests: int = Field(default=100, ge=0)
    evaluation_window_seconds: int = Field(default=300, ge=1)
    # How many consecutive INCONCLUSIVE automated evaluations a deployment tolerates
    # (e.g. while waiting for enough traffic, or - most commonly today - for
    # actual_label data that doesn't exist yet, see Sprint 5) before the worker
    # freezes it into INCONCLUSIVE status for a human to look at, instead of retrying
    # forever.
    max_inconclusive_retries: int = Field(default=10, ge=1)
    latency: LatencyPolicy = LatencyPolicy()
    reliability: ReliabilityPolicy = ReliabilityPolicy()
    quality: QualityPolicy = QualityPolicy()


class PolicySettings(BaseSettings):
    """Environment-configurable defaults for PolicyConfig, following the same
    pattern as RouterSettings/ControlPlaneSettings."""

    model_config = SettingsConfigDict(env_prefix="POLICY_", protected_namespaces=())

    minimum_requests: int = 100
    evaluation_window_seconds: int = 300
    max_inconclusive_retries: int = 10
    latency_p95_max_increase_percent: float = 20.0
    reliability_max_error_rate_percent: float = 5.0
    quality_minimum_recall: float = 0.8

    def to_policy_config(self) -> PolicyConfig:
        return PolicyConfig(
            minimum_requests=self.minimum_requests,
            evaluation_window_seconds=self.evaluation_window_seconds,
            max_inconclusive_retries=self.max_inconclusive_retries,
            latency=LatencyPolicy(p95_max_increase_percent=self.latency_p95_max_increase_percent),
            reliability=ReliabilityPolicy(
                max_error_rate_percent=self.reliability_max_error_rate_percent
            ),
            quality=QualityPolicy(minimum_recall=self.quality_minimum_recall),
        )


policy_settings = PolicySettings()
