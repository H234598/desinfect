#!/usr/bin/env python3
"""Shared offline-testable implementation for injected remote storage ports."""
from __future__ import annotations

from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any, Protocol, runtime_checkable

from scripts.rki_pipeline.io_utils import atomic_write_bytes, normalize_posix_path
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    RightsStorageAuthorizer,
    StorageBackend,
    StorageError,
    StorageIntent,
    StorageReference,
    authorize_storage_operation,
    hash_file,
    read_verified_payload,
)

_REMOTE_METADATA_FIELDS = frozenset(
    {
        "schema_version",
        "artifact_id",
        "sha256",
        "size",
        "source_id",
        "source_sha256",
        "document_id",
        "conversion_id",
        "decision_sha256",
        "provenance_state",
        "visibility",
        "rights_state",
        "public_reference",
    }
)


@runtime_checkable
class RemoteClient(Protocol):
    """Minimal client port implemented by release and object integrations."""

    def head(self, key: str) -> dict[str, Any] | None: ...
    def put(
        self,
        key: str,
        source_path: Path,
        metadata: dict[str, object],
    ) -> str | None: ...
    def get(self, key: str, target_path: Path) -> None: ...
    def list(self, prefix: str) -> tuple[dict[str, Any], ...]: ...


class RemoteStorageAdapter:
    """Mode-aware, idempotent adapter over one injected remote client."""

    backend: StorageBackend
    effect_kind: EffectKind

    def __init__(
        self,
        *,
        client: RemoteClient,
        prefix: str,
        object_prefix: str,
        authorizer: RightsStorageAuthorizer,
    ) -> None:
        if not isinstance(client, RemoteClient):
            raise StorageError("client erfüllt das RemoteClient-Protokoll nicht")
        self.client = client
        self.prefix = normalize_posix_path(prefix)
        self.object_prefix = object_prefix
        if type(authorizer) is not RightsStorageAuthorizer:
            raise StorageError(
                "authorizer muss ein exakter RightsStorageAuthorizer sein"
            )
        self.authorizer = authorizer

    def authorize(
        self,
        subject: StorageIntent | PreparedObject | StorageReference,
        *,
        operation: str,
    ) -> None:
        authorize_storage_operation(
            self.authorizer,
            subject,
            operation=operation,
        )

    def _key(self, logical_key: str) -> str:
        normalized = normalize_posix_path(logical_key)
        if normalized == self.prefix or normalized.startswith(f"{self.prefix}/"):
            return normalized
        return normalize_posix_path(f"{self.prefix}/{normalized}")

    def _reference(
        self,
        *,
        artifact_id: str,
        key: str,
        sha256: str,
        size: int,
        source_id: str | None,
        source_sha256: str | None,
        document_id: str | None,
        conversion_id: str | None,
        decision_sha256: str | None,
        provenance_state: str,
        visibility: str,
        rights_state: str,
        public_reference: str | None,
    ) -> StorageReference:
        return StorageReference(
            artifact_id=artifact_id,
            relative_path=key,
            storage_backend=self.backend,
            storage_object_id=f"{self.object_prefix}:{key}",
            sha256=sha256,
            size=size,
            source_id=source_id,
            source_sha256=source_sha256,
            document_id=document_id,
            conversion_id=conversion_id,
            decision_sha256=decision_sha256,
            provenance_state=provenance_state,
            visibility=visibility,
            rights_state=rights_state,
            public_reference=public_reference,
        )

    def _reference_from_metadata(
        self,
        key: str,
        metadata: dict[str, Any],
        *,
        listed: bool = False,
    ) -> StorageReference:
        if not isinstance(metadata, dict):
            raise StorageError("Remote-Metadaten sind kein Objekt")
        expected = _REMOTE_METADATA_FIELDS | ({"key"} if listed else set())
        if set(metadata) != expected or metadata.get("schema_version") != "1.1.0":
            raise StorageError("Remote-Metadaten erfüllen Vertrag 1.1.0 nicht")
        try:
            return self._reference(
                artifact_id=metadata["artifact_id"],
                key=key,
                sha256=metadata["sha256"],
                size=metadata["size"],
                source_id=metadata["source_id"],
                source_sha256=metadata["source_sha256"],
                document_id=metadata["document_id"],
                conversion_id=metadata["conversion_id"],
                decision_sha256=metadata["decision_sha256"],
                provenance_state=metadata["provenance_state"],
                visibility=metadata["visibility"],
                rights_state=metadata["rights_state"],
                public_reference=metadata["public_reference"],
            )
        except (TypeError, ValueError) as exc:
            raise StorageError("Remote-Metadaten sind ungültig") from exc

    @staticmethod
    def _metadata(subject: StorageIntent | PreparedObject) -> dict[str, object]:
        return {
            "schema_version": "1.1.0",
            "artifact_id": subject.artifact_id,
            "sha256": subject.sha256,
            "size": subject.size,
            "source_id": subject.source_id,
            "source_sha256": subject.source_sha256,
            "document_id": subject.document_id,
            "conversion_id": subject.conversion_id,
            "decision_sha256": subject.decision_sha256,
            "provenance_state": "current",
            "visibility": subject.visibility,
            "rights_state": subject.rights_state,
            "public_reference": None,
        }

    def _expected_reference(
        self,
        subject: StorageIntent | PreparedObject,
        *,
        key: str,
        public_reference: str | None,
    ) -> StorageReference:
        return self._reference(
            artifact_id=subject.artifact_id,
            key=key,
            sha256=subject.sha256,
            size=subject.size,
            source_id=subject.source_id,
            source_sha256=subject.source_sha256,
            document_id=subject.document_id,
            conversion_id=subject.conversion_id,
            decision_sha256=subject.decision_sha256,
            provenance_state="current",
            visibility=subject.visibility,
            rights_state=subject.rights_state,
            public_reference=public_reference,
        )

    def exists(self, intent: StorageIntent) -> StorageReference | None:
        self.authorize(intent, operation="exists")
        key = self._key(intent.logical_key)
        metadata = self.client.head(key)
        if metadata is None:
            return None
        reference = self._reference_from_metadata(key, metadata)
        expected = self._expected_reference(
            intent,
            key=key,
            public_reference=reference.public_reference,
        )
        if reference != expected:
            raise StorageError(f"Remote-Konflikt für {key}")
        return reference

    def materialize(self, intent: StorageIntent, *, temp_root: Path, ledger: EffectLedger) -> PreparedObject:
        if ledger.mode is not RunMode.MATERIALIZE:
            raise StorageError("Remote-Materialisierung benötigt RunMode materialize")
        self.authorize(intent, operation="materialize")
        payload = read_verified_payload(
            intent.source_path,
            sha256=intent.sha256,
            size=intent.size,
        )
        target = Path(temp_root) / normalize_posix_path(intent.logical_key)
        atomic_write_bytes(target, payload, allowed_root=Path(temp_root))
        ledger.record(EffectKind.TEMP_FILE, target.absolute().as_posix(), sha256=intent.sha256, size=intent.size)
        return PreparedObject(
            artifact_id=intent.artifact_id,
            logical_key=intent.logical_key,
            path=target,
            temp_root=Path(temp_root),
            sha256=intent.sha256,
            size=intent.size,
            source_id=intent.source_id,
            source_sha256=intent.source_sha256,
            decision_sha256=intent.decision_sha256,
            document_id=intent.document_id,
            conversion_id=intent.conversion_id,
            visibility=intent.visibility,
            rights_state=intent.rights_state,
        )

    def export(self, reference: StorageReference, *, temp_root: Path, ledger: EffectLedger) -> PreparedObject:
        """Download one verified reference only into the explicit temp root."""

        if ledger.mode is not RunMode.MATERIALIZE:
            raise StorageError("Remote-Export benötigt RunMode materialize")
        if reference.storage_backend is not self.backend:
            raise StorageError("Referenz gehört zu einem anderen Backend")
        self.authorize(reference, operation="export")
        target = Path(temp_root) / normalize_posix_path(reference.relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            prefix=f".{target.name}.export-",
            suffix=".part",
            dir=target.parent,
            delete=False,
        ) as handle:
            part = Path(handle.name)
        try:
            self.client.get(reference.relative_path, part)
            payload = read_verified_payload(
                part,
                sha256=reference.sha256,
                size=reference.size,
            )
            atomic_write_bytes(target, payload, allowed_root=Path(temp_root))
        finally:
            part.unlink(missing_ok=True)
        ledger.record(
            EffectKind.TEMP_FILE,
            target.absolute().as_posix(),
            sha256=reference.sha256,
            size=reference.size,
        )
        return PreparedObject(
            artifact_id=reference.artifact_id,
            logical_key=reference.relative_path,
            path=target,
            temp_root=Path(temp_root),
            sha256=reference.sha256,
            size=reference.size,
            source_id=reference.source_id,
            source_sha256=reference.source_sha256,
            decision_sha256=reference.decision_sha256,
            document_id=reference.document_id,
            conversion_id=reference.conversion_id,
            visibility=reference.visibility,
            rights_state=reference.rights_state,
        )

    @staticmethod
    def _snapshot_prepared(prepared: PreparedObject, directory: Path) -> tuple[Path, int, str]:
        source = Path(prepared.path)
        if source.is_symlink() or not source.is_file():
            raise StorageError("Vorbereitetes Remote-Objekt ist keine reguläre Datei")
        payload = source.read_bytes()
        snapshot = directory / "payload.bin"
        atomic_write_bytes(snapshot, payload, allowed_root=directory)
        size, sha256 = hash_file(snapshot)
        if (size, sha256) != (prepared.size, prepared.sha256):
            raise StorageError(
                "Vorbereitetes Remote-Objekt besitzt falsche Größe/SHA-256"
            )
        return snapshot, size, sha256

    def apply(self, prepared: PreparedObject, *, ledger: EffectLedger) -> StorageReference:
        if ledger.mode is not RunMode.APPLY:
            raise StorageError("Remote-Publikation benötigt RunMode apply")
        self.authorize(prepared, operation="apply")
        key = self._key(prepared.logical_key)
        metadata = self.client.head(key)
        if metadata is not None:
            reference = self._reference_from_metadata(key, metadata)
            expected = self._expected_reference(
                prepared,
                key=key,
                public_reference=reference.public_reference,
            )
            if reference != expected:
                raise StorageError(f"Remote-Konflikt für {key}")
            self.verify(reference)
            return reference

        with TemporaryDirectory(prefix="desinfect-remote-upload-") as temporary:
            snapshot, measured_size, measured_hash = self._snapshot_prepared(
                prepared,
                Path(temporary),
            )
            public_reference = self.client.put(
                key,
                snapshot,
                self._metadata(prepared),
            )
        ledger.record(
            self.effect_kind,
            key,
            sha256=measured_hash,
            size=measured_size,
        )
        reference = self._reference(
            artifact_id=prepared.artifact_id,
            key=key,
            sha256=measured_hash,
            size=measured_size,
            source_id=prepared.source_id,
            source_sha256=prepared.source_sha256,
            document_id=prepared.document_id,
            conversion_id=prepared.conversion_id,
            decision_sha256=prepared.decision_sha256,
            provenance_state="current",
            visibility=prepared.visibility,
            rights_state=prepared.rights_state,
            public_reference=public_reference,
        )
        self.verify(reference)
        return reference

    def verify(self, reference: StorageReference) -> None:
        if reference.storage_backend is not self.backend:
            raise StorageError("Referenz gehört zu einem anderen Backend")
        self.authorize(reference, operation="verify")
        metadata = self.client.head(reference.relative_path)
        if metadata is None:
            raise StorageError(f"Remote-Objekt fehlt: {reference.relative_path}")
        remote_reference = self._reference_from_metadata(reference.relative_path, metadata)
        if remote_reference != reference:
            raise StorageError(f"Remote-Metadaten driften: {reference.relative_path}")
        with TemporaryDirectory(prefix="desinfect-remote-verify-") as temporary:
            target = Path(temporary) / "payload.bin"
            self.client.get(reference.relative_path, target)
            measured_size, measured_hash = hash_file(target)
        if (measured_hash, measured_size) != (reference.sha256, reference.size):
            raise StorageError(
                f"Remote-Objektintegrität weicht ab: {reference.relative_path}"
            )

    def list_references(self) -> tuple[StorageReference, ...]:
        references: list[StorageReference] = []
        for metadata in self.client.list(self.prefix):
            if not isinstance(metadata, dict) or type(metadata.get("key")) is not str:
                raise StorageError("Remote-Liste enthält ungültige Metadaten")
            references.append(
                self._reference_from_metadata(metadata["key"], metadata, listed=True)
            )
        return tuple(sorted(references, key=lambda reference: reference.artifact_id))
