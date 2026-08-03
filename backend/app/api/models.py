from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app.config import settings
from app.registry import (
    ArtifactNotFoundError,
    CorruptArtifactError,
    LocalModelRegistry,
    ModelNotFoundError,
)
from app.registry.base import ModelRegistry

router = APIRouter(prefix="/api/models", tags=["models"])


def get_registry() -> ModelRegistry:
    return LocalModelRegistry(settings.model_artifacts_dir)


RegistryDep = Annotated[ModelRegistry, Depends(get_registry)]


@router.get("")
def list_models(registry: RegistryDep) -> list[str]:
    return registry.list_models()


@router.get("/{model_name}/versions")
def list_versions(model_name: str, registry: RegistryDep) -> list[str]:
    try:
        return registry.list_versions(model_name)
    except ModelNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{model_name}/versions/{version}")
def get_version_metadata(
    model_name: str, version: str, registry: RegistryDep
) -> dict[str, Any]:
    try:
        return registry.get_model_metadata(model_name, version)
    except (ModelNotFoundError, ArtifactNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CorruptArtifactError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{model_name}/versions/{version}/evaluation")
def get_version_evaluation(
    model_name: str, version: str, registry: RegistryDep
) -> dict[str, Any]:
    try:
        return registry.get_evaluation(model_name, version)
    except (ModelNotFoundError, ArtifactNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except CorruptArtifactError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
