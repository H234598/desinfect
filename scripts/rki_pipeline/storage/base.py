#!/usr/bin/env python3
"""Immutable backend-neutral storage contracts."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from scripts.rki_pipeline.io_utils import normalize_posix_path
from scripts.rki_pipeline.run_modes import EffectLedger

_SHA256 = frozenset("0123456789abcdef")
_VISIBILITY = frozenset({"public", "repository_authorized", "internal", "restricted"})
_RIGHTS = frozenset({"approved", "metadata_only", "internal_only", "unknown", "takedown"})


class StorageError(RuntimeError):
    """A storage operation cannot satisfy its integrity contract."""


class StorageConfigurationError(StorageError):
    """Storage configuration is unknown, malformed, or incomplete."""


class StorageBackend(StrEnum):
    """The only supported durable archive backends."""

    LFS = "lfs"
    RELEASE = "release"
    OBJECT = "object"


def _valid_sha256(value: str) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _SHA256


def _validate_common(
    *, artifact_id: str, logical_key: str, sha256: str, size: int,
    visibility: str, rights_state: str,
) -> None:
    if type(artifact_id) is not str or len(artifact_id) < 3:
        raise ValueError("artifact_id muss mindestens drei Zeichen besitzen")
    normalize_posix_path(logical_key)
    if not _valid_sha256(sha256):
        raise ValueError("sha256 muss ein kleingeschriebener SHA-256 sein")
    if type(size) is not int or size < 0:
        raise ValueError("size muss eine nichtnegative Ganzzahl sein")
    if visibility not in _VISIBILITY:
        raise ValueError("Unbekannte Sichtbarkeit")
    if rights_state not in _RIGHTS:
        raise ValueError("Unbekannter Rechtezustand")


def hash_file(path: Path) -> tuple[int, str]:
    """Stream one regular file and return size plus SHA-256."""

    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise StorageError(f"Storagequelle ist keine reguläre Datei: {path}")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


@dataclass(frozen=True, slots=True)
class StorageIntent:
    """One immutable source object and its logical backend-neutral key."""

    artifact_id: str
    logical_key: str
    source_path: Path
    sha256: str
    size: int
    visibility: str
    rights_state: str

    def __post_init__(self) -> None:
        _validate_common(
            artifact_id=self.artifact_id,
            logical_key=self.logical_key,
            sha256=self.sha256,
            size=self.size,
            visibility=self.visibility,
            rights_state=self.rights_state,
        )
        if not isinstance(self.source_path, Path):
            raise ValueError("source_path muss ein pathlib.Path sein")

    @classmethod
    def from_path(
        cls, source_path: Path, *, artifact_id: str, logical_key: str,
        visibility: str, rights_state: str,
    ) -> StorageIntent:
        size, sha256 = hash_file(source_path)
        return cls(
            artifact_id=artifact_id,
            logical_key=normalize_posix_path(logical_key),
            source_path=Path(source_path),
            sha256=sha256,
            size=size,
            visibility=visibility,
            rights_state=rights_state,
        )


@dataclass(frozen=True, slots=True)
class PreparedObject:
    """A verified materialized object constrained to one temporary root."""

    artifact_id: str
    logical_key: str
    path: Path
    temp_root: Path
    sha256: str
    size: int
    visibility: str
    rights_state: str

    def __post_init__(self) -> None:
        _validate_common(
            artifact_id=self.artifact_id,
            logical_key=self.logical_key,
            sha256=self.sha256,
            size=self.size,
            visibility=self.visibility,
            rights_state=self.rights_state,
        )
        root = Path(os.path.abspath(self.temp_root))
        path = Path(os.path.abspath(self.path))
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("PreparedObject liegt außerhalb temp_root") from exc
        if path.exists():
            measured_size, measured_hash = hash_file(path)
            if (measured_size, measured_hash) != (self.size, self.sha256):
                raise StorageError("PreparedObject stimmt nicht mit Größe/SHA-256 überein")


@dataclass(frozen=True, slots=True)
class StorageReference:
    """Schema-compatible durable reference independent of backend internals."""

    artifact_id: str
    relative_path: str
    storage_backend: StorageBackend
    storage_object_id: str
    sha256: str
    size: int
    visibility: str
    rights_state: str
    public_reference: str | None

    def __post_init__(self) -> None:
        _validate_common(
            artifact_id=self.artifact_id,
            logical_key=self.relative_path,
            sha256=self.sha256,
            size=self.size,
            visibility=self.visibility,
            rights_state=self.rights_state,
        )
        if not isinstance(self.storage_backend, StorageBackend):
            raise ValueError("storage_backend muss ein StorageBackend sein")
        if type(self.storage_object_id) is not str or not self.storage_object_id:
            raise ValueError("storage_object_id darf nicht leer sein")
        if self.public_reference is not None and type(self.public_reference) is not str:
            raise ValueError("public_reference muss String oder None sein")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "storage_backend": self.storage_backend.value,
            "storage_object_id": self.storage_object_id,
            "sha256": self.sha256,
            "bytes": self.size,
            "visibility": self.visibility,
            "rights_state": self.rights_state,
            "public_reference": self.public_reference,
        }


@runtime_checkable
class StorageAdapter(Protocol):
    """Required behavior of every testable and migratable backend."""

    backend: StorageBackend

    def exists(self, intent: StorageIntent) -> StorageReference | None: ...
    def materialize(self, intent: StorageIntent, *, temp_root: Path, ledger: EffectLedger) -> PreparedObject: ...
    def export(self, reference: StorageReference, *, temp_root: Path, ledger: EffectLedger) -> PreparedObject: ...
    def apply(self, prepared: PreparedObject, *, ledger: EffectLedger) -> StorageReference: ...
    def verify(self, reference: StorageReference) -> None: ...
    def list_references(self) -> tuple[StorageReference, ...]: ...
