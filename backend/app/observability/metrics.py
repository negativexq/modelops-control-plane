"""Prometheus metrics shared by every HTTP service (backend, router, model
serving): a request counter and a latency histogram per route, and the
GET /metrics endpoint that exposes them plus each component's own domain
metrics (app/router/telemetry.py, app/control_plane/telemetry.py, ...).

Each container runs one uvicorn process, so the default in-process registry is
the whole picture for that container - no multiprocess mode needed. The worker
has no HTTP app of its own and serves the same registry with
prometheus_client.start_http_server instead (see app/worker/main.py).
"""

import time

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Requests are short API calls and model predictions - the interesting range is
# a few ms to a couple of seconds; anything slower is already an outlier.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

HTTP_REQUESTS = Counter(
    "modelops_http_requests_total",
    "HTTP requests handled, by service, route template and status code.",
    ["service", "method", "route", "status"],
)
HTTP_LATENCY = Histogram(
    "modelops_http_request_duration_seconds",
    "Time until the response started (headers sent), by service and route template. "
    "Measured to the first byte so the SSE stream reports its setup time, not how "
    "long the client stayed connected.",
    ["service", "method", "route"],
    buckets=LATENCY_BUCKETS,
)

UNMATCHED_ROUTE = "unmatched"


def _route_template(scope: Scope) -> str:
    """The matched route's path template (/api/deployments/{deployment_id}), never
    the raw path - one time series per route, not per deployment id. Unmatched
    paths (404s, probes for random URLs) collapse into a single label value."""
    route = scope.get("route")
    path = getattr(route, "path_format", None) or getattr(route, "path", None)
    return path if isinstance(path, str) else UNMATCHED_ROUTE


class PrometheusMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so a streaming response - the SSE
    feed - passes through untouched."""

    def __init__(self, app: ASGIApp, service: str, excluded_paths: frozenset[str]) -> None:
        self.app = app
        self.service = service
        self.excluded_paths = excluded_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.excluded_paths:
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                self._record(scope, status, time.perf_counter() - start)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            if status is None:
                self._record(scope, 500, time.perf_counter() - start)
            raise

    def _record(self, scope: Scope, status: int, seconds: float) -> None:
        method = str(scope.get("method", "GET"))
        route = _route_template(scope)
        HTTP_REQUESTS.labels(self.service, method, route, str(status)).inc()
        HTTP_LATENCY.labels(self.service, method, route).observe(seconds)


def metrics_response() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def instrument_app(app: FastAPI, service: str) -> None:
    """Adds the request metrics middleware and GET /metrics. /metrics is left out
    of the OpenAPI schema (it's for Prometheus, not the dashboard) and out of its
    own request counts, along with the liveness/readiness probes."""
    app.add_middleware(
        PrometheusMiddleware,
        service=service,
        excluded_paths=frozenset({"/metrics", "/health", "/ready"}),
    )
    app.add_api_route("/metrics", metrics_response, methods=["GET"], include_in_schema=False)
