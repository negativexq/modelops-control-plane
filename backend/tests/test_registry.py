import json
from pathlib import Path

import joblib
import pytest

from app.registry.exceptions import ArtifactNotFoundError, CorruptArtifactError, ModelNotFoundError
from app.registry.local import LocalModelRegistry


def _write_artifact(
    artifacts_dir: Path,
    model_name: str,
    version: str,
    metadata: dict | None = None,
    evaluation: dict | None = None,
    write_model: bool = True,
) -> Path:
    version_dir = artifacts_dir / model_name / version
    version_dir.mkdir(parents=True, exist_ok=True)

    if write_model:
        joblib.dump({"fake": "model"}, version_dir / "model.joblib")
    if metadata is not None:
        (version_dir / "metadata.json").write_text(json.dumps(metadata))
    if evaluation is not None:
        (version_dir / "evaluation.json").write_text(json.dumps(evaluation))

    return version_dir


@pytest.fixture
def registry(tmp_path: Path) -> LocalModelRegistry:
    _write_artifact(
        tmp_path,
        "fraud-model",
        "v1",
        metadata={"version": "v1", "algorithm": "LogisticRegression"},
        evaluation={"recall": 0.9, "precision": 0.5},
    )
    _write_artifact(
        tmp_path,
        "fraud-model",
        "v2-good",
        metadata={"version": "v2-good", "algorithm": "HistGradientBoostingClassifier"},
        evaluation={"recall": 0.95, "precision": 0.6},
    )
    return LocalModelRegistry(tmp_path)


def test_list_models(registry: LocalModelRegistry) -> None:
    assert registry.list_models() == ["fraud-model"]


def test_list_models_empty_dir(tmp_path: Path) -> None:
    empty_registry = LocalModelRegistry(tmp_path / "does-not-exist")
    assert empty_registry.list_models() == []


def test_list_versions(registry: LocalModelRegistry) -> None:
    assert registry.list_versions("fraud-model") == ["v1", "v2-good"]


def test_list_versions_unknown_model_raises(registry: LocalModelRegistry) -> None:
    with pytest.raises(ModelNotFoundError):
        registry.list_versions("unknown-model")


def test_get_model_metadata(registry: LocalModelRegistry) -> None:
    metadata = registry.get_model_metadata("fraud-model", "v1")
    assert metadata["algorithm"] == "LogisticRegression"


def test_get_evaluation(registry: LocalModelRegistry) -> None:
    evaluation = registry.get_evaluation("fraud-model", "v2-good")
    assert evaluation["recall"] == 0.95


def test_get_model_path(registry: LocalModelRegistry) -> None:
    path = registry.get_model_path("fraud-model", "v1")
    assert path.name == "model.joblib"
    assert path.exists()


def test_missing_version_raises_artifact_not_found(registry: LocalModelRegistry) -> None:
    with pytest.raises(ArtifactNotFoundError):
        registry.get_model_metadata("fraud-model", "v99")


def test_incomplete_artifact_raises_artifact_not_found(tmp_path: Path) -> None:
    _write_artifact(
        tmp_path,
        "fraud-model",
        "v-broken",
        metadata={"version": "v-broken"},
        evaluation=None,
        write_model=True,
    )
    registry = LocalModelRegistry(tmp_path)
    with pytest.raises(ArtifactNotFoundError):
        registry.get_model_metadata("fraud-model", "v-broken")


def test_corrupt_json_raises_corrupt_artifact_error(tmp_path: Path) -> None:
    version_dir = _write_artifact(
        tmp_path,
        "fraud-model",
        "v-corrupt",
        metadata={"version": "v-corrupt"},
        evaluation={"recall": 0.5},
    )
    (version_dir / "metadata.json").write_text("{not valid json")

    registry = LocalModelRegistry(tmp_path)
    with pytest.raises(CorruptArtifactError):
        registry.get_model_metadata("fraud-model", "v-corrupt")
