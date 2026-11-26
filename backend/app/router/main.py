import asyncio
import logging
import random
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from app.observability import tracing
from app.observability.metrics import instrument_app
from app.router import telemetry
from app.router.client import UpstreamUnavailableError, check_ready, forward_predict
from app.router.config import (
    RouterConfig,
    RouterConfigStore,
    RouterSettings,
    TargetWeight,
    router_settings,
)
from app.router.gateway import BREAKER_STATE_VALUE, GatewayState, outcome_of, sticky_target
from app.router.metrics import send_metric

logger = logging.getLogger("model_router")

REQUEST_ID_HEADER = "X-Request-ID"
ROUTING_KEY_HEADER = "X-Routing-Key"


class GatewayError(Exception):
    """A request the gateway answers itself, without a model's response."""

    def __init__(self, status_code: int, detail: str, retry_after: int | None = None) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.retry_after = retry_after


def _drift_inputs(
    payload: dict[str, Any], body: Any, status_code: int
) -> tuple[dict[str, Any] | None, str | None, float | None]:
    """What the drift checks need from one answered prediction: the inputs the
    model scored, which trained artifact scored them (the serving container
    names it in its response; a router version label like
    "v2-good-latency-fault" doesn't), and the score it gave. Only for
    predictions the model actually made - a rejected request's body is a
    data-quality problem, not drift."""
    if status_code >= 400 or not isinstance(body, dict):
        return None, None, None
    model_name, model_version = body.get("model_name"), body.get("model_version")
    if not isinstance(model_name, str) or not isinstance(model_version, str):
        return None, None, None
    score = body.get("fraud_probability")
    features = {
        key: value
        for key, value in payload.items()
        if isinstance(value, (int, float, str)) and not isinstance(value, bool)
    }
    if not isinstance(score, (int, float)) or isinstance(score, bool):
        score = None
    return features, f"{model_name}/{model_version}", None if score is None else float(score)


def choose_target(config: RouterConfig) -> TargetWeight:
    """Weighted random pick across all configured targets."""
    return random.choices(
        config.targets,
        weights=[t.weight for t in config.targets],
        k=1,
    )[0]


async def _fetch_control_plane_allocations(
    client: httpx.AsyncClient, control_plane_url: str, timeout: float
) -> list[RouterConfig]:
    """Best-effort startup sync: every model's authoritative traffic allocation
    from the control plane (GET /api/router-config).

    Not a full sync mechanism (no polling, no push subscription) - just enough to
    avoid booting with a stale default after a restart. Any failure (control plane
    down, bad response) is logged and swallowed; the router keeps its own
    bootstrap config for the default model, and the control plane's reconciler
    pushes the rest on its next tick.
    """
    url = f"{control_plane_url.rstrip('/')}/api/router-config"
    try:
        response = await client.get(url, timeout=timeout)
        response.raise_for_status()
        return [RouterConfig.model_validate(item) for item in response.json()]
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "startup sync from control plane failed (%s), keeping default targets: %s",
            url,
            exc,
        )
        return []


def create_app(
    settings: RouterSettings,
    client: httpx.AsyncClient | None = None,
    tracer_provider: TracerProvider | None = None,
) -> FastAPI:
    """Build the traffic router app.

    `client` is injectable so tests can point the router at in-process fake serving
    apps (via httpx mounts/MockTransport) instead of real sockets. When omitted, a
    real httpx.AsyncClient is created and owned (and closed) by this app's lifespan.

    `tracer_provider` defaults to the one configured from OTEL_* env vars (none
    when tracing is off - see app/observability/tracing.py); tests pass their own.
    """
    provider = tracer_provider or tracing.configured_provider("model-router")
    gateway = GatewayState(
        breaker_failure_threshold=settings.breaker_failure_threshold,
        breaker_cooldown_seconds=settings.breaker_cooldown_seconds,
        ready_cache_seconds=settings.ready_cache_seconds,
        rate_limit_rps=settings.rate_limit_rps,
        rate_limit_burst=settings.rate_limit_burst,
    )
    initial = settings.to_router_config()
    store = RouterConfigStore(initial)
    telemetry.record_split(initial, None)
    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient()
    # Injects traceparent into every outbound call - /ready, /predict and the
    # metric push - so the model service's spans join the router's trace.
    tracing.instrument_httpx(http_client, provider)

    def _apply(config: RouterConfig) -> None:
        previous = store.get(config.model_name)
        store.set(config)
        telemetry.record_split(config, previous)

    # Fire-and-forget metric pushes (see route_predict below). Tracked in a set with a
    # done-callback purely so asyncio keeps a strong reference until each task
    # finishes (otherwise it's eligible for GC mid-flight, which asyncio warns about) -
    # nothing here ever awaits them as part of the request/response cycle.
    pending_metric_tasks: set[asyncio.Task[None]] = set()

    def _emit_metric(
        deployment_id: str,
        model_version: str,
        latency_ms: float,
        status_code: int,
        prediction: Any,
        prediction_id: Any,
        features: dict[str, Any] | None = None,
        artifact: str | None = None,
        score: float | None = None,
    ) -> None:
        if not settings.control_plane_url:
            return
        task = asyncio.create_task(
            send_metric(
                http_client,
                settings.control_plane_url,
                deployment_id,
                model_version,
                latency_ms,
                status_code,
                prediction if isinstance(prediction, int) else None,
                prediction_id if isinstance(prediction_id, str) else None,
                settings.upstream_timeout_seconds,
                features,
                artifact,
                score,
            )
        )
        pending_metric_tasks.add(task)
        task.add_done_callback(pending_metric_tasks.discard)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.control_plane_url:
            for synced in await _fetch_control_plane_allocations(
                http_client, settings.control_plane_url, settings.upstream_timeout_seconds
            ):
                _apply(synced)
                logger.info("synced traffic allocation from control plane: %s", synced)
        yield
        if owns_client:
            await http_client.aclose()

    app = FastAPI(title="ModelOps Traffic Router", lifespan=lifespan)
    instrument_app(app, "router")
    tracing.instrument_fastapi(app, provider)
    app.state.pending_metric_tasks = pending_metric_tasks  # test introspection only
    app.state.gateway = gateway  # test introspection only

    def _resolve(version: str) -> str:
        base_url = settings.resolve_base_url(version)
        if base_url is None:
            raise HTTPException(
                status_code=400,
                detail=f"No host:port mapping for version '{version}' "
                "(router doesn't know where it runs)",
            )
        return base_url

    def _config_for(model_name: str) -> RouterConfig:
        config = store.get(model_name)
        if config is None:
            raise HTTPException(
                status_code=404,
                detail=f"Router has no traffic split for model '{model_name}'",
            )
        return config

    @app.get("/router/config")
    def get_config(model_name: str | None = None) -> RouterConfig:
        """One model's split; the default model when model_name is omitted."""
        return _config_for(model_name or settings.model_name)

    @app.get("/router/configs")
    def get_configs() -> list[RouterConfig]:
        """Every model's split - what the control plane reconciles against."""
        return store.all()

    @app.put("/router/config")
    def put_config(config: RouterConfig) -> RouterConfig:
        unknown = [
            t.version for t in config.targets if settings.resolve_base_url(t.version) is None
        ]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"No host:port mapping for version(s): {unknown}",
            )

        # Revisions are a monotonic routing generation scoped to the model (see
        # TrafficAllocation.revision and
        # docs/DESIGN_NOTES.md#desired-observed-reconciliation): a delayed push
        # from an old, already-terminal deployment must be rejected if a newer
        # one already landed a higher generation for the same model. Each model
        # has its own slot, so a push for another model is judged only against
        # that model's history, and never displaces this one.
        current = store.get(config.model_name)
        if current is not None and config.revision <= current.revision:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"stale revision {config.revision} for model "
                    f"{config.model_name} - router already has revision "
                    f"{current.revision} applied"
                ),
            )

        _apply(config)
        logger.info(
            "router config updated: model=%s deployment=%s revision=%s targets=%s",
            config.model_name,
            config.deployment_id,
            config.revision,
            [(t.version, t.weight) for t in config.targets],
        )
        return config

    @app.get("/router/health")
    async def router_health() -> dict[str, Any]:
        timeout = settings.upstream_timeout_seconds
        readiness: dict[str, bool] = {}
        models = []
        for config in store.all():
            target_health = []
            for target in config.targets:
                if target.version not in readiness:
                    base_url = settings.resolve_base_url(target.version)
                    readiness[target.version] = base_url is not None and await check_ready(
                        http_client, base_url, timeout
                    )
                target_health.append(
                    {
                        "version": target.version,
                        "weight": target.weight,
                        "ready": readiness[target.version],
                    }
                )
            models.append({"model_name": config.model_name, "targets": target_health})
        default = next((m for m in models if m["model_name"] == settings.model_name), None)
        return {
            "status": "ok",
            # Default model's view, kept at the top level for existing callers.
            "model_name": settings.model_name,
            "targets": default["targets"] if default else [],
            "models": models,
        }

    async def _is_ready(version: str, base_url: str) -> bool:
        cached = gateway.readiness.get(version)
        if cached is not None:
            return cached
        ready = await check_ready(http_client, base_url, settings.upstream_timeout_seconds)
        gateway.readiness.put(version, ready)
        return ready

    def _unanswered(
        model_name: str,
        config: RouterConfig,
        version: str,
        outcome: str,
        started: float,
        *,
        trip_breaker: bool,
    ) -> None:
        """A request the picked version never answered. Counted against that
        version and reported to the control plane as a 503 (with the time the
        client waited for it), so a canary that's down shows up in its error
        rate instead of just going quiet."""
        breaker = gateway.breaker(version)
        if trip_breaker:
            breaker.record_failure()
        telemetry.BREAKER_STATE.labels(version).set(BREAKER_STATE_VALUE[breaker.state])
        telemetry.ROUTED_REQUESTS.labels(model_name, version, "503").inc()
        waited_ms = (time.perf_counter() - started) * 1000
        gateway.stats(model_name, version).record(outcome, None)
        if config.deployment_id:
            _emit_metric(config.deployment_id, version, waited_ms, 503, None, None)

    async def _route(
        model_name: str, payload: dict[str, Any], routing_key: str | None
    ) -> tuple[JSONResponse, str]:
        started = time.perf_counter()
        config = _config_for(model_name)
        if not gateway.admit(model_name):
            telemetry.RATE_LIMITED.labels(model_name).inc()
            raise GatewayError(
                429,
                f"Rate limit for model '{model_name}' exceeded",
                gateway.rate_limit_retry_after(model_name),
            )
        if routing_key:
            target = sticky_target(config.targets, f"{config.deployment_id}:{routing_key}")
            gateway.count_sticky(model_name)
        else:
            target = choose_target(config)
        version = target.version
        base_url = _resolve(version)
        trace.get_current_span().set_attributes(
            {
                "modelops.model": model_name,
                "modelops.version": version,
                "modelops.deployment_id": config.deployment_id or "",
                "modelops.config_revision": config.revision,
                "modelops.sticky": routing_key is not None,
            }
        )

        breaker = gateway.breaker(version)
        if not breaker.allow():
            # Fail fast rather than queue more requests on a version that keeps
            # failing - and never fail over: see app/router/gateway.py.
            _unanswered(model_name, config, version, "rejected", started, trip_breaker=False)
            raise GatewayError(
                503,
                f"Target '{version}' circuit breaker is open",
                breaker.retry_after_seconds(),
            )

        if not await _is_ready(version, base_url):
            logger.warning(
                "routing refused: target %s (%s) is not ready - rejecting request, "
                "not rerouting to another target",
                version,
                base_url,
            )
            _unanswered(model_name, config, version, "gateway_error", started, trip_breaker=True)
            raise GatewayError(503, f"Target '{version}' is not ready")

        stats = gateway.stats(model_name, version)
        stats.in_flight += 1
        start = time.perf_counter()
        try:
            response = await forward_predict(
                http_client, version, base_url, payload, settings.upstream_timeout_seconds
            )
        except UpstreamUnavailableError as exc:
            logger.warning("routing failed: %s", exc)
            # Don't trust a cached "ready" for a version that just refused a connection.
            gateway.readiness.put(version, False)
            _unanswered(model_name, config, version, "gateway_error", started, trip_breaker=True)
            raise GatewayError(503, str(exc)) from exc
        finally:
            stats.in_flight -= 1
        elapsed = time.perf_counter() - start
        latency_ms = elapsed * 1000
        breaker.record_success()
        telemetry.BREAKER_STATE.labels(version).set(BREAKER_STATE_VALUE[breaker.state])
        telemetry.UPSTREAM_LATENCY.labels(model_name, version).observe(elapsed)
        telemetry.ROUTED_REQUESTS.labels(model_name, version, str(response.status_code)).inc()
        stats.record(outcome_of(response.status_code), latency_ms)

        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text}

        if isinstance(body, dict):
            body.setdefault("routed_to", version)

        if config.deployment_id:
            prediction = body.get("prediction") if isinstance(body, dict) else None
            prediction_id = body.get("prediction_id") if isinstance(body, dict) else None
            _emit_metric(
                config.deployment_id,
                version,
                latency_ms,
                response.status_code,
                prediction,
                prediction_id,
                *_drift_inputs(payload, body, response.status_code),
            )

        return JSONResponse(status_code=response.status_code, content=body), version

    async def _handle(model_name: str, payload: dict[str, Any], request: Request) -> JSONResponse:
        request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex
        trace.get_current_span().set_attribute("modelops.request_id", request_id)
        headers = {REQUEST_ID_HEADER: request_id}
        try:
            response, version = await _route(
                model_name, payload, request.headers.get(ROUTING_KEY_HEADER)
            )
        except GatewayError as exc:
            if exc.retry_after is not None:
                headers["Retry-After"] = str(exc.retry_after)
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail, "request_id": request_id},
                headers=headers,
            )
        response.headers.update({**headers, "X-Routed-To": version})
        return response

    @app.post("/router/predict")
    async def route_predict(payload: dict[str, Any], request: Request) -> JSONResponse:
        """Routes for the default model (ROUTER_MODEL_NAME)."""
        return await _handle(settings.model_name, payload, request)

    @app.post("/router/models/{model_name}/predict")
    async def route_model_predict(
        model_name: str, payload: dict[str, Any], request: Request
    ) -> JSONResponse:
        return await _handle(model_name, payload, request)

    @app.get("/router/gateway")
    def gateway_stats() -> dict[str, Any]:
        """Live view of the gateway: per model, each version's weight, breaker,
        readiness and a rolling 60-second window of traffic. What the
        dashboard's flow view animates from."""
        rate, burst = gateway.rate_limit
        models = []
        for config in store.all():
            total_weight = sum(t.weight for t in config.targets) or 1.0
            versions = []
            for target in config.targets:
                breaker = gateway.breaker(target.version)
                versions.append(
                    {
                        "version": target.version,
                        "weight": target.weight / total_weight,
                        "breaker": breaker.state.value,
                        "breaker_opened_total": breaker.opened_total,
                        "ready": gateway.readiness.peek(target.version),
                        **gateway.stats(config.model_name, target.version).snapshot(),
                    }
                )
            models.append(
                {
                    "model_name": config.model_name,
                    "deployment_id": config.deployment_id,
                    "revision": config.revision,
                    "rate_limited_total": gateway.rate_limited.get(config.model_name, 0),
                    "sticky_requests_total": gateway.sticky_requests.get(config.model_name, 0),
                    "versions": versions,
                }
            )
        return {
            "policy": {
                "breaker_failure_threshold": settings.breaker_failure_threshold,
                "breaker_cooldown_seconds": settings.breaker_cooldown_seconds,
                "ready_cache_seconds": settings.ready_cache_seconds,
                "rate_limit_rps": rate,
                "rate_limit_burst": burst or rate,
                "upstream_timeout_seconds": settings.upstream_timeout_seconds,
            },
            "models": models,
        }

    return app


app = create_app(router_settings)
