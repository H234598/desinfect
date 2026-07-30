#!/usr/bin/env python3
"""Fail-closed construction of configured storage adapters."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.rki_pipeline.storage.base import (
    StorageAdapter,
    StorageBackend,
    StorageConfigurationError,
)
from scripts.rki_pipeline.storage.config import StorageConfig


def build_storage_adapter(
    config: StorageConfig,
    *,
    backend: StorageBackend | None = None,
    repository_root: Path,
    release_client: Any | None = None,
    object_client: Any | None = None,
) -> StorageAdapter:
    """Build exactly one adapter, requiring injected clients for remote backends."""

    selected = config.backend if backend is None else backend
    if not isinstance(selected, StorageBackend):
        raise StorageConfigurationError("backend muss ein StorageBackend sein")
    if selected is StorageBackend.LFS:
        from scripts.rki_pipeline.storage.lfs import LfsStorageAdapter

        return LfsStorageAdapter(
            repository_root=Path(repository_root),
            config=config.lfs,
        )
    if selected is StorageBackend.RELEASE:
        if release_client is None:
            raise StorageConfigurationError("ReleaseClient muss injiziert werden")
        from scripts.rki_pipeline.storage.release import ReleaseStorageAdapter

        return ReleaseStorageAdapter(config.release, release_client)
    if selected is StorageBackend.OBJECT:
        if object_client is None:
            raise StorageConfigurationError("ObjectClient muss injiziert werden")
        from scripts.rki_pipeline.storage.object import ObjectStorageAdapter

        return ObjectStorageAdapter(config.object, object_client)
    raise StorageConfigurationError(f"Unbekanntes Storage-Backend: {selected}")
