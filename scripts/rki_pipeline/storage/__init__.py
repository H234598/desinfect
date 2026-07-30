"""Backend-neutral storage contracts and adapters for the RKI pipeline."""

from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageAdapter,
    StorageBackend,
    StorageConfigurationError,
    StorageError,
    StorageIntent,
    StorageReference,
)

__all__ = [
    "PreparedObject",
    "StorageAdapter",
    "StorageBackend",
    "StorageConfigurationError",
    "StorageError",
    "StorageIntent",
    "StorageReference",
]
