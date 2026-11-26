"""Prometheus metrics and OpenTelemetry tracing (app/observability/, plus each
component's telemetry.py)."""

# Fixtures (client, fake_gateway, artifacts_dir) are imported from the modules
# that define them and then requested by name, which ruff reads as redefinition.
# ruff: noqa: F811

import asyncio
import json
import re
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

import app.control_plane.telemetry
import app.router.telemetry
import app.serving.main
import app.worker.telemetry  # noqa: F401 - registers the worker's metrics for the dashboard check
from app.router.config import RouterConfig, TargetWeight
from app.router.main import create_app as create_router_app
from app.serving.main import create_app as create_serving_app
from app.worker.loop import run_once
from tests.test_control_plane import (  # noqa: F401 - fixtures
    FakeRouterGateway,
    _create_deployment,
    client,
    fake_gateway,
)
from tests.test_router import (
    CANARY_HOST_PORT,
    FULL_FEATURES,
    STABLE_HOST_PORT,
    _router_settings,
    _sample_payload,
    _serving_app,
    artifacts_dir,  # noqa: F401 - fixture
    unreachable_transport,
)
from tests.test_worker_loop import FakeWorkerClient, _deployment

DASHBOARDS = Path(__file__).resolve().parents[2] / "observability" / "grafana" / "dashboards"


def sample(name: str, **labels: str) -> float:
    """Current value of one series, 0 when it doesn't exist yet. Metrics are
    process-global, so tests compare before/after instead of absolute values."""
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _router(artifacts_dir: Path, **kwargs: object) -> TestClient:
    mounts = {
        f"http://{STABLE_HOST_PORT}": httpx.ASGITransport(app=_serving_app(artifacts_dir, "v1")),
        f"http://{CANARY_HOST_PORT}": httpx.ASGITransport(
            app=_serving_app(artifacts_dir, "v2-good")
        ),
    }
    settings = _router_settings(stable_weight=0.0, canary_weight=1.0)
    app = create_router_app(settings, client=httpx.AsyncClient(mounts=mounts), **kwargs)  # type: ignore[arg-type]
    return TestClient(app)


# --- HTTP metrics -------------------------------------------------------------------


def test_backend_metrics_label_requests_by_route_template(client: TestClient) -> None:
    labels = {
        "service": "backend",
        "method": "GET",
        "route": "/api/deployments/{deployment_id}",
        "status": "404",
    }
    before = sample("modelops_http_requests_total", **labels)

    client.get("/api/deployments/does-not-exist")
    client.get("/api/deployments/also-missing")

    # One series for the route, not one per id.
    assert sample("modelops_http_requests_total", **labels) == before + 2
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "modelops_http_request_duration_seconds_bucket" in response.text
    assert "does-not-exist" not in response.text


def test_metrics_endpoint_is_not_in_the_openapi_schema(client: TestClient) -> None:
    assert "/metrics" not in client.get("/openapi.json").json()["paths"]


def test_unmatched_paths_share_one_label(client: TestClient) -> None:
    labels = {"service": "backend", "method": "GET", "route": "unmatched", "status": "404"}
    before = sample("modelops_http_requests_total", **labels)
    client.get("/no/such/path/123")
    client.get("/another/random/path")
    assert sample("modelops_http_requests_total", **labels) == before + 2


# --- router -------------------------------------------------------------------------


def test_router_counts_predictions_and_latency_per_version(artifacts_dir: Path) -> None:
    router = _router(artifacts_dir)
    requests = {"model": "fraud-model", "version": "v2-good", "status": "200"}
    before = sample("modelops_router_requests_total", **requests)
    latency_before = sample(
        "modelops_router_upstream_latency_seconds_count", model="fraud-model", version="v2-good"
    )
    inference_before = sample(
        "modelops_serving_inference_seconds_count", model="fraud-model", version="v2-good"
    )

    for _ in range(3):
        assert (
            router.post("/router/predict", json=_sample_payload(FULL_FEATURES)).status_code == 200
        )

    assert sample("modelops_router_requests_total", **requests) == before + 3
    assert (
        sample(
            "modelops_router_upstream_latency_seconds_count",
            model="fraud-model",
            version="v2-good",
        )
        == latency_before + 3
    )
    assert (
        sample("modelops_serving_inference_seconds_count", model="fraud-model", version="v2-good")
        == inference_before + 3
    )


def test_router_counts_unreachable_targets_as_503(artifacts_dir: Path) -> None:
    settings = _router_settings(stable_weight=0.0, canary_weight=1.0)
    app = create_router_app(settings, client=httpx.AsyncClient(transport=unreachable_transport()))
    labels = {"model": "fraud-model", "version": "v2-good", "status": "503"}
    before = sample("modelops_router_requests_total", **labels)

    response = TestClient(app).post("/router/predict", json=_sample_payload(FULL_FEATURES))

    assert response.status_code == 503
    assert sample("modelops_router_requests_total", **labels) == before + 1


def test_traffic_weight_gauge_follows_config_pushes(artifacts_dir: Path) -> None:
    router = _router(artifacts_dir)
    model = "gauge-model"
    weight = "modelops_router_traffic_weight"

    first = RouterConfig(
        model_name=model,
        revision=1,
        targets=[
            TargetWeight(version="v1", weight=0.8),
            TargetWeight(version="v2-good", weight=0.2),
        ],
    )
    assert router.put("/router/config", json=first.model_dump()).status_code == 200
    assert sample(weight, model=model, version="v2-good") == 0.2
    assert sample("modelops_router_config_revision", model=model) == 1

    # A promote: v1 leaves the split, so its series goes away instead of
    # staying at a stale 0.8.
    second = RouterConfig(
        model_name=model, revision=2, targets=[TargetWeight(version="v2-good", weight=1.0)]
    )
    assert router.put("/router/config", json=second.model_dump()).status_code == 200
    assert sample(weight, model=model, version="v2-good") == 1.0
    assert REGISTRY.get_sample_value(weight, {"model": model, "version": "v1"}) is None
    assert sample("modelops_router_config_revision", model=model) == 2


# --- control plane ------------------------------------------------------------------


def test_decisions_are_counted_by_outcome_and_trigger(client: TestClient) -> None:
    promoted = {"outcome": "PROMOTED", "trigger": "manual"}
    rolled_back = {"outcome": "ROLLED_BACK", "trigger": "automatic"}
    promoted_before = sample("modelops_decisions_total", **promoted)
    rolled_back_before = sample("modelops_decisions_total", **rolled_back)

    first = _create_deployment(client, model_name="metrics-a").json()
    assert client.post(f"/api/deployments/{first['id']}/promote").status_code == 200
    second = _create_deployment(client, model_name="metrics-b").json()
    response = client.post(
        f"/api/deployments/{second['id']}/rollback", params={"triggered_by": "automatic"}
    )
    assert response.status_code == 200

    assert sample("modelops_decisions_total", **promoted) == promoted_before + 1
    assert sample("modelops_decisions_total", **rolled_back) == rolled_back_before + 1


def test_policy_evaluations_are_counted_by_verdict(client: TestClient) -> None:
    deployment = _create_deployment(client, model_name="metrics-eval").json()
    # No traffic recorded yet, so the minimum-sample gate keeps it INCONCLUSIVE.
    before = sample("modelops_policy_evaluations_total", result="INCONCLUSIVE")
    checks_before = sample(
        "modelops_policy_checks_total", policy="minimum_requests", result="INCONCLUSIVE"
    )

    response = client.post(f"/api/deployments/{deployment['id']}/evaluate")

    assert response.status_code == 200
    assert response.json()["overall_result"] == "INCONCLUSIVE"
    # The dashboard pre-creates one series per policy; a new policy must be added
    # to telemetry.POLICY_NAMES or its first failure is invisible to increase().
    checked = {c["policy_name"] for c in response.json()["checks"]}
    assert checked <= set(app.control_plane.telemetry.POLICY_NAMES)
    assert sample("modelops_policy_evaluations_total", result="INCONCLUSIVE") == before + 1
    assert (
        sample("modelops_policy_checks_total", policy="minimum_requests", result="INCONCLUSIVE")
        == checks_before + 1
    )


def test_reconcile_counts_corrections_and_router_outages(
    client: TestClient, fake_gateway: FakeRouterGateway
) -> None:
    _create_deployment(client, model_name="metrics-reconcile")
    fake_gateway.configs.clear()  # router lost its state, e.g. a restart
    corrected = sample(
        "modelops_reconcile_runs_total", model="metrics-reconcile", result="corrected"
    )

    assert client.post("/api/router/reconcile").json()["reconciled"] is True
    assert (
        sample("modelops_reconcile_runs_total", model="metrics-reconcile", result="corrected")
        == corrected + 1
    )

    fake_gateway.should_fail = True  # router unreachable
    errors = sample("modelops_reconcile_errors_total", reason="router_unreachable")
    client.post("/api/router/reconcile")
    assert sample("modelops_reconcile_errors_total", reason="router_unreachable") == errors + 1


# --- worker -------------------------------------------------------------------------


def test_worker_sweep_records_heartbeat_actions_and_errors() -> None:
    fake = FakeWorkerClient()
    fake.deployments["dep-1"] = _deployment(canary_weight=1.0)
    fake.evaluate_result["dep-1"] = "PASS"
    fake.reconcile_should_raise = True
    sweeps = sample("modelops_worker_sweeps_total")
    promotes = sample("modelops_worker_actions_total", action="promote")
    reconcile_errors = sample("modelops_worker_errors_total", stage="reconcile")

    asyncio.run(run_once(fake, 300))

    assert sample("modelops_worker_sweeps_total") == sweeps + 1
    assert sample("modelops_worker_actions_total", action="promote") == promotes + 1
    assert sample("modelops_worker_errors_total", stage="reconcile") == reconcile_errors + 1
    assert sample("modelops_worker_active_rollouts") == 1
    assert sample("modelops_worker_last_sweep_timestamp_seconds") > 0


# --- tracing ------------------------------------------------------------------------


def test_one_trace_spans_router_to_model_inference(artifacts_dir: Path) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    serving = create_serving_app(
        app.serving.main.ServingSettings(
            model_name="fraud-model", model_version="v2-good", artifacts_dir=artifacts_dir
        ),
        tracer_provider=provider,
    )
    mounts = {
        f"http://{STABLE_HOST_PORT}": httpx.ASGITransport(app=serving),
        f"http://{CANARY_HOST_PORT}": httpx.ASGITransport(app=serving),
    }
    router = create_router_app(
        _router_settings(stable_weight=0.0, canary_weight=1.0),
        client=httpx.AsyncClient(mounts=mounts),
        tracer_provider=provider,
    )

    response = TestClient(router).post("/router/predict", json=_sample_payload(FULL_FEATURES))
    assert response.status_code == 200

    spans = exporter.get_finished_spans()
    by_name = {s.name: s for s in spans}
    root = by_name["POST /router/predict"]
    inference = by_name["model.inference"]
    serving_predict = by_name["POST /predict"]  # the model service's server span

    # traceparent crossed the HTTP hop: the model service's spans are in the
    # router's trace, not a new one.
    assert {s.context.trace_id for s in spans} == {root.context.trace_id}
    assert inference.parent is not None
    assert inference.parent.span_id == serving_predict.context.span_id
    assert root.attributes is not None
    assert root.attributes["modelops.version"] == "v2-good"
    assert root.attributes["modelops.model"] == "fraud-model"
    assert inference.attributes is not None
    assert inference.attributes["modelops.version"] == "v2-good"


def test_tracing_is_off_without_an_otlp_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.observability import tracing

    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    tracing.configured_provider.cache_clear()
    try:
        assert tracing.configured_provider("anything") is None
    finally:
        tracing.configured_provider.cache_clear()


# --- dashboards ---------------------------------------------------------------------


def test_dashboards_only_query_metrics_the_services_export() -> None:
    """A renamed metric would otherwise leave a Grafana panel silently empty."""
    exported = set()
    for collector_names in REGISTRY._collector_to_names.values():  # noqa: SLF001
        exported.update(collector_names)

    dashboards = sorted(DASHBOARDS.glob("*.json"))
    assert dashboards, f"no dashboards in {DASHBOARDS}"
    for path in dashboards:
        queried = set(re.findall(r"\bmodelops_[a-z_]+", path.read_text()))
        assert queried, path.name
        missing = sorted(queried - exported)
        assert not missing, f"{path.name} queries unknown metrics: {missing}"
        json.loads(path.read_text())  # and it's valid JSON


def test_trace_exclusions_skip_scrapes_but_not_metric_ingestion() -> None:
    """The router's metric push (POST .../metrics) must stay traced, or the control
    plane drops out of every prediction trace; only the Prometheus scrape and the
    probes are skipped."""
    from opentelemetry.util.http import ExcludeList

    from app.observability.tracing import EXCLUDED_URLS

    excluded = ExcludeList(EXCLUDED_URLS.split(","))
    assert excluded.url_disabled("http://router:8000/metrics")
    assert excluded.url_disabled("http://model-serving-v1:8000/health")
    assert not excluded.url_disabled("http://backend:8000/api/deployments/abc/metrics")
    assert not excluded.url_disabled("http://backend:8000/api/deployments/abc/metrics/timeseries")
    assert not excluded.url_disabled("http://router:8000/router/health")
