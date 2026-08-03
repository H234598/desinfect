#!/usr/bin/env python3
"""Release asset adapter without direct GitHub SDK or network dependencies."""
from __future__ import annotations

from scripts.rki_pipeline.run_modes import EffectKind
from scripts.rki_pipeline.storage.base import RightsStorageAuthorizer, StorageBackend
from scripts.rki_pipeline.storage.config import ReleaseConfig
from scripts.rki_pipeline.storage.remote import RemoteClient, RemoteStorageAdapter


class ReleaseStorageAdapter(RemoteStorageAdapter):
    """Store versioned assets through one injected ReleaseClient-compatible port."""

    backend = StorageBackend.RELEASE
    effect_kind = EffectKind.RELEASE

    def __init__(
        self,
        config: ReleaseConfig,
        client: RemoteClient,
        authorizer: RightsStorageAuthorizer,
    ) -> None:
        self.config = config
        super().__init__(
            client=client,
            prefix=config.asset_prefix,
            object_prefix=f"release:{config.tag_prefix}",
            authorizer=authorizer,
        )
