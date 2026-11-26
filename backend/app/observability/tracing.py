"""OpenTelemetry tracing, off unless OTEL_EXPORTER_OTLP_ENDPOINT is set.

docker-compose points every service at Jaeger's OTLP/HTTP receiver, so a single
prediction shows up as one trace: router server span -> readiness check and
/predict client spans -> the serving container's server span -> its
model.inference span, plus the router's metric push to the control plane. W3C
traceparent headers carry the context across each hop (the httpx
instrumentation injects them, the FastAPI instrumentation reads them).

Tests and local runs without the env var get no provider and no instrumentation
at all - nothing is exported and no headers are added. Tests that want spans
pass their own TracerProvider (with an in-memory exporter) to the app factory.
"""

import os
from functools import cache

import httpx
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

# Probes and scrapes would otherwise be most of what a trace backend stores.
# These are regexes *searched* in the full URL, so they're anchored to the path
# root: a bare "/metrics" also matched the router's metric push to
# /api/deployments/{id}/metrics and cut the control plane out of every trace.
EXCLUDED_URLS = r"^https?://[^/]+/metrics$,^https?://[^/]+/health$"


@cache
def configured_provider(default_service_name: str) -> TracerProvider | None:
    """The process-wide provider, built once from the standard OTEL_* env vars.
    OTEL_SERVICE_NAME (set per container in docker-compose) wins over
    `default_service_name`."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return None
    attributes = {} if os.getenv("OTEL_SERVICE_NAME") else {SERVICE_NAME: default_service_name}
    provider = TracerProvider(resource=Resource.create(attributes))
    # The exporter reads OTEL_EXPORTER_OTLP_ENDPOINT itself and appends /v1/traces.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    return provider


def instrument_fastapi(app: FastAPI, provider: TracerProvider | None) -> None:
    if provider is not None:
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls=EXCLUDED_URLS,
            # The per-message ASGI "http receive"/"http send" spans double a
            # trace's length without saying anything the server span doesn't.
            exclude_spans=["receive", "send"],
        )


def instrument_httpx(client: httpx.AsyncClient, provider: TracerProvider | None) -> None:
    if provider is not None:
        HTTPXClientInstrumentor.instrument_client(client, tracer_provider=provider)


def tracer(name: str, provider: TracerProvider | None) -> trace.Tracer:
    """A no-op tracer when tracing is off, so call sites never branch on it."""
    return trace.get_tracer(name, tracer_provider=provider)
