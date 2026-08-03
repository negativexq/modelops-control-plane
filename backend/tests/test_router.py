import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import joblib
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from app.router.config import RouterConfig, RouterSettings, TargetWeight
from app.router.main import create_app as create_router_app
from app.serving.config import ServingSettings
from app.serving.main import create_app as create_serving_app

FULL_FEATURES = [f"feature_{i}" for i in range(4)] + ["amount", "merchant_category"]
SMALL_FEATURES = ["feature_0", "amount"]

STABLE_HOST_PORT = "stable:8000"
CANARY_HOST_PORT = "canary:8000"


def _write_fake_artifact(
    artifacts_dir: Path, model_name: str, version: str, features: list[str]
) -> None:
    version_dir = artifacts_dir / model_name / version
    version_dir.mkdir(parents=True)

    categorical = [f for f in features if f == "merchant_category"]
    numeric = [f for f in features if f != "merchant_category"]
    rows = []
    for i in range(10):
        row = {f: float(i % 2) for f in numeric}
        if categorical:
            row["merchant_category"] = "grocery" if i % 2 == 0 else "travel"
        rows.append(row)
    x = pd.DataFrame(rows)[features]
    y = [i % 2 for i in range(10)]

    preprocess = ColumnTransformer(
        transformers=[
            ("numeric", "passthrough", numeric),
            *(
                [("categorical", OneHotEncoder(handle_unknown="ignore"), categorical)]
                if categorical
                else []
            ),
        ]
    )
    model = Pipeline(steps=[("preprocess", preprocess), ("model", LogisticRegression())])
    model.fit(x, y)
    joblib.dump(model, version_dir / "model.joblib")

    metadata = {"model_name": model_name, "version": version, "features": features}
    (version_dir / "metadata.json").write_text(json.dumps(metadata))
    (version_dir / "evaluation.json").write_text(json.dumps({"recall": 0.9, "precision": 0.5}))


def _serving_app(artifacts_dir: Path, version: str) -> FastAPI:
    settings = ServingSettings(
        model_name="fraud-model", model_version=version, artifacts_dir=artifacts_dir
    )
    return create_serving_app(settings)


def _sample_payload(features: list[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in features:
        if name == "merchant_category":
            payload[name] = "grocery"
        elif name == "transaction_hour":
            payload[name] = 12
        else:
            payload[name] = 1.0
    return payload


class CountingASGITransport(httpx.ASGITransport):
    """Wraps ASGITransport to count how many requests actually reached it."""

    def __init__(self, app: FastAPI) -> None:
        super().__init__(app=app)
        self.call_count = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.call_count += 1
        return await super().handle_async_request(request)


def unreachable_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    _write_fake_artifact(tmp_path, "fraud-model", "v1", FULL_FEATURES)
    _write_fake_artifact(tmp_path, "fraud-model", "v2-good", FULL_FEATURES)
    _write_fake_artifact(tmp_path, "fraud-model", "v2-quality-bad", SMALL_FEATURES)
    return tmp_path


def _router_settings(
    stable_version: str = "v1",
    canary_version: str = "v2-good",
    stable_weight: float = 0.9,
    canary_weight: float = 0.1,
    control_plane_url: str | None = None,
) -> RouterSettings:
    return RouterSettings(
        version_hosts={
            stable_version: STABLE_HOST_PORT,
            canary_version: CANARY_HOST_PORT,
        },
        initial_targets=[
            TargetWeight(version=stable_version, weight=stable_weight),
            TargetWeight(version=canary_version, weight=canary_weight),
        ],
        control_plane_url=control_plane_url,
    )


def _router_client(
    artifacts_dir: Path,
    stable_version: str = "v1",
    canary_version: str = "v2-good",
    stable_weight: float = 0.9,
    canary_weight: float = 0.1,
) -> TestClient:
    stable_app = _serving_app(artifacts_dir, stable_version)
    canary_app = _serving_app(artifacts_dir, canary_version)
    mounts = {
        f"http://{STABLE_HOST_PORT}": httpx.ASGITransport(app=stable_app),
        f"http://{CANARY_HOST_PORT}": httpx.ASGITransport(app=canary_app),
    }
    client = httpx.AsyncClient(mounts=mounts)
    settings = _router_settings(stable_version, canary_version, stable_weight, canary_weight)
    router_app = create_router_app(settings, client=client)
    return TestClient(router_app)


def test_router_health_reports_downstream_readiness(artifacts_dir: Path) -> None:
    client = _router_client(artifacts_dir)
    response = client.get("/router/health")
    assert response.status_code == 200
    body = response.json()
    targets_by_version = {t["version"]: t for t in body["targets"]}
    assert targets_by_version["v1"]["ready"] is True
    assert targets_by_version["v2-good"]["ready"] is True


def test_get_config_returns_current_config(artifacts_dir: Path) -> None:
    client = _router_client(artifacts_dir, stable_weight=0.7, canary_weight=0.3)
    response = client.get("/router/config")
    assert response.status_code == 200
    body = response.json()
    weights = {t["version"]: t["weight"] for t in body["targets"]}
    assert weights == {"v1": 0.7, "v2-good": 0.3}


def test_put_config_updates_weights_without_restart(artifacts_dir: Path) -> None:
    client = _router_client(artifacts_dir, stable_weight=1.0, canary_weight=0.0)
    sample = _sample_payload(FULL_FEATURES)

    # With canary_weight=0, every request should go to stable.
    for _ in range(20):
        response = client.post("/router/predict", json=sample)
        assert response.json()["routed_to"] == "v1"

    new_config = client.get("/router/config").json()
    new_config["targets"] = [
        {"version": "v1", "weight": 0.0},
        {"version": "v2-good", "weight": 1.0},
    ]
    put_response = client.put("/router/config", json=new_config)
    assert put_response.status_code == 200
    weights = {t["version"]: t["weight"] for t in put_response.json()["targets"]}
    assert weights["v2-good"] == 1.0

    # After the update, every request should now go to canary - no restart needed.
    for _ in range(20):
        response = client.post("/router/predict", json=sample)
        assert response.json()["routed_to"] == "v2-good"


def test_put_config_rejects_target_with_unknown_version(artifacts_dir: Path) -> None:
    client = _router_client(artifacts_dir)
    config = client.get("/router/config").json()
    config["targets"] = [{"version": "v99-does-not-exist", "weight": 1.0}]
    response = client.put("/router/config", json=config)
    assert response.status_code == 400


def test_weighted_distribution_is_close_to_configured_ratio(artifacts_dir: Path) -> None:
    client = _router_client(artifacts_dir, stable_weight=0.9, canary_weight=0.1)
    sample = _sample_payload(FULL_FEATURES)

    counts: Counter[str] = Counter()
    n = 2000
    for _ in range(n):
        response = client.post("/router/predict", json=sample)
        assert response.status_code == 200
        counts[response.json()["routed_to"]] += 1

    stable_ratio = counts["v1"] / n
    assert 0.85 <= stable_ratio <= 0.95


def test_payload_forwarded_as_is_to_reduced_schema_canary(artifacts_dir: Path) -> None:
    client = _router_client(
        artifacts_dir, canary_version="v2-quality-bad", stable_weight=0.0, canary_weight=1.0
    )
    response = client.post("/router/predict", json=_sample_payload(SMALL_FEATURES))
    assert response.status_code == 200
    assert response.json()["routed_to"] == "v2-quality-bad"


def test_downstream_schema_mismatch_error_is_forwarded_unmodified(artifacts_dir: Path) -> None:
    # v2-quality-bad only expects SMALL_FEATURES; sending an incomplete payload should
    # surface the downstream's own 422, unmodified, rather than the router
    # pre-validating or rewriting it.
    client = _router_client(
        artifacts_dir, canary_version="v2-quality-bad", stable_weight=0.0, canary_weight=1.0
    )
    payload = _sample_payload(SMALL_FEATURES)
    del payload["amount"]
    response = client.post("/router/predict", json=payload)
    assert response.status_code == 422
    assert response.json()["routed_to"] == "v2-quality-bad"


def test_unready_target_returns_503_and_does_not_fall_back(
    artifacts_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stable_app = _serving_app(artifacts_dir, "v1")
    # canary points at a version with no artifact at all -> /ready returns 503.
    canary_app = create_serving_app(
        ServingSettings(
            model_name="fraud-model",
            model_version="does-not-exist",
            artifacts_dir=artifacts_dir,
        )
    )
    stable_transport = CountingASGITransport(app=stable_app)
    mounts = {
        f"http://{STABLE_HOST_PORT}": stable_transport,
        f"http://{CANARY_HOST_PORT}": httpx.ASGITransport(app=canary_app),
    }
    client = httpx.AsyncClient(mounts=mounts)
    settings = _router_settings(stable_weight=0.0, canary_weight=1.0)
    router_app = create_router_app(settings, client=client)
    test_client = TestClient(router_app)

    with caplog.at_level("WARNING"):
        response = test_client.post("/router/predict", json=_sample_payload(FULL_FEATURES))

    assert response.status_code == 503
    assert "not ready" in response.json()["detail"]
    assert any("not ready" in record.message for record in caplog.records)
    # Must not have silently rerouted to stable.
    assert stable_transport.call_count == 0


def test_unreachable_target_returns_503(artifacts_dir: Path) -> None:
    stable_app = _serving_app(artifacts_dir, "v1")
    mounts = {
        f"http://{STABLE_HOST_PORT}": httpx.ASGITransport(app=stable_app),
        f"http://{CANARY_HOST_PORT}": unreachable_transport(),
    }
    client = httpx.AsyncClient(mounts=mounts)
    settings = _router_settings(stable_weight=0.0, canary_weight=1.0)
    router_app = create_router_app(settings, client=client)
    test_client = TestClient(router_app)

    response = test_client.post("/router/predict", json=_sample_payload(FULL_FEATURES))
    assert response.status_code == 503


def test_router_config_rejects_zero_total_weight() -> None:
    with pytest.raises(ValidationError):
        RouterConfig(
            model_name="fraud-model",
            targets=[
                TargetWeight(version="v1", weight=0),
                TargetWeight(version="v2-good", weight=0),
            ],
        )


def test_router_config_rejects_duplicate_versions() -> None:
    with pytest.raises(ValidationError):
        RouterConfig(
            model_name="fraud-model",
            targets=[
                TargetWeight(version="v1", weight=0.5),
                TargetWeight(version="v1", weight=0.5),
            ],
        )


def test_router_config_rejects_empty_targets() -> None:
    with pytest.raises(ValidationError):
        RouterConfig(model_name="fraud-model", targets=[])


CONTROL_PLANE_HOST_PORT = "control-plane:9000"
CONTROL_PLANE_URL = f"http://{CONTROL_PLANE_HOST_PORT}"


class RecordingTransport(httpx.AsyncBaseTransport):
    """Records every request it receives and returns 202. Optionally delays first."""

    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.calls: list[httpx.Request] = []
        self._delay_seconds = delay_seconds

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.calls.append(request)
        if self._delay_seconds:
            import asyncio

            await asyncio.sleep(self._delay_seconds)
        return httpx.Response(202, json={"recorded": True})


def _router_client_with_control_plane(
    artifacts_dir: Path,
    control_plane_transport: httpx.AsyncBaseTransport,
    stable_version: str = "v1",
    canary_version: str = "v2-good",
) -> TestClient:
    stable_app = _serving_app(artifacts_dir, stable_version)
    canary_app = _serving_app(artifacts_dir, canary_version)
    mounts = {
        f"http://{STABLE_HOST_PORT}": httpx.ASGITransport(app=stable_app),
        f"http://{CANARY_HOST_PORT}": httpx.ASGITransport(app=canary_app),
        CONTROL_PLANE_URL: control_plane_transport,
    }
    client = httpx.AsyncClient(mounts=mounts)
    settings = RouterSettings(
        version_hosts={stable_version: STABLE_HOST_PORT, canary_version: CANARY_HOST_PORT},
        initial_targets=[
            TargetWeight(version=stable_version, weight=1.0),
            TargetWeight(version=canary_version, weight=0.0),
        ],
        control_plane_url=CONTROL_PLANE_URL,
    )
    router_app = create_router_app(settings, client=client)
    test_client = TestClient(router_app)

    # Attach a deployment_id via PUT /router/config so route_predict has one to
    # attribute metrics to (bypasses the lifespan startup-sync path entirely).
    config = test_client.get("/router/config").json()
    config["deployment_id"] = "dep-123"
    put_response = test_client.put("/router/config", json=config)
    assert put_response.status_code == 200

    return test_client


def test_predict_emits_metric_with_deployment_id(artifacts_dir: Path) -> None:
    transport = RecordingTransport()
    client = _router_client_with_control_plane(artifacts_dir, transport)

    response = client.post("/router/predict", json=_sample_payload(FULL_FEATURES))
    assert response.status_code == 200

    for _ in range(20):
        if transport.calls:
            break
        time.sleep(0.02)

    assert len(transport.calls) == 1
    sent = json.loads(transport.calls[0].content)
    assert sent["model_version"] == "v1"
    assert sent["status_code"] == 200
    assert transport.calls[0].url.path == "/api/deployments/dep-123/metrics"


def test_predict_does_not_block_on_slow_metric_push(artifacts_dir: Path) -> None:
    delay_seconds = 0.4
    transport = RecordingTransport(delay_seconds=delay_seconds)
    client = _router_client_with_control_plane(artifacts_dir, transport)

    start = time.perf_counter()
    response = client.post("/router/predict", json=_sample_payload(FULL_FEATURES))
    elapsed = time.perf_counter() - start

    assert response.status_code == 200
    # The metric push takes `delay_seconds` - if predict waited on it, this would be
    # at least that long. It shouldn't be anywhere close.
    assert elapsed < delay_seconds / 2

    # Confirm the metric call did eventually happen (fire-and-forget, not "never").
    for _ in range(50):
        if transport.calls:
            break
        time.sleep(0.02)
    assert len(transport.calls) == 1


def test_predict_succeeds_even_if_metric_push_is_unreachable(artifacts_dir: Path) -> None:
    unreachable = unreachable_transport()
    client = _router_client_with_control_plane(artifacts_dir, unreachable)

    response = client.post("/router/predict", json=_sample_payload(FULL_FEATURES))
    assert response.status_code == 200
    assert response.json()["routed_to"] == "v1"


def test_predict_skips_metric_emission_without_deployment_id(artifacts_dir: Path) -> None:
    transport = RecordingTransport()
    stable_app = _serving_app(artifacts_dir, "v1")
    canary_app = _serving_app(artifacts_dir, "v2-good")
    mounts = {
        f"http://{STABLE_HOST_PORT}": httpx.ASGITransport(app=stable_app),
        f"http://{CANARY_HOST_PORT}": httpx.ASGITransport(app=canary_app),
        CONTROL_PLANE_URL: transport,
    }
    client = httpx.AsyncClient(mounts=mounts)
    settings = _router_settings(control_plane_url=CONTROL_PLANE_URL)
    router_app = create_router_app(settings, client=client)
    test_client = TestClient(router_app)

    # No PUT /router/config here, so config.deployment_id stays None (the default).
    response = test_client.post("/router/predict", json=_sample_payload(FULL_FEATURES))
    assert response.status_code == 200

    time.sleep(0.1)
    assert transport.calls == []
