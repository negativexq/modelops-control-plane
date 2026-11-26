"""Benchmark scenario definitions. Each scenario gets its own model_name so its
Deployment/PolicyEvaluation/DeploymentEvent rows never mix with real ("fraud-model")
deployments created via the dashboard - see run_benchmark.py and the README's
"Benchmark suite" section for the one caveat this does NOT cover (the router itself
has a single active traffic split; benchmarks must be run one at a time, not
concurrently with a real demo you care about).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ExpectedOutcome = Literal["rollback", "promote", "inconclusive_freeze", "throughput_only"]


FaultTarget = Literal["latency", "error"]


@dataclass(frozen=True)
class Scenario:
    key: str
    description: str
    model_name: str
    stable_version: str
    canary_version: str
    canary_weight: float
    policy_config: dict[str, Any]
    # Which always-running fault-injected serving container (if any) to PUT
    # /fault-injection on before the run and reset to zero after - see
    # run_benchmark.py's _set_fault_injection/_reset_fault_injection. Not a
    # docker-compose service to bring up/down: these containers run continuously
    # (fault injection off by default), so no docker CLI/socket access is needed
    # here at all - just an HTTP call to a container that's already up.
    fault_target: FaultTarget | None = None
    fault_latency_ms: int = 0
    fault_error_rate: float = 0.0
    expected_outcome: ExpectedOutcome = "throughput_only"
    load_duration_seconds: int = 120
    max_wait_seconds: int = 120
    # Share of fraud (positive) rows in the generated load; None = the dataset's
    # natural ~2%. A short window at 10% canary holds only a handful of positives
    # at the natural rate, so minimum_positive_labels could never pass.
    positive_ratio: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)
    # User-facing disclaimer for the dashboard's scenario cards (app/benchmarks/) -
    # None for scenarios that reflect real, unmodified platform behavior
    # (baseline/latency-failure/error-failure). Set for quality-failure and success
    # (both use generous, non-default policy thresholds so harness noise doesn't
    # false-trigger a latency FAIL). The dashboard must show this, not just this
    # script's own report.
    synthetic_disclaimer: str | None = None


_DEFAULT_LATENCY_POLICY = {"p95_max_increase_percent": 20.0}
_DEFAULT_RELIABILITY_POLICY = {"max_error_rate_percent": 5.0}

# For scenarios with NO injected fault (quality-failure, success): different model
# architectures genuinely have different real inference latency, and a shared,
# resource-constrained local Docker host adds its own noise on top - confirmed via
# manual end-to-end verification twice: first, v2-quality-bad's real (non-injected)
# latency variance alone was enough to trigger a latency FAIL at a 20% threshold;
# second, "success" tripped false latency FAILs at its old 100% step, where
# stable got no real traffic and was compared idle against a canary carrying the
# whole load. The ramp now promotes at 50% instead (app/traffic_stages.py), so
# both sides always serve real traffic when they're compared.
# These scenarios use a very generous threshold so only an actual injected fault
# (400ms in latency-failure, ~20-40x any real model's inference time observed here)
# can trigger a latency FAIL - not routine, unfaked variance or harness artifacts.
_GENEROUS_LATENCY_POLICY = {"p95_max_increase_percent": 2000.0}
_GENEROUS_RELIABILITY_POLICY = {"max_error_rate_percent": 20.0}

SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario(
        key="baseline",
        description=(
            "Fixed load against v1 only (no canary) - measures raw serving "
            "throughput/latency/error-rate with nothing else in the picture."
        ),
        model_name="benchmark-baseline",
        stable_version="v1",
        canary_version="v2-good",  # present but weight 0 - never actually selected
        canary_weight=0.0,
        policy_config={"minimum_requests": 1000000},  # never triggers an evaluation cycle
        expected_outcome="throughput_only",
        load_duration_seconds=300,
        max_wait_seconds=10,
        notes=("No worker interaction expected or required for this scenario.",),
    ),
    "latency-failure": Scenario(
        key="latency-failure",
        description=(
            "Canary is v2-good with 400ms of injected latency "
            "(model-serving-v2-good-latency-fault). Expects the worker to detect a "
            "p95 latency regression and automatically roll back to stable."
        ),
        model_name="benchmark-latency-failure",
        stable_version="v1",
        canary_version="v2-good-latency-fault",
        canary_weight=0.3,
        policy_config={
            "minimum_requests": 20,
            "evaluation_window_seconds": 20,
            "max_inconclusive_retries": 10,
            "latency": _DEFAULT_LATENCY_POLICY,
            "reliability": _DEFAULT_RELIABILITY_POLICY,
        },
        fault_target="latency",
        fault_latency_ms=400,
        expected_outcome="rollback",
        load_duration_seconds=180,
        max_wait_seconds=180,
    ),
    "error-failure": Scenario(
        key="error-failure",
        description=(
            "Canary is v2-good with a 50% injected error rate "
            "(model-serving-v2-good-error-fault). Expects the worker to detect an "
            "error-rate regression and automatically roll back to stable."
        ),
        model_name="benchmark-error-failure",
        stable_version="v1",
        canary_version="v2-good-error-fault",
        canary_weight=0.3,
        policy_config={
            "minimum_requests": 20,
            "evaluation_window_seconds": 20,
            "max_inconclusive_retries": 10,
            "latency": _DEFAULT_LATENCY_POLICY,
            "reliability": _DEFAULT_RELIABILITY_POLICY,
        },
        fault_target="error",
        fault_error_rate=0.5,
        expected_outcome="rollback",
        load_duration_seconds=180,
        max_wait_seconds=180,
    ),
    "quality-failure": Scenario(
        key="quality-failure",
        description=(
            "Canary is v2-quality-bad (deliberately weak model, no fault injection). "
            "Labels now flow through the real ingestion path (locustfile.py's "
            "_LabelFeeder, delayed by BENCHMARK_LABEL_DELAY_SECONDS), but this "
            "scenario's short window and low max_inconclusive_retries are tuned so "
            "the quality data-sufficiency gate (minimum_labeled_samples/"
            "minimum_label_coverage) typically doesn't mature in time - "
            "demonstrating the max-inconclusive-retries freeze: latency/error "
            "checks PASS (no fault injection), but INCONCLUSIVE beats PASS (Sprint "
            "7), so the deployment never advances traffic and eventually freezes "
            "into INCONCLUSIVE status. If enough labels do mature before the freeze "
            "(a real recall FAIL on this deliberately weak model is also a "
            "plausible, legitimate outcome now that real quality signal exists), "
            "the worker will automatically roll it back instead - both outcomes "
            "demonstrate the policy engine working correctly."
        ),
        model_name="benchmark-quality-failure",
        stable_version="v1",
        canary_version="v2-quality-bad",
        canary_weight=0.3,
        policy_config={
            "minimum_requests": 20,
            "evaluation_window_seconds": 15,
            "max_inconclusive_retries": 3,
            "latency": _GENEROUS_LATENCY_POLICY,
            "reliability": _GENEROUS_RELIABILITY_POLICY,
        },
        expected_outcome="inconclusive_freeze",
        load_duration_seconds=150,
        max_wait_seconds=150,
        notes=(
            "Expected end state is INCONCLUSIVE (frozen after max_inconclusive_retries) "
            "- the short window and low retry budget are tuned so the quality gate "
            "typically hasn't matured enough labeled data yet. A ROLLED_BACK result "
            "instead (a real recall FAIL resolving before the freeze) is also a "
            "correct outcome, not a benchmark bug - see scenario description.",
        ),
        synthetic_disclaimer=(
            "Demo scenario: uses deliberately loosened latency/error thresholds so "
            "harness noise (different models' real inference-time variance) can't "
            "falsely trigger a rollback before the INCONCLUSIVE-freeze mechanism this "
            "scenario is actually testing gets a chance to run. Not representative of "
            "a real deployment's policy thresholds."
        ),
    ),
    "success": Scenario(
        key="success",
        description=(
            "Canary is the real v2-good model, no fault injection. Demonstrates a "
            "full automatic promotion (10% -> 25% -> 50% -> PROMOTED)."
        ),
        model_name="benchmark-success",
        stable_version="v1",
        canary_version="v2-good",
        canary_weight=0.1,
        policy_config={
            "minimum_requests": 20,
            "evaluation_window_seconds": 15,
            "max_inconclusive_retries": 20,
            # Labels land after BENCHMARK_LABEL_DELAY_SECONDS (10s); the default
            # 60s maturity would leave the quality window empty for the first
            # minute of a 15s-step rollout.
            "label_maturity_seconds": 15,
            "minimum_positive_labels": 20,
            "latency": _GENEROUS_LATENCY_POLICY,
            "reliability": _GENEROUS_RELIABILITY_POLICY,
            "quality": {"minimum_recall": 0.7},
            # positive_ratio=0.5 below makes the stream half fraud, a real shift
            # from the ~2% the model was trained on; feature_drift would rightly
            # flag it, so it's off for this scenario.
            "drift": {"max_feature_psi": None},
        },
        expected_outcome="promote",
        # Half fraud rows: a 15s quality window at 10% canary holds ~120 labeled
        # predictions, so ~60 positives against minimum_positive_labels=20. At 30%
        # a passing run cleared it with 20-21, and any late labels froze the
        # rollout at 10%. Recall is TP / positives, so oversampling positives
        # doesn't flatter it.
        positive_ratio=0.5,
        load_duration_seconds=150,
        max_wait_seconds=240,
        notes=(
            "Ground-truth labels flow through the platform's real ingestion "
            "surface (POST /api/labels/batch), delayed and generated by the "
            "Locust load definition itself (locustfile.py's _LabelFeeder) - the "
            "labels' source is still the synthetic dataset's known labels, but "
            "the ingestion path is the same one a real deployment would use. See "
            "docs/DESIGN_NOTES.md's Label ingestion section.",
        ),
        synthetic_disclaimer=(
            "Demo scenario: uses deliberately loosened latency/error thresholds. "
            "Labels arrive delayed through the platform's real ingestion surface "
            "(POST /api/labels), not a direct DB write - but their source is still "
            "the synthetic dataset's known labels, not a real production feedback "
            "loop. Not representative of real platform traffic."
        ),
    ),
}
