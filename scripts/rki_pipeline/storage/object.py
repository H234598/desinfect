#!/usr/bin/env python3
"""Object storage adapter without direct cloud SDK or network dependencies."""
from __future__ import annotations

from scripts.rki_pipeline.run_modes import EffectKind
from scripts.rki_pipeline.storage.base import StorageBackend
from scripts.rki_pipeline.storage.config import ObjectConfig
from scripts.rki_pipeline.storage.remote import RemoteClient, RemoteStorageAdapter


class ObjectStorageAdapter(RemoteStorageAdapter):
    """Store namespaced objects through one injected ObjectClient-compatible port."""

    backend = StorageBackend.OBJECT
    effect_kind = EffectKind.OBJECT

    def __init__(self, config: ObjectConfig, client: RemoteClient) -> None:
        self.config = config
        super().__init__(
            client=client,
            prefix=config.namespace,
            object_prefix=f"object:{config.bucket}",
        )
