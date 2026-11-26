"""The router's gateway layer: what happens to a request before and around the
forward to a model version.

- **Request IDs.** Every response carries `X-Request-ID` (the caller's, or a new
  one), also set on the trace span, so a client report, a trace and a log line
  can be matched up.
- **Sticky routing.** A request with `X-Routing-Key` (a user or session id) goes
  to the same version every time, chosen by weighted rendezvous hashing: each
  version scores the key, the highest score wins, and the scores are skewed so
  each version still gets its configured share. When a rollout ramps the canary
  from 10% to 30%, keys only ever move *to* the canary - nobody bounces back and
  forth between versions mid-rollout.
- **Readiness cache.** A version's `/ready` answer is reused for
  `ready_cache_seconds` instead of probed on every request, which took a second
  upstream round trip per prediction.
- **Circuit breaker per version.** After `breaker_failure_threshold` consecutive
  gateway failures (unreachable, timed out, not ready) the version's breaker
  opens: requests routed to it fail fast with 503 and `Retry-After` for
  `breaker_cooldown_seconds`, then one trial request decides whether it closes.
  Model errors (a 500 from the model itself) never trip it - those are the
  canary analysis' to judge. And it never fails over to another version: a
  broken canary must not hide behind stable's success rate (see
  docs/DESIGN_NOTES.md#traffic-router).
- **Rate limit per model.** A token bucket (`rate_limit_rps`, off by default)
  answers 429 with `Retry-After` once a model is over its budget.
- **Live stats.** A rolling 60-second window per version (requests, statuses,
  latency, in-flight), served at GET /router/gateway for the dashboard's flow
  view.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.router.config import TargetWeight

STATS_WINDOW_SECONDS = 60.0
RATE_WINDOW_SECONDS = 10.0

Clock = Callable[[], float]


# --- sticky routing ------------------------------------------------------------


def _unit_hash(key: str, version: str) -> float:
    """Deterministic, uniform in (0, 1), independent per (key, version)."""
    digest = hashlib.blake2b(f"{key}\x00{version}".encode(), digest_size=8).digest()
    return (int.from_bytes(digest, "big") + 0.5) / 2**64


def sticky_target(targets: Sequence[TargetWeight], key: str) -> TargetWeight:
    """Weighted rendezvous (highest-random-weight) hashing.

    Score = -weight / ln(u): with u uniform, the argmax lands on each target with
    probability weight / sum(weights). Raising one target's weight only raises
    its own score, so the keys that move all move to it."""
    best: TargetWeight | None = None
    best_score = -math.inf
    for target in targets:
        if target.weight <= 0:
            continue
        score = -target.weight / math.log(_unit_hash(key, target.version))
        if score > best_score:
            best, best_score = target, score
    if best is None:  # RouterConfig guarantees a positive total weight
        raise ValueError("no target with positive weight")
    return best


# --- circuit breaker -------------------------------------------------------------


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


BREAKER_STATE_VALUE = {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}


class CircuitBreaker:
    def __init__(self, failure_threshold: int, cooldown_seconds: float, clock: Clock) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False
        self._trial_started = 0.0
        self.opened_total = 0

    @property
    def state(self) -> BreakerState:
        with self._lock:
            return self._current_state()

    def _current_state(self) -> BreakerState:
        if self._state is BreakerState.OPEN and self._clock() - self._opened_at >= self._cooldown:
            self._state = BreakerState.HALF_OPEN
            self._trial_in_flight = False
        return self._state

    def retry_after_seconds(self) -> int:
        with self._lock:
            remaining = self._cooldown - (self._clock() - self._opened_at)
        return max(1, math.ceil(remaining))

    def allow(self) -> bool:
        """Whether a request may go upstream now. In HALF_OPEN only one trial
        request is let through at a time."""
        if self._threshold <= 0:
            return True
        with self._lock:
            state = self._current_state()
            if state is BreakerState.CLOSED:
                return True
            # A trial that never reported back (it died on something other
            # than a gateway failure) mustn't hold the breaker half-open forever.
            trial_lost = self._clock() - self._trial_started >= self._cooldown
            if state is BreakerState.HALF_OPEN and (not self._trial_in_flight or trial_lost):
                self._trial_in_flight = True
                self._trial_started = self._clock()
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = BreakerState.CLOSED
            self._trial_in_flight = False

    def record_failure(self) -> None:
        if self._threshold <= 0:
            return
        with self._lock:
            state = self._current_state()
            self._failures += 1
            if state is BreakerState.HALF_OPEN or self._failures >= self._threshold:
                if self._state is not BreakerState.OPEN:
                    self.opened_total += 1
                self._state = BreakerState.OPEN
                self._opened_at = self._clock()
                self._trial_in_flight = False


# --- rate limit ----------------------------------------------------------------------


class TokenBucket:
    def __init__(self, rate_per_second: float, burst: float, clock: Clock) -> None:
        self._rate = rate_per_second
        self._capacity = max(burst, 1.0)
        self._tokens = self._capacity
        self._clock = clock
        self._updated = clock()
        self._lock = threading.Lock()

    def take(self) -> bool:
        with self._lock:
            now = self._clock()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens >= 1:
                self._tokens -= 1
                return True
            return False

    def retry_after_seconds(self) -> int:
        return max(1, math.ceil(1 / self._rate)) if self._rate > 0 else 1


# --- readiness cache -----------------------------------------------------------------


class ReadinessCache:
    def __init__(self, ttl_seconds: float, clock: Clock) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[float, bool]] = {}

    def get(self, version: str) -> bool | None:
        entry = self._entries.get(version)
        if entry is None or self._clock() - entry[0] >= self._ttl:
            return None
        return entry[1]

    def peek(self, version: str) -> bool | None:
        """Last known answer regardless of age, for display."""
        entry = self._entries.get(version)
        return entry[1] if entry else None

    def put(self, version: str, ready: bool) -> None:
        self._entries[version] = (self._clock(), ready)


# --- rolling stats -------------------------------------------------------------------


OUTCOMES = ("ok", "client_error", "server_error", "gateway_error", "rejected")


@dataclass
class _Sample:
    t: float
    # "ok" (2xx/3xx) | "client_error" (4xx) | "server_error" (5xx from the model)
    # | "gateway_error" (unreachable / not ready) | "rejected" (breaker open)
    outcome: str
    latency_ms: float | None


def outcome_of(status_code: int) -> str:
    if status_code < 400:
        return "ok"
    if status_code < 500:
        return "client_error"
    return "server_error"


def _percentile(sorted_values: list[float], pct: float) -> float:
    k = (len(sorted_values) - 1) * pct / 100
    lo, hi = math.floor(k), math.ceil(k)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


@dataclass
class VersionStats:
    clock: Clock
    samples: deque[_Sample] = field(default_factory=deque)
    in_flight: int = 0
    total: int = 0

    def record(self, outcome: str, latency_ms: float | None) -> None:
        now = self.clock()
        self.samples.append(_Sample(now, outcome, latency_ms))
        self.total += 1
        self._trim(now)

    def _trim(self, now: float) -> None:
        while self.samples and now - self.samples[0].t > STATS_WINDOW_SECONDS:
            self.samples.popleft()

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        self._trim(now)
        samples = list(self.samples)
        recent = [s for s in samples if now - s.t <= RATE_WINDOW_SECONDS]
        outcomes = dict.fromkeys(OUTCOMES, 0)
        for s in samples:
            outcomes[s.outcome] += 1
        latencies = sorted(s.latency_ms for s in samples if s.latency_ms is not None)
        failed = outcomes["server_error"] + outcomes["gateway_error"] + outcomes["rejected"]
        return {
            "requests_per_second": round(len(recent) / RATE_WINDOW_SECONDS, 2),
            "in_flight": self.in_flight,
            "total": self.total,
            "window_seconds": STATS_WINDOW_SECONDS,
            "window_requests": len(samples),
            "outcomes": outcomes,
            "error_rate": round(failed / len(samples), 4) if samples else None,
            "p50_latency_ms": round(_percentile(latencies, 50), 2) if latencies else None,
            "p95_latency_ms": round(_percentile(latencies, 95), 2) if latencies else None,
        }


class GatewayState:
    """Everything the gateway remembers between requests, per model and version."""

    def __init__(
        self,
        *,
        breaker_failure_threshold: int,
        breaker_cooldown_seconds: float,
        ready_cache_seconds: float,
        rate_limit_rps: float,
        rate_limit_burst: float,
        clock: Clock = time.monotonic,
    ) -> None:
        self.clock = clock
        self._breaker_args = (breaker_failure_threshold, breaker_cooldown_seconds)
        self._rate = (rate_limit_rps, rate_limit_burst)
        self.readiness = ReadinessCache(ready_cache_seconds, clock)
        self._breakers: dict[str, CircuitBreaker] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._stats: dict[tuple[str, str], VersionStats] = {}
        self.rate_limited: dict[str, int] = {}
        self.sticky_requests: dict[str, int] = {}

    def breaker(self, version: str) -> CircuitBreaker:
        """Per version, not per model: a version is one serving deployment, and
        when it's down it's down for every model routed to it."""
        breaker = self._breakers.get(version)
        if breaker is None:
            breaker = CircuitBreaker(*self._breaker_args, clock=self.clock)
            self._breakers[version] = breaker
        return breaker

    def admit(self, model_name: str) -> bool:
        rate, burst = self._rate
        if rate <= 0:
            return True
        bucket = self._buckets.get(model_name)
        if bucket is None:
            bucket = TokenBucket(rate, burst or rate, self.clock)
            self._buckets[model_name] = bucket
        if bucket.take():
            return True
        self.rate_limited[model_name] = self.rate_limited.get(model_name, 0) + 1
        return False

    def rate_limit_retry_after(self, model_name: str) -> int:
        bucket = self._buckets.get(model_name)
        return bucket.retry_after_seconds() if bucket else 1

    def stats(self, model_name: str, version: str) -> VersionStats:
        key = (model_name, version)
        stats = self._stats.get(key)
        if stats is None:
            stats = VersionStats(clock=self.clock)
            self._stats[key] = stats
        return stats

    def count_sticky(self, model_name: str) -> None:
        self.sticky_requests[model_name] = self.sticky_requests.get(model_name, 0) + 1

    @property
    def rate_limit(self) -> tuple[float, float]:
        return self._rate
