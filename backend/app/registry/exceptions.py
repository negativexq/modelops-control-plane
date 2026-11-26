class RegistryError(Exception):
    """Base class for every registry lookup failure."""


class ModelNotFoundError(RegistryError):
    """Raised when a model name is not present in the registry."""


class ArtifactNotFoundError(RegistryError):
    """Raised when a model version or one of its required artifact files is missing."""


class CorruptArtifactError(RegistryError):
    """Raised when an artifact file exists but cannot be parsed."""
