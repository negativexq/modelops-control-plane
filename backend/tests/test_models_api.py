import json
from collections.abc import Iterator
from pathlib import Path

import joblib
import pytest
from fastapi.testclient import TestClient

from app.api.models import get_registry
from app.main import app
from app.registry.local import LocalModelRegistry


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    version_dir = tmp_path / "fraud-model" / "v1"
    version_dir.mkdir(parents=True)
    joblib.dump({"fake": "model"}, version_dir / "model.joblib")
    (version_dir / "metadata.json").write_text(json.dumps({"version": "v1"}))
    (version_dir / "evaluation.json").write_text(json.dumps({"recall": 0.9, "precision": 0.5}))

    app.dependency_overrides[get_registry] = lambda: LocalModelRegistry(tmp_path)
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.pop(get_registry, None)


def test_list_models(client: TestClient) -> None:
    response = client.get("/api/models")
    assert response.status_code == 200
    assert response.json() == ["fraud-model"]


def test_list_versions(client: TestClient) -> None:
    response = client.get("/api/models/fraud-model/versions")
    assert response.status_code == 200
    assert response.json() == ["v1"]


def test_list_versions_unknown_model_404(client: TestClient) -> None:
    response = client.get("/api/models/unknown/versions")
    assert response.status_code == 404


def test_get_version_metadata(client: TestClient) -> None:
    response = client.get("/api/models/fraud-model/versions/v1")
    assert response.status_code == 200
    assert response.json() == {"version": "v1"}


def test_get_version_metadata_unknown_version_404(client: TestClient) -> None:
    response = client.get("/api/models/fraud-model/versions/v99")
    assert response.status_code == 404


def test_get_version_evaluation(client: TestClient) -> None:
    response = client.get("/api/models/fraud-model/versions/v1/evaluation")
    assert response.status_code == 200
    assert response.json() == {"recall": 0.9, "precision": 0.5}
