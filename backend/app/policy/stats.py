"""The statistics behind the policy checks.

A canary at 30% of traffic over a 20-second window may have served 25 requests.
Two of them failing is an 8% error rate, over a 5% limit, and also exactly what a
healthy 2% model produces by chance often enough to roll back good releases. So
the engine doesn't compare point estimates to limits; it asks how sure it can be
that the limit is breached:

- error rate: Wilson score interval for the canary's failure proportion, plus an
  exact one-sided binomial test of "the true rate is above the limit";
- p95 latency: a bootstrap confidence interval for the canary-vs-stable p95
  increase, plus a one-sided Mann-Whitney U test of "canary latencies are
  stochastically larger than stable's scaled up by the allowed increase";
- recall: Wilson interval for TP / positives, plus an exact one-sided binomial
  test of "the true recall is below the minimum".

Every function here is pure and deterministic (the bootstrap uses a fixed seed),
so re-running an evaluation on the same rows gives the same interval.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import stats

# The bootstrap is O(resamples * n). Past these sizes the interval is already
# narrow and extra rows only cost time.
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_MAX_SAMPLES = 5000
_SEED = 20260301


@dataclass(frozen=True)
class Interval:
    low: float
    high: float


def _z(confidence: float) -> float:
    return float(stats.norm.ppf(1 - (1 - confidence) / 2))


def wilson_interval(successes: int, trials: int, confidence: float) -> Interval | None:
    """Two-sided Wilson score interval for a binomial proportion. Unlike the
    normal ("Wald") interval it stays inside [0, 1] and behaves at 0 or n
    successes, which is the usual case for a canary's error count."""
    if trials <= 0:
        return None
    z = _z(confidence)
    p = successes / trials
    denominator = 1 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denominator
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denominator
    return Interval(max(0.0, centre - margin), min(1.0, centre + margin))


def binomial_p_above(successes: int, trials: int, limit: float) -> float | None:
    """One-sided exact p-value for H1: true proportion > `limit`."""
    if trials <= 0:
        return None
    if limit >= 1:
        return 1.0
    return float(stats.binomtest(successes, trials, p=limit, alternative="greater").pvalue)


def binomial_p_below(successes: int, trials: int, limit: float) -> float | None:
    """One-sided exact p-value for H1: true proportion < `limit`."""
    if trials <= 0:
        return None
    if limit <= 0:
        return 1.0
    return float(stats.binomtest(successes, trials, p=limit, alternative="less").pvalue)


def _cap(values: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    if values.size <= BOOTSTRAP_MAX_SAMPLES:
        return values
    return rng.choice(values, size=BOOTSTRAP_MAX_SAMPLES, replace=False)


def p95_increase_interval(
    stable: list[float], canary: list[float], confidence: float
) -> Interval | None:
    """Percentile-bootstrap interval for (canary p95 / stable p95 - 1) * 100.

    Each resample draws both sides with replacement and takes the ratio of their
    95th percentiles, so the interval carries the noise of both estimates. A
    resample whose stable p95 is 0 is dropped (the ratio is undefined)."""
    if len(stable) < 2 or len(canary) < 2:
        return None
    rng = np.random.default_rng(_SEED)
    s = _cap(np.asarray(stable, dtype=float), rng)
    c = _cap(np.asarray(canary, dtype=float), rng)
    s_p95 = np.percentile(rng.choice(s, size=(BOOTSTRAP_RESAMPLES, s.size)), 95, axis=1)
    c_p95 = np.percentile(rng.choice(c, size=(BOOTSTRAP_RESAMPLES, c.size)), 95, axis=1)
    valid = s_p95 > 0
    if not valid.any():
        return None
    increase = (c_p95[valid] / s_p95[valid] - 1) * 100
    alpha = 1 - confidence
    low, high = np.percentile(increase, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return Interval(float(low), float(high))


def latency_shift_p_value(
    stable: list[float], canary: list[float], max_increase_percent: float
) -> float | None:
    """One-sided Mann-Whitney U p-value for H1: canary latencies are
    stochastically larger than stable latencies scaled by the allowed increase.

    Scaling stable first makes the test ask the policy's own question ("slower
    by more than the limit?") instead of "slower at all?", which with enough
    traffic would be significant for a 1 ms difference."""
    if len(stable) < 2 or len(canary) < 2:
        return None
    scaled = np.asarray(stable, dtype=float) * (1 + max_increase_percent / 100)
    result = stats.mannwhitneyu(np.asarray(canary, dtype=float), scaled, alternative="greater")
    return float(result.pvalue)
