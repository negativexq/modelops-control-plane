from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LatencyPolicy(BaseModel):
    # Always present in responses, so the API schema marks it required there.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    p95_max_increase_percent: float = Field(default=20.0, ge=0)


class ReliabilityPolicy(BaseModel):
    # Always present in responses, so the API schema marks it required there.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    max_error_rate_percent: float = Field(default=5.0, ge=0)


class QualityPolicy(BaseModel):
    # Always present in responses, so the API schema marks it required there.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    minimum_recall: float = Field(default=0.8, ge=0, le=1)


class PolicyConfig(BaseModel):
    """A named set of promotion-gate thresholds for one evaluation run.

    `evaluation_window_seconds` is part of the policy definition itself, not a
    caller-supplied query parameter (unlike the comparison endpoint's
    `window_seconds`) - a policy's outcome should be reproducible from the policy
    alone, not depend on whatever window a client happened to pass that call.
    """

    # Always present in responses, so the API schema marks it required there.
    model_config = ConfigDict(json_schema_serialization_defaults_required=True)

    minimum_requests: int = Field(default=100, ge=0)
    evaluation_window_seconds: int = Field(default=300, ge=1)
    # How many consecutive INCONCLUSIVE automated evaluations a deployment tolerates
    # (e.g. while waiting for enough traffic, or for a label backlog to catch up)
    # before the worker freezes it into INCONCLUSIVE status for a human to look at,
    # instead of retrying forever.
    max_inconclusive_retries: int = Field(default=10, ge=1)
    # Quality checks (minimum_labeled_samples, minimum_label_coverage,
    # minimum_recall) read an *older* window than reliability checks - see
    # app/policy/engine.py's evaluate_quality_policies and docs/DESIGN_NOTES.md.
    # Labels arrive delayed (POST /api/labels), so the freshest window is
    # definitionally the least-labeled one; without this, a healthy deployment
    # would see its quality checks stay perpetually INCONCLUSIVE, not because
    # anything is wrong, but because ground truth for the newest predictions simply
    # hasn't arrived yet.
    label_maturity_seconds: int = Field(default=60, ge=0)
    minimum_labeled_samples: int = Field(default=30, ge=0)
    minimum_label_coverage: float = Field(default=0.5, ge=0, le=1)
    # recall's denominator is positive (actual_label=1) examples, not labeled
    # samples in general - on a low-positive-rate dataset, minimum_labeled_samples
    # can pass while the window still holds only 1-3 positives, making recall
    # statistically meaningless. Checked after minimum_labeled_samples/
    # minimum_label_coverage, before minimum_recall - see
    # app/policy/engine.py's evaluate_quality_policies.
    minimum_positive_labels: int = Field(default=30, ge=0)
    # How sure the engine must be before it calls a breach real. Error rate,
    # latency and recall checks only FAIL when the whole confidence interval at
    # this level lies past the limit; past the limit but inside the noise is
    # INCONCLUSIVE (see app/policy/stats.py and engine.py's _decide_* helpers).
    confidence_level: float = Field(default=0.95, ge=0.5, le=0.999)
    # Spend the confidence level's error budget across a rollout's repeated
    # evaluations instead of granting it to each one, so re-checking a healthy
    # canary every worker cycle can't add up to a likely false rollback - see
    # app/policy/stats.py's look_confidence.
    sequential_testing: bool = True
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
    label_maturity_seconds: int = 60
    minimum_labeled_samples: int = 30
    minimum_label_coverage: float = 0.5
    minimum_positive_labels: int = 30
    confidence_level: float = 0.95
    sequential_testing: bool = True
    latency_p95_max_increase_percent: float = 20.0
    reliability_max_error_rate_percent: float = 5.0
    quality_minimum_recall: float = 0.8

    def to_policy_config(self) -> PolicyConfig:
        return PolicyConfig(
            minimum_requests=self.minimum_requests,
            evaluation_window_seconds=self.evaluation_window_seconds,
            max_inconclusive_retries=self.max_inconclusive_retries,
            label_maturity_seconds=self.label_maturity_seconds,
            minimum_labeled_samples=self.minimum_labeled_samples,
            minimum_label_coverage=self.minimum_label_coverage,
            minimum_positive_labels=self.minimum_positive_labels,
            confidence_level=self.confidence_level,
            sequential_testing=self.sequential_testing,
            latency=LatencyPolicy(p95_max_increase_percent=self.latency_p95_max_increase_percent),
            reliability=ReliabilityPolicy(
                max_error_rate_percent=self.reliability_max_error_rate_percent
            ),
            quality=QualityPolicy(minimum_recall=self.quality_minimum_recall),
        )


policy_settings = PolicySettings()
