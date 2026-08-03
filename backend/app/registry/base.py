from abc import ABC, abstractmethod
from typing import Any


class ModelRegistry(ABC):
    """Interface for looking up trained model versions and their metadata."""

    @abstractmethod
    def list_models(self) -> list[str]:
        """Return the names of every model tracked by the registry."""

    @abstractmethod
    def list_versions(self, model_name: str) -> list[str]:
        """Return the versions available for a given model."""

    @abstractmethod
    def get_model_metadata(self, model_name: str, version: str) -> dict[str, Any]:
        """Return the metadata document for a specific model version."""

    @abstractmethod
    def get_evaluation(self, model_name: str, version: str) -> dict[str, Any]:
        """Return the evaluation metrics document for a specific model version."""
