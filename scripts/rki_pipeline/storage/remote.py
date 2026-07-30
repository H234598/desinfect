#!/usr/bin/env python3
"""Shared offline-testable implementation for injected remote storage ports."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from scripts.rki_pipeline.io_utils import atomic_write_bytes, normalize_posix_path
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageBackend,
    StorageError,
    StorageIntent,
    StorageReference,
    hash_file,
)


@runtime_checkable
class RemoteClient(Protocol):
    """Minimal client port implemented by release and object integrations."""

    def head(self, key: str) -> dict[str, Any] | None: ...
    def put(self, key: str, source_path: Path, sha256: str, size: int) -> str | None: ...
    def get(self, key: str, target_path: Path) -> None: ...
    def list(self, prefix: str) -> tuple[dict[str, Any], ...]: ...


class RemoteStorageAdapter:
    """Mode-aware, idempotent adapter over one injected remote client."""

    backend: StorageBackend
    effect_kind: EffectKind

    def __init__(self, *, client: RemoteClient, prefix: str, object_prefix: str) -> None:
        if not isinstance(client, RemoteClient):
            raise TypeError("client erfüllt das RemoteClient-Protokoll nicht")
        self.client = client
        self.prefix = normalize_posix_path(prefix)
        self.object_prefix = object_prefix

    def _key(self, logical_key: str) -> str:
        normalized = normalize_posix_path(logical_key)
        if normalized == self.prefix or normalized.startswith(f"{self.prefix}/"):
            return normalized
        return normalize_posix_path(f"{self.prefix}/{normalized}")

    def _reference(self, *, artifact_id: str, key: str, sha256: str, size: int, visibility: str, rights_state: str, public_reference: str | None) -> StorageReference:
        return StorageReference(
            artifact_id=artifact_id,
            relative_path=key,
            storage_backend=self.backend,
            storage_object_id=f"{self.object_prefix}:{key}",
            sha256=sha256,
            size=size,
            visibility=visibility,
            rights_state=rights_state,
            public_reference=public_reference,
        )

    def _reference_from_metadata(self, key: str, metadata: dict[str, Any], *, fallback: StorageIntent | PreparedObject | None = None) -> StorageReference:
        def value(name: str) -> Any:
            if name in metadata:
                return metadata[name]
            if fallback is not None:
                return getattr(fallback, name)
            raise StorageError(f"Remote-Metadaten fehlen: {name}")

        return self._reference(
            artifact_id=value("artifact_id"),
            key=key,
            sha256=value("sha256"),
            size=value("size"),
            visibility=value("visibility"),
            rights_state=value("rights_state"),
            public_reference=metadata.get("public_reference"),
        )

    def exists(self, intent: StorageIntent) -> StorageReference | None:
        key = self._key(intent.logical_key)
        metadata = self.client.head(key)
        if metadata is None:
            return None
        reference = self._reference_from_metadata(key, metadata, fallback=intent)
        if (reference.sha256, reference.size) != (intent.sha256, intent.size):
            raise StorageError(f"Remote-Konflikt für {key}")
        return reference

    def materialize(self, intent: StorageIntent, *, temp_root: Path, ledger: EffectLedger) -> PreparedObject:
        if ledger.mode is not RunMode.MATERIALIZE:
            raise StorageError("Remote-Materialisierung benötigt RunMode materialize")
        target = Path(temp_root) / normalize_posix_path(intent.logical_key)
        atomic_write_bytes(target, intent.source_path.read_bytes(), allowed_root=Path(temp_root))
        ledger.record(EffectKind.TEMP_FILE, target.absolute().as_posix(), sha256=intent.sha256, size=intent.size)
        return PreparedObject(
            artifact_id=intent.artifact_id,
            logical_key=intent.logical_key,
            path=target,
            temp_root=Path(temp_root),
            sha256=intent.sha256,
            size=intent.size,
            visibility=intent.visibility,
            rights_state=intent.rights_state,
        )

    def export(self, reference: StorageReference, *, temp_root: Path, ledger: EffectLedger) -> PreparedObject:
        """Download one verified reference only into the explicit temp root."""

        if ledger.mode is not RunMode.MATERIALIZE:
            raise StorageError("Remote-Export benötigt RunMode materialize")
        if reference.storage_backend is not self.backend:
            raise StorageError("Referenz gehört zu einem anderen Backend")
        target = Path(temp_root) / normalize_posix_path(reference.relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(f".{target.name}.export.part")
        if part.exists():
            part.unlink()
        self.client.get(reference.relative_path, part)
        size, sha256 = hash_file(part)
        if (size, sha256) != (reference.size, reference.sha256):
            part.unlink(missing_ok=True)
            raise StorageError("Exportiertes Remote-Objekt besitzt falsche Größe/SHA-256")
        atomic_write_bytes(target, part.read_bytes(), allowed_root=Path(temp_root))
        part.unlink(missing_ok=True)
        ledger.record(EffectKind.TEMP_FILE, target.absolute().as_posix(), sha256=sha256, size=size)
        return PreparedObject(
            artifact_id=reference.artifact_id,
            logical_key=reference.relative_path,
            path=target,
            temp_root=Path(temp_root),
            sha256=sha256,
            size=size,
            visibility=reference.visibility,
            rights_state=reference.rights_state,
        )

    def apply(self, prepared: PreparedObject, *, ledger: EffectLedger) -> StorageReference:
        if ledger.mode is not RunMode.APPLY:
            raise StorageError("Remote-Publikation benötigt RunMode apply")
        key = self._key(prepared.logical_key)
        metadata = self.client.head(key)
        if metadata is not None:
            reference = self._reference_from_metadata(key, metadata, fallback=prepared)
            if (reference.sha256, reference.size) != (prepared.sha256, prepared.size):
                raise StorageError(f"Remote-Konflikt für {key}")
            return reference
        public_reference = self.client.put(key, prepared.path, prepared.sha256, prepared.size)
        ledger.record(self.effect_kind, key, sha256=prepared.sha256, size=prepared.size)
        reference = self._reference(
            artifact_id=prepared.artifact_id,
            key=key,
            sha256=prepared.sha256,
            size=prepared.size,
            visibility=prepared.visibility,
            rights_state=prepared.rights_state,
            public_reference=public_reference,
        )
        self.verify(reference)
        return reference

    def verify(self, reference: StorageReference) -> None:
        if reference.storage_backend is not self.backend:
            raise StorageError("Referenz gehört zu einem anderen Backend")
        metadata = self.client.head(reference.relative_path)
        if metadata is None:
            raise StorageError(f"Remote-Objekt fehlt: {reference.relative_path}")
        if (metadata.get("sha256"), metadata.get("size")) != (reference.sha256, reference.size):
            raise StorageError(f"Remote-Objektintegrität weicht ab: {reference.relative_path}")

    def list_references(self) -> tuple[StorageReference, ...]:
        references: list[StorageReference] = []
        for metadata in self.client.list(self.prefix):
            if not isinstance(metadata, dict) or type(metadata.get("key")) is not str:
                raise StorageError("Remote-Liste enthält ungültige Metadaten")
            references.append(self._reference_from_metadata(metadata["key"], metadata))
        return tuple(sorted(references, key=lambda reference: reference.artifact_id))
