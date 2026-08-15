import asyncio
import logging
import random
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.router.client import UpstreamUnavailableError, check_ready, forward_predict
from app.router.config import (
    RouterConfig,
    RouterConfigStore,
    RouterSettings,
    TargetWeight,
    router_settings,
)
from app.router.metrics import send_metric

logger = logging.getLogger("model_router")


def choose_target(config: RouterConfig) -> TargetWeight:
    """Weighted random pick across all configured targets."""
    return random.choices(
        config.targets,
        weights=[t.weight for t in config.targets],
        k=1,
    )[0]


async def _fetch_control_plane_allocation(
    client: httpx.AsyncClient, control_plane_url: str, model_name: str, timeout: float
) -> RouterConfig | None:
    """Best-effort startup sync from the control plane's current traffic allocation.

    Not a full sync mechanism (no polling, no push subscription) - just enough to
    avoid booting with a stale default config after a restart. Any failure (control
    plane down, no active deployment yet, bad response) is logged and swallowed; the
    router falls back to its own configured initial_targets.
    """
    url = f"{control_plane_url.rstrip('/')}/api/router-config/{model_name}"
    try:
        response = await client.get(url, timeout=timeout)
        response.raise_for_status()
        return RouterConfig.model_validate(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning(
            "startup sync from control plane failed (%s), keeping default targets: %s",
            url,
            exc,
        )
        return None


def create_app(settings: RouterSettings, client: httpx.AsyncClient | None = None) -> FastAPI:
    """Build the traffic router app.

    `client` is injectable so tests can point the router at in-process fake serving
    apps (via httpx mounts/MockTransport) instead of real sockets. When omitted, a
    real httpx.AsyncClient is created and owned (and closed) by this app's lifespan.
    """
    store = RouterConfigStore(settings.to_router_config())
    owns_client = client is None
    http_client = client if client is not None else httpx.AsyncClient()

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
            )
        )
        pending_metric_tasks.add(task)
        task.add_done_callback(pending_metric_tasks.discard)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.control_plane_url:
            synced = await _fetch_control_plane_allocation(
                http_client,
                settings.control_plane_url,
                settings.model_name,
                settings.upstream_timeout_seconds,
            )
            if synced is not None:
                store.set(synced)
                logger.info("synced initial traffic allocation from control plane: %s", synced)
        yield
        if owns_client:
            await http_client.aclose()

    app = FastAPI(title="ModelOps Traffic Router", lifespan=lifespan)
    app.state.pending_metric_tasks = pending_metric_tasks  # test introspection only

    def _resolve(version: str) -> str:
        base_url = settings.resolve_base_url(version)
        if base_url is None:
            raise HTTPException(
                status_code=400,
                detail=f"No host:port mapping for version '{version}' "
                "(router doesn't know where it runs)",
            )
        return base_url

    @app.get("/router/config")
    def get_config() -> RouterConfig:
        return store.get()

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

        current = store.get()
        # Compare revisions per model_name, not per deployment_id (changed in
        # Sprint 14 - see TrafficAllocation.revision and
        # docs/DESIGN_NOTES.md#desired-observed-reconciliation): revision is now
        # a monotonic routing generation scoped to the model, so a push from a
        # *different* deployment_id no longer automatically wins - a delayed
        # push from an old, already-terminal deployment must still be rejected
        # if a newer deployment (or a later push from the same one) already
        # landed a higher generation for this model. Only a config for a
        # genuinely different model_name (this router serves exactly one at a
        # time, but the field exists) always wins outright, same as a first-ever
        # push.
        same_model = config.model_name == current.model_name
        if same_model and config.revision <= current.revision:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"stale revision {config.revision} for model "
                    f"{config.model_name} - router already has revision "
                    f"{current.revision} applied"
                ),
            )

        store.set(config)
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
        config = store.get()
        timeout = settings.upstream_timeout_seconds
        target_health = []
        for target in config.targets:
            base_url = settings.resolve_base_url(target.version)
            ready = base_url is not None and await check_ready(http_client, base_url, timeout)
            target_health.append(
                {"version": target.version, "weight": target.weight, "ready": ready}
            )
        return {
            "status": "ok",
            "model_name": config.model_name,
            "targets": target_health,
        }

    @app.post("/router/predict")
    async def route_predict(payload: dict[str, Any]) -> JSONResponse:
        config = store.get()
        target = choose_target(config)
        base_url = _resolve(target.version)

        ready = await check_ready(http_client, base_url, settings.upstream_timeout_seconds)
        if not ready:
            logger.warning(
                "routing refused: target %s (%s) is not ready - rejecting request, "
                "not rerouting to another target",
                target.version,
                base_url,
            )
            raise HTTPException(
                status_code=503,
                detail=f"Target '{target.version}' is not ready",
            )

        start = time.perf_counter()
        try:
            response = await forward_predict(
                http_client, target.version, base_url, payload, settings.upstream_timeout_seconds
            )
        except UpstreamUnavailableError as exc:
            logger.warning("routing failed: %s", exc)
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        latency_ms = (time.perf_counter() - start) * 1000

        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text}

        if isinstance(body, dict):
            body.setdefault("routed_to", target.version)

        if config.deployment_id:
            prediction = body.get("prediction") if isinstance(body, dict) else None
            prediction_id = body.get("prediction_id") if isinstance(body, dict) else None
            _emit_metric(
                config.deployment_id,
                target.version,
                latency_ms,
                response.status_code,
                prediction,
                prediction_id,
            )

        return JSONResponse(status_code=response.status_code, content=body)

    return app


app = create_app(router_settings)
