"""The router's gateway layer: request IDs, sticky routing, readiness cache,
circuit breaker, rate limit and live stats (app/router/gateway.py)."""

# ruff: noqa: F811 - pytest fixtures imported from test_router
import json
import time
from collections import Counter
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from app.router.config import RouterSettings, TargetWeight
from app.router.gateway import BreakerState, CircuitBreaker, TokenBucket, sticky_target
from app.router.main import create_app as create_router_app
from app.serving.config import ServingSettings
from app.serving.main import create_app as create_serving_app
from tests.test_router import (
    CANARY_HOST_PORT,
    CONTROL_PLANE_URL,
    FULL_FEATURES,
    STABLE_HOST_PORT,
    CountingASGITransport,
    RecordingTransport,
    _sample_payload,
    _serving_app,
    artifacts_dir,  # noqa: F401
    unreachable_transport,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _settings(
    stable_weight: float = 0.5, canary_weight: float = 0.5, **overrides: object
) -> RouterSettings:
    return RouterSettings(
        version_hosts={"v1": STABLE_HOST_PORT, "v2-good": CANARY_HOST_PORT},
        initial_targets=[
            TargetWeight(version="v1", weight=stable_weight),
            TargetWeight(version="v2-good", weight=canary_weight),
        ],
        **overrides,  # type: ignore[arg-type]
    )


def _client(
    stable: httpx.AsyncBaseTransport,
    canary: httpx.AsyncBaseTransport,
    settings: RouterSettings,
    control_plane: httpx.AsyncBaseTransport | None = None,
) -> TestClient:
    mounts = {f"http://{STABLE_HOST_PORT}": stable, f"http://{CANARY_HOST_PORT}": canary}
    if control_plane is not None:
        mounts[CONTROL_PLANE_URL] = control_plane
    return TestClient(create_router_app(settings, client=httpx.AsyncClient(mounts=mounts)))


# --- sticky routing ------------------------------------------------------------


def test_sticky_hashing_splits_keys_by_weight() -> None:
    targets = [TargetWeight(version="v1", weight=0.7), TargetWeight(version="v2", weight=0.3)]
    counts = Counter(sticky_target(targets, f"user-{i}").version for i in range(20000))
    assert abs(counts["v2"] / 20000 - 0.3) < 0.015


def test_ramping_the_canary_only_moves_keys_onto_it() -> None:
    keys = [f"user-{i}" for i in range(5000)]
    at_10 = [TargetWeight(version="v1", weight=0.9), TargetWeight(version="v2", weight=0.1)]
    at_30 = [TargetWeight(version="v1", weight=0.7), TargetWeight(version="v2", weight=0.3)]
    before = {k: sticky_target(at_10, k).version for k in keys}
    after = {k: sticky_target(at_30, k).version for k in keys}
    # Nobody on the canary at 10% is sent back to stable at 30%.
    assert all(after[k] == "v2" for k in keys if before[k] == "v2")
    assert sum(v == "v2" for v in after.values()) > sum(v == "v2" for v in before.values())


def test_zero_weight_versions_never_get_sticky_traffic() -> None:
    targets = [TargetWeight(version="v1", weight=1.0), TargetWeight(version="v2", weight=0.0)]
    assert {sticky_target(targets, f"k{i}").version for i in range(500)} == {"v1"}


def test_same_routing_key_always_reaches_the_same_version(artifacts_dir: Path) -> None:
    client = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v2-good")),
        _settings(),
    )
    payload = _sample_payload(FULL_FEATURES)
    for key in ("alice", "bob", "carol", "dave"):
        seen = {
            client.post("/router/predict", json=payload, headers={"X-Routing-Key": key}).headers[
                "X-Routed-To"
            ]
            for _ in range(8)
        }
        assert len(seen) == 1
    stats = client.get("/router/gateway").json()
    assert stats["models"][0]["sticky_requests_total"] == 32


# --- request ids ---------------------------------------------------------------


def test_request_id_is_echoed_or_minted(artifacts_dir: Path) -> None:
    client = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v2-good")),
        _settings(),
    )
    payload = _sample_payload(FULL_FEATURES)
    echoed = client.post("/router/predict", json=payload, headers={"X-Request-ID": "req-42"})
    minted = client.post("/router/predict", json=payload)
    assert echoed.headers["X-Request-ID"] == "req-42"
    assert len(minted.headers["X-Request-ID"]) == 32


def test_gateway_errors_carry_the_request_id(artifacts_dir: Path) -> None:
    client = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        unreachable_transport(),
        _settings(0.0, 1.0),
    )
    response = client.post(
        "/router/predict", json=_sample_payload(FULL_FEATURES), headers={"X-Request-ID": "r-1"}
    )
    assert response.status_code == 503
    assert response.json()["request_id"] == "r-1"
    assert response.headers["X-Request-ID"] == "r-1"


# --- readiness cache -----------------------------------------------------------


def test_readiness_is_probed_once_per_ttl_not_per_request(artifacts_dir: Path) -> None:
    canary = CountingASGITransport(app=_serving_app(artifacts_dir, "v2-good"))
    cached = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        canary,
        _settings(0.0, 1.0, ready_cache_seconds=60),
    )
    for _ in range(5):
        assert (
            cached.post("/router/predict", json=_sample_payload(FULL_FEATURES)).status_code == 200
        )
    assert canary.call_count == 1 + 5  # one /ready, five /predict

    uncached_canary = CountingASGITransport(app=_serving_app(artifacts_dir, "v2-good"))
    uncached = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        uncached_canary,
        _settings(0.0, 1.0, ready_cache_seconds=0),
    )
    for _ in range(5):
        uncached.post("/router/predict", json=_sample_payload(FULL_FEATURES))
    assert uncached_canary.call_count == 10


# --- circuit breaker -----------------------------------------------------------


def test_breaker_opens_after_consecutive_gateway_failures_and_fails_fast(
    artifacts_dir: Path,
) -> None:
    stable = CountingASGITransport(app=_serving_app(artifacts_dir, "v1"))
    calls = {"n": 0}

    def refuse(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("refused", request=request)

    client = _client(
        stable,
        httpx.MockTransport(refuse),
        _settings(0.0, 1.0, breaker_failure_threshold=3, ready_cache_seconds=0),
    )
    payload = _sample_payload(FULL_FEATURES)
    for _ in range(3):
        assert client.post("/router/predict", json=payload).status_code == 503
    upstream_calls = calls["n"]

    fast = client.post("/router/predict", json=payload)
    assert fast.status_code == 503
    assert "circuit breaker is open" in fast.json()["detail"]
    assert int(fast.headers["Retry-After"]) >= 1
    assert calls["n"] == upstream_calls  # never reached the broken version
    assert stable.call_count == 0  # and never failed over to stable

    view = client.get("/router/gateway").json()["models"][0]["versions"]
    canary = next(v for v in view if v["version"] == "v2-good")
    assert canary["breaker"] == "open"
    assert canary["outcomes"]["gateway_error"] == 3
    assert canary["outcomes"]["rejected"] == 1


def test_model_errors_do_not_trip_the_breaker(artifacts_dir: Path) -> None:
    failing = create_serving_app(
        ServingSettings(
            model_name="fraud-model",
            model_version="v2-good",
            artifacts_dir=artifacts_dir,
            injected_error_rate=1.0,
        )
    )
    client = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        httpx.ASGITransport(app=failing),
        _settings(0.0, 1.0, breaker_failure_threshold=2),
    )
    statuses = [
        client.post("/router/predict", json=_sample_payload(FULL_FEATURES)).status_code
        for _ in range(6)
    ]
    # Every request reached the model and got its 500: a bad model is the canary
    # analysis' call, not the gateway's.
    assert statuses == [500] * 6
    view = client.get("/router/gateway").json()["models"][0]["versions"]
    assert next(v for v in view if v["version"] == "v2-good")["breaker"] == "closed"


def test_breaker_half_opens_after_cooldown_and_closes_on_success() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=5, clock=clock)
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN and not breaker.allow()

    clock.now += 5
    assert breaker.state is BreakerState.HALF_OPEN
    assert breaker.allow()  # the one trial request
    assert not breaker.allow()  # nobody else while it's in flight
    breaker.record_success()
    assert breaker.state is BreakerState.CLOSED and breaker.allow()


def test_a_trial_that_never_reports_back_is_retried_after_a_cooldown() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=clock)
    breaker.record_failure()
    clock.now += 5
    assert breaker.allow()  # trial goes out and is never heard from again
    clock.now += 1
    assert not breaker.allow()
    clock.now += 5
    assert breaker.allow()


def test_a_failed_trial_reopens_the_breaker() -> None:
    clock = FakeClock()
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=5, clock=clock)
    breaker.record_failure()
    clock.now += 5
    assert breaker.allow()
    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert breaker.retry_after_seconds() == 5
    assert breaker.opened_total == 2


def test_gateway_failures_reach_the_control_plane_as_503s(artifacts_dir: Path) -> None:
    recorder = RecordingTransport()
    client = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        unreachable_transport(),
        _settings(0.0, 1.0, control_plane_url=CONTROL_PLANE_URL),
        control_plane=recorder,
    )
    config = client.get("/router/config").json()
    client.put("/router/config", json={**config, "deployment_id": "dep-9", "revision": 1})

    assert client.post("/router/predict", json=_sample_payload(FULL_FEATURES)).status_code == 503
    for _ in range(50):
        if recorder.calls:
            break
        time.sleep(0.02)
    sent = json.loads(recorder.calls[0].content)
    # A canary that can't be reached must count against its error rate, not
    # just vanish from the canary analysis.
    assert sent["model_version"] == "v2-good"
    assert sent["status_code"] == 503


# --- rate limit ------------------------------------------------------------------


def test_rate_limit_answers_429_with_retry_after(artifacts_dir: Path) -> None:
    client = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v2-good")),
        _settings(rate_limit_rps=0.5, rate_limit_burst=2),
    )
    payload = _sample_payload(FULL_FEATURES)
    statuses = [client.post("/router/predict", json=payload).status_code for _ in range(3)]
    assert statuses == [200, 200, 429]
    limited = client.post("/router/predict", json=payload)
    assert limited.headers["Retry-After"] == "2"
    assert client.get("/router/gateway").json()["models"][0]["rate_limited_total"] == 2


def test_token_bucket_refills_over_time() -> None:
    clock = FakeClock()
    bucket = TokenBucket(rate_per_second=2, burst=1, clock=clock)
    assert bucket.take() and not bucket.take()
    clock.now += 0.5
    assert bucket.take()


# --- live stats ------------------------------------------------------------------


def test_gateway_view_reports_split_and_recent_traffic(artifacts_dir: Path) -> None:
    client = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v2-good")),
        _settings(3.0, 1.0),
    )
    for _ in range(12):
        client.post("/router/predict", json=_sample_payload(FULL_FEATURES))
    view = client.get("/router/gateway").json()
    model = view["models"][0]
    weights = {v["version"]: v["weight"] for v in model["versions"]}
    assert weights == {"v1": 0.75, "v2-good": 0.25}  # normalised
    assert sum(v["window_requests"] for v in model["versions"]) == 12
    assert all(v["breaker"] == "closed" for v in model["versions"])
    busy = max(model["versions"], key=lambda v: v["window_requests"])
    assert busy["p95_latency_ms"] is not None and busy["requests_per_second"] > 0
    assert view["policy"]["breaker_failure_threshold"] == 5


# --- control plane passthrough ------------------------------------------------------


def test_control_plane_passes_the_gateway_view_through(artifacts_dir: Path) -> None:
    from app.control_plane.router_gateway import RouterGateway, get_router_gateway
    from app.main import app as control_plane

    router_client = _client(
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        httpx.ASGITransport(app=_serving_app(artifacts_dir, "v2-good")),
        _settings(),
    )
    for _ in range(4):
        router_client.post("/router/predict", json=_sample_payload(FULL_FEATURES))
    live_view = router_client.get("/router/gateway").json()

    class Up(RouterGateway):
        def __init__(self) -> None:
            pass

        async def get_gateway_stats(self) -> dict[str, object] | None:
            return live_view

    class Down(Up):
        async def get_gateway_stats(self) -> dict[str, object] | None:
            return None

    try:
        with TestClient(control_plane) as client:
            control_plane.dependency_overrides[get_router_gateway] = Up
            body = client.get("/api/router/gateway").json()
            assert body["reachable"] is True
            [model] = body["models"]
            assert sum(v["window_requests"] for v in model["versions"]) == 4
            assert body["policy"]["ready_cache_seconds"] == 1.0

            control_plane.dependency_overrides[get_router_gateway] = Down
            assert client.get("/api/router/gateway").json() == {
                "reachable": False,
                "policy": None,
                "models": [],
            }
    finally:
        control_plane.dependency_overrides.pop(get_router_gateway, None)
