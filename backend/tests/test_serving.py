import json
import time
from pathlib import Path

import joblib
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from sklearn.compose import ColumnTransformer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from app.serving.config import ServingSettings
from app.serving.main import create_app
from app.serving.model_loader import normalize_class_weight


def _write_fake_artifact(
    artifacts_dir: Path, model_name: str, version: str, features: list[str]
) -> None:
    version_dir = artifacts_dir / model_name / version
    version_dir.mkdir(parents=True)

    # A tiny real, fitted pipeline (numeric passthrough + one-hot for merchant_category,
    # matching the shape of the real training pipeline) so predict()/predict_proba()
    # work end to end, including for the categorical column.
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

    metadata = {
        "model_name": model_name,
        "version": version,
        "algorithm": "LogisticRegression",
        "features": features,
        "hyperparameters": {"class_weight": {"0": 1, "1": 150}},
    }
    (version_dir / "metadata.json").write_text(json.dumps(metadata))
    (version_dir / "evaluation.json").write_text(json.dumps({"recall": 0.9, "precision": 0.5}))


FULL_FEATURES = [f"feature_{i}" for i in range(4)] + ["amount", "merchant_category"]
SMALL_FEATURES = ["feature_0", "amount"]


def _settings(artifacts_dir: Path, version: str = "v1", **overrides: object) -> ServingSettings:
    return ServingSettings(
        model_name="fraud-model", model_version=version, artifacts_dir=artifacts_dir, **overrides
    )


@pytest.fixture
def artifacts_dir(tmp_path: Path) -> Path:
    _write_fake_artifact(tmp_path, "fraud-model", "v1", FULL_FEATURES)
    _write_fake_artifact(tmp_path, "fraud-model", "v2-quality-bad", SMALL_FEATURES)
    return tmp_path


def _sample_payload(features: list[str]) -> dict[str, float | int | str]:
    payload: dict[str, float | int | str] = {}
    for name in features:
        if name == "merchant_category":
            payload[name] = "grocery"
        elif name == "transaction_hour":
            payload[name] = 12
        else:
            payload[name] = 1.0
    return payload


def test_health_reports_model_identity(artifacts_dir: Path) -> None:
    client = TestClient(create_app(_settings(artifacts_dir)))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "model_name": "fraud-model", "model_version": "v1"}


def test_ready_reports_feature_schema(artifacts_dir: Path) -> None:
    client = TestClient(create_app(_settings(artifacts_dir)))
    response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["features"] == FULL_FEATURES


def test_ready_returns_503_when_model_missing(tmp_path: Path) -> None:
    client = TestClient(create_app(_settings(tmp_path, version="does-not-exist")))
    response = client.get("/ready")
    assert response.status_code == 503


def test_predict_uses_full_feature_schema_for_v1(artifacts_dir: Path) -> None:
    client = TestClient(create_app(_settings(artifacts_dir)))
    response = client.post("/predict", json=_sample_payload(FULL_FEATURES))
    assert response.status_code == 200
    body = response.json()
    assert body["model_version"] == "v1"
    assert "prediction" in body
    assert "fraud_probability" in body
    assert body["latency_ms"] >= 0


def test_predict_uses_reduced_feature_schema_for_quality_bad(artifacts_dir: Path) -> None:
    client = TestClient(create_app(_settings(artifacts_dir, version="v2-quality-bad")))

    # A field that only the full schema has must be rejected as unexpected for the
    # reduced schema's own required-fields contract to hold - the endpoint should
    # only require SMALL_FEATURES, not FULL_FEATURES.
    response = client.post("/predict", json=_sample_payload(SMALL_FEATURES))
    assert response.status_code == 200
    assert response.json()["model_version"] == "v2-quality-bad"


def test_predict_rejects_missing_required_feature(artifacts_dir: Path) -> None:
    client = TestClient(create_app(_settings(artifacts_dir)))
    payload = _sample_payload(FULL_FEATURES)
    del payload["amount"]
    response = client.post("/predict", json=payload)
    assert response.status_code == 422


def test_latency_injection_delays_response(artifacts_dir: Path) -> None:
    settings = _settings(artifacts_dir, injected_latency_ms=150)
    client = TestClient(create_app(settings))
    start = time.perf_counter()
    response = client.post("/predict", json=_sample_payload(FULL_FEATURES))
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert response.status_code == 200
    assert elapsed_ms >= 150


def test_error_injection_returns_500(artifacts_dir: Path) -> None:
    settings = _settings(artifacts_dir, injected_error_rate=1.0)
    client = TestClient(create_app(settings))
    response = client.post("/predict", json=_sample_payload(FULL_FEATURES))
    assert response.status_code == 500


def test_fault_injection_disabled_in_production(artifacts_dir: Path) -> None:
    settings = _settings(
        artifacts_dir,
        injected_error_rate=1.0,
        injected_latency_ms=500,
        environment="production",
    )
    # The settings themselves must refuse to hold the injected values in production.
    assert settings.injected_error_rate == 0.0
    assert settings.injected_latency_ms == 0

    client = TestClient(create_app(settings))
    start = time.perf_counter()
    response = client.post("/predict", json=_sample_payload(FULL_FEATURES))
    elapsed_ms = (time.perf_counter() - start) * 1000
    assert response.status_code == 200
    assert elapsed_ms < 500


def test_normalize_class_weight_converts_string_keys() -> None:
    assert normalize_class_weight({"0": 1, "1": 150}) == {0: 1, 1: 150}


def test_normalize_class_weight_passthrough_for_non_dict() -> None:
    assert normalize_class_weight("balanced") == "balanced"
