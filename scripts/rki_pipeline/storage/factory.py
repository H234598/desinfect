#!/usr/bin/env python3
"""Fail-closed construction of configured storage adapters."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.rki_pipeline.storage.base import (
    StorageAdapter,
    StorageAuthorizer,
    StorageBackend,
    StorageConfigurationError,
)
from scripts.rki_pipeline.storage.config import StorageConfig


def build_storage_adapter(
    config: StorageConfig,
    *,
    backend: StorageBackend | None = None,
    repository_root: Path,
    authorizer: StorageAuthorizer,
    release_client: Any | None = None,
    object_client: Any | None = None,
) -> StorageAdapter:
    """Build exactly one adapter, requiring injected clients for remote backends."""

    selected = config.backend if backend is None else backend
    if not isinstance(selected, StorageBackend):
        raise StorageConfigurationError("backend muss ein StorageBackend sein")
    if not isinstance(authorizer, StorageAuthorizer):
        raise StorageConfigurationError(
            "authorizer erfüllt das StorageAuthorizer-Protokoll nicht"
        )
    if selected is StorageBackend.LFS:
        from scripts.rki_pipeline.storage.lfs import LfsStorageAdapter

        return LfsStorageAdapter(
            repository_root=Path(repository_root),
            config=config.lfs,
            authorizer=authorizer,
        )
    if selected is StorageBackend.RELEASE:
        if release_client is None:
            raise StorageConfigurationError("ReleaseClient muss injiziert werden")
        from scripts.rki_pipeline.storage.release import ReleaseStorageAdapter

        return ReleaseStorageAdapter(config.release, release_client, authorizer)
    if selected is StorageBackend.OBJECT:
        if object_client is None:
            raise StorageConfigurationError("ObjectClient muss injiziert werden")
        from scripts.rki_pipeline.storage.object import ObjectStorageAdapter

        return ObjectStorageAdapter(config.object, object_client, authorizer)
    raise StorageConfigurationError(f"Unbekanntes Storage-Backend: {selected}")
