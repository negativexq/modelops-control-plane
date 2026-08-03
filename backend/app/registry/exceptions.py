class ModelNotFoundError(Exception):
    """Raised when a model name is not present in the registry."""


class ArtifactNotFoundError(Exception):
    """Raised when a model version or one of its required artifact files is missing."""


class CorruptArtifactError(Exception):
    """Raised when an artifact file exists but cannot be parsed."""
