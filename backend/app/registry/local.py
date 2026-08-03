import json
from pathlib import Path
from typing import Any

from app.registry.base import ModelRegistry
from app.registry.exceptions import ArtifactNotFoundError, CorruptArtifactError, ModelNotFoundError

REQUIRED_ARTIFACT_FILES = ("model.joblib", "metadata.json", "evaluation.json")


class LocalModelRegistry(ModelRegistry):
    """Reads model artifacts from a directory tree of the shape:

    <artifacts_dir>/<model_name>/<version>/{model.joblib, metadata.json, evaluation.json}
    """

    def __init__(self, artifacts_dir: Path) -> None:
        self.artifacts_dir = Path(artifacts_dir)

    def list_models(self) -> list[str]:
        if not self.artifacts_dir.is_dir():
            return []
        return sorted(p.name for p in self.artifacts_dir.iterdir() if p.is_dir())

    def list_versions(self, model_name: str) -> list[str]:
        model_dir = self._model_dir(model_name)
        return sorted(p.name for p in model_dir.iterdir() if p.is_dir())

    def get_model_metadata(self, model_name: str, version: str) -> dict[str, Any]:
        return self._read_json(model_name, version, "metadata.json")

    def get_evaluation(self, model_name: str, version: str) -> dict[str, Any]:
        return self._read_json(model_name, version, "evaluation.json")

    def get_model_path(self, model_name: str, version: str) -> Path:
        version_dir = self._version_dir(model_name, version)
        model_path = version_dir / "model.joblib"
        if not model_path.is_file():
            raise ArtifactNotFoundError(
                f"model.joblib missing for {model_name}/{version} at {model_path}"
            )
        return model_path

    def _model_dir(self, model_name: str) -> Path:
        model_dir = self.artifacts_dir / model_name
        if not model_dir.is_dir():
            raise ModelNotFoundError(f"Model '{model_name}' not found in registry")
        return model_dir

    def _version_dir(self, model_name: str, version: str) -> Path:
        version_dir = self._model_dir(model_name) / version
        if not version_dir.is_dir():
            raise ArtifactNotFoundError(f"Version '{version}' not found for model '{model_name}'")

        missing = [f for f in REQUIRED_ARTIFACT_FILES if not (version_dir / f).is_file()]
        if missing:
            raise ArtifactNotFoundError(
                f"Artifact for {model_name}/{version} is missing required file(s): {missing}"
            )
        return version_dir

    def _read_json(self, model_name: str, version: str, filename: str) -> dict[str, Any]:
        version_dir = self._version_dir(model_name, version)
        path = version_dir / filename
        try:
            result: dict[str, Any] = json.loads(path.read_text())
            return result
        except json.JSONDecodeError as exc:
            raise CorruptArtifactError(
                f"Could not parse {filename} for {model_name}/{version}: {exc}"
            ) from exc
