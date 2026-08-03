from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
from sklearn.pipeline import Pipeline

from app.registry.local import LocalModelRegistry


def normalize_class_weight(value: Any) -> Any:
    """Convert JSON-deserialized class_weight dict keys back to int.

    json.dump()/json.load() round-trips dict keys through strings, so a
    class_weight={0: 1, 1: 150} hyperparameter is stored (and read back) as
    {"0": 1, "1": 150} in metadata.json. Callers that need to reuse the value
    (rather than just display it) should go through this helper.
    """
    is_int_keyed = isinstance(value, dict) and all(
        isinstance(k, str) and k.lstrip("-").isdigit() for k in value
    )
    if is_int_keyed:
        return {int(k): v for k, v in value.items()}
    return value


@dataclass
class LoadedModel:
    pipeline: Pipeline
    metadata: dict[str, Any]
    evaluation: dict[str, Any]

    @property
    def features(self) -> list[str]:
        features: list[str] = self.metadata["features"]
        return features


def load_model(artifacts_dir: Path, model_name: str, version: str) -> LoadedModel:
    registry = LocalModelRegistry(artifacts_dir)
    metadata = registry.get_model_metadata(model_name, version)
    evaluation = registry.get_evaluation(model_name, version)
    model_path = registry.get_model_path(model_name, version)
    pipeline: Pipeline = joblib.load(model_path)
    return LoadedModel(pipeline=pipeline, metadata=metadata, evaluation=evaluation)
