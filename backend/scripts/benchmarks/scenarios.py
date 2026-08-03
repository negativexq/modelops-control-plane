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
    # "success" only: since there is no real actual_label source anywhere in the
    # platform yet (see Sprint 5/7), the quality/recall policy check is always
    # INCONCLUSIVE and therefore no deployment can be automatically promoted or even
    # advanced past its first traffic stage - INCONCLUSIVE beats PASS by design
    # (Sprint 7). To still demonstrate the worker's real ramp/promote mechanics, this
    # scenario backfills synthetic ground-truth labels via POST .../metrics after the
    # load phase - simulating a delayed label feed that doesn't exist as a real
    # system component. This is called out explicitly in the report, not hidden.
    backfill_synthetic_labels: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)
    # User-facing disclaimer for the dashboard's scenario cards (app/benchmarks/) -
    # None for scenarios that reflect real, unmodified platform behavior
    # (baseline/latency-failure/error-failure). Set for quality-failure (generous,
    # non-default policy thresholds so harness noise doesn't false-trigger a latency
    # FAIL) and success (generous thresholds PLUS synthetic actual_label backfill,
    # since promotion is otherwise unreachable - see backfill_synthetic_labels
    # above). The dashboard must show this, not just this script's own report.
    synthetic_disclaimer: str | None = None


_DEFAULT_LATENCY_POLICY = {"p95_max_increase_percent": 20.0}
_DEFAULT_RELIABILITY_POLICY = {"max_error_rate_percent": 5.0}

# For scenarios with NO injected fault (quality-failure, success): different model
# architectures genuinely have different real inference latency, and a shared,
# resource-constrained local Docker host adds its own noise on top - confirmed via
# manual end-to-end verification twice: first, v2-quality-bad's real (non-injected)
# latency variance alone was enough to trigger a latency FAIL at a 20% threshold;
# second, even at 300%, "success" still tripped a false latency FAIL right at the
# moment load generation stopped and the canary was at 100% traffic - stable's
# window held only low-latency synthetic health-check backfill (see
# run_benchmark.py's _backfill_synthetic_labels) while canary's window still held
# a residue of real, CPU-contended request latency, an apples-to-oranges comparison
# that's a transient artifact of this benchmark harness, not a real regression.
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
            "The 'quality regression' this demonstrates is NOT a recall policy FAIL - "
            "there is no actual_label data anywhere in the platform, so the recall "
            "check is always INCONCLUSIVE, never FAIL. What this scenario actually "
            "shows is the max-inconclusive-retries freeze: latency/error checks PASS "
            "(no fault injection), but the perpetually-INCONCLUSIVE recall check "
            "keeps the overall result INCONCLUSIVE (INCONCLUSIVE beats PASS, Sprint "
            "7), so the deployment never advances traffic and eventually freezes "
            "into INCONCLUSIVE status once max_inconclusive_retries is exceeded."
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
            "Expected end state is INCONCLUSIVE (frozen after max_inconclusive_retries), "
            "not ROLLED_BACK - there is no FAIL involved here, only unresolvable "
            "INCONCLUSIVE recall checks. See scenario description.",
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
            "full automatic promotion (10% -> 25% -> 50% -> 100% -> PROMOTED)."
        ),
        model_name="benchmark-success",
        stable_version="v1",
        canary_version="v2-good",
        canary_weight=0.1,
        policy_config={
            "minimum_requests": 20,
            "evaluation_window_seconds": 15,
            "max_inconclusive_retries": 20,
            "latency": _GENEROUS_LATENCY_POLICY,
            "reliability": _GENEROUS_RELIABILITY_POLICY,
            "quality": {"minimum_recall": 0.7},
        },
        expected_outcome="promote",
        load_duration_seconds=90,
        max_wait_seconds=240,
        backfill_synthetic_labels=True,
        notes=(
            "Without a real actual_label source, the recall check is always "
            "INCONCLUSIVE and no deployment can ever be promoted automatically - see "
            "quality-failure. This scenario backfills synthetic (prediction, "
            "actual_label) pairs via POST .../metrics throughout the run to "
            "simulate a delayed ground-truth label feed, purely so the promotion "
            "path can be exercised end to end. This is NOT real platform behavior "
            "today; it demonstrates what happens once a real label source exists.",
            "Also found via manual end-to-end verification: once the canary reaches "
            "100% traffic, the stable side receives zero real requests, so "
            "minimum_requests (which requires BOTH sides to have enough samples) "
            "would stay unmet forever and PROMOTE would never fire - a genuine "
            "platform limitation, not a benchmark bug. This scenario also backfills "
            "small plain 'health-check' metrics for the stable version so this "
            "specific benchmark can still reach PROMOTED; a real deployment at 100% "
            "canary would need an equivalent synthetic-monitoring traffic source of "
            "its own to ever complete promotion automatically.",
        ),
        synthetic_disclaimer=(
            "Demo scenario: uses deliberately loosened latency/error thresholds AND "
            "backfills synthetic (prediction, actual_label) pairs via POST "
            "/metrics to simulate ground-truth labels that don't exist as a real "
            "system component yet. Without this, no deployment can be automatically "
            "promoted today - see the quality-failure scenario. Not representative "
            "of real platform behavior."
        ),
    ),
}
