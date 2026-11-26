"""The canary traffic ramp an automated rollout climbs: 10% -> 25% -> 50%, then
promote. Shared by the control plane (which steps the split) and the worker
(which decides whether a PASS advances or promotes), so it lives on its own,
free of either side's dependencies.

The ramp stops short of 100% on purpose. At 100% the stable version gets no
real traffic, so every check that compares canary with stable compares a
version carrying the whole load with an idle one: a healthy canary queueing
under 100 req/s on one container measured p95 ~370 ms against ~12 ms of probes
on stable, and the latency check rolled it back. A PASS at the last stage, with
real traffic on both sides, is the last evidence that means anything; promoting
is what moves the canary to 100%. Flagger and Argo Rollouts end their analysis
the same way, at a max weight below 100%.
"""

TRAFFIC_STAGES: tuple[float, ...] = (0.10, 0.25, 0.50)
FINAL_STAGE = TRAFFIC_STAGES[-1]


def at_final_stage(canary_weight: float) -> bool:
    """True once the canary is at the last analysis stage, or past it (a
    deployment created with a larger canary_weight): a PASS here promotes."""
    return canary_weight >= FINAL_STAGE - 1e-9
