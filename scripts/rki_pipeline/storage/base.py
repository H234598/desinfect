#!/usr/bin/env python3
"""Immutable backend-neutral storage contracts."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import os
from pathlib import Path
import re
from typing import Protocol, runtime_checkable

from scripts.rki_pipeline.io_utils import normalize_posix_path
from scripts.rki_pipeline.run_modes import EffectLedger

_SHA256 = frozenset("0123456789abcdef")
_VISIBILITY = frozenset({"public", "repository_authorized", "internal", "restricted"})
_RIGHTS = frozenset({"approved", "metadata_only", "internal_only", "unknown", "takedown"})
_PROVENANCE_STATES = frozenset({"current", "legacy_needs_review"})
_SOURCE_ID = re.compile(
    r"^rki:176904/(?P<number>[0-9]+)(?:\.(?P<version>[2-9]|[1-9][0-9]+))?$"
)
_DOCUMENT_ID = re.compile(
    r"^rki-176904-(?P<number>[0-9]+)-v(?P<version>[1-9][0-9]*)$"
)
_CONVERSION_ID = re.compile(r"^conv-[0-9a-f]{64}$")


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


def _validate_authorization_provenance(
    *,
    source_id: str | None,
    source_sha256: str | None,
    decision_sha256: str | None,
    required: bool,
) -> None:
    if source_id is None:
        if required:
            raise ValueError("source_id fehlt für current-Provenienz")
    elif type(source_id) is not str or _SOURCE_ID.fullmatch(source_id) is None:
        raise ValueError("source_id ist keine kanonische RKI-Quell-ID")
    if source_sha256 is None:
        if required:
            raise ValueError("source_sha256 fehlt für current-Provenienz")
    elif not _valid_sha256(source_sha256):
        raise ValueError("source_sha256 muss ein kleingeschriebener SHA-256 sein")
    if decision_sha256 is None:
        if required:
            raise ValueError("decision_sha256 fehlt für current-Provenienz")
    elif not _valid_sha256(decision_sha256):
        raise ValueError("decision_sha256 muss ein kleingeschriebener SHA-256 sein")


def _validate_nullable_id(value: str | None, *, name: str, pattern: re.Pattern[str]) -> None:
    if value is not None and (type(value) is not str or pattern.fullmatch(value) is None):
        raise ValueError(f"{name} ist nicht kanonisch")


def validate_storage_provenance_relationship(
    source_id: str | None,
    document_id: str | None,
) -> None:
    """Require an optional document link to match exact source handle and version."""

    if document_id is None:
        return
    source = _SOURCE_ID.fullmatch(source_id) if isinstance(source_id, str) else None
    document = _DOCUMENT_ID.fullmatch(document_id)
    if source is None or document is None:
        raise ValueError("source_id und document_id sind nicht kanonisch verknüpft")
    source_version = int(source.group("version") or "1")
    if (
        source.group("number") != document.group("number")
        or source_version != int(document.group("version"))
    ):
        raise ValueError("source_id und document_id müssen Handle und Version teilen")


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
    source_id: str
    source_sha256: str
    decision_sha256: str
    visibility: str
    rights_state: str
    document_id: str | None = None
    conversion_id: str | None = None

    def __post_init__(self) -> None:
        _validate_common(
            artifact_id=self.artifact_id,
            logical_key=self.logical_key,
            sha256=self.sha256,
            size=self.size,
            visibility=self.visibility,
            rights_state=self.rights_state,
        )
        _validate_authorization_provenance(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            decision_sha256=self.decision_sha256,
            required=True,
        )
        _validate_nullable_id(self.document_id, name="document_id", pattern=_DOCUMENT_ID)
        _validate_nullable_id(self.conversion_id, name="conversion_id", pattern=_CONVERSION_ID)
        if not isinstance(self.source_path, Path):
            raise ValueError("source_path muss ein pathlib.Path sein")

    @classmethod
    def from_path(
        cls, source_path: Path, *, artifact_id: str, logical_key: str,
        source_id: str, source_sha256: str, decision_sha256: str,
        visibility: str, rights_state: str,
        document_id: str | None = None, conversion_id: str | None = None,
    ) -> StorageIntent:
        size, sha256 = hash_file(source_path)
        return cls(
            artifact_id=artifact_id,
            logical_key=normalize_posix_path(logical_key),
            source_path=Path(source_path),
            sha256=sha256,
            size=size,
            source_id=source_id,
            source_sha256=source_sha256,
            decision_sha256=decision_sha256,
            visibility=visibility,
            rights_state=rights_state,
            document_id=document_id,
            conversion_id=conversion_id,
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
    source_id: str
    source_sha256: str
    decision_sha256: str
    visibility: str
    rights_state: str
    document_id: str | None = None
    conversion_id: str | None = None

    def __post_init__(self) -> None:
        _validate_common(
            artifact_id=self.artifact_id,
            logical_key=self.logical_key,
            sha256=self.sha256,
            size=self.size,
            visibility=self.visibility,
            rights_state=self.rights_state,
        )
        _validate_authorization_provenance(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            decision_sha256=self.decision_sha256,
            required=True,
        )
        _validate_nullable_id(self.document_id, name="document_id", pattern=_DOCUMENT_ID)
        _validate_nullable_id(self.conversion_id, name="conversion_id", pattern=_CONVERSION_ID)
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
    source_id: str | None
    source_sha256: str | None
    document_id: str | None
    conversion_id: str | None
    decision_sha256: str | None
    provenance_state: str
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
        if self.provenance_state not in _PROVENANCE_STATES:
            raise ValueError("Unbekannter provenance_state")
        _validate_authorization_provenance(
            source_id=self.source_id,
            source_sha256=self.source_sha256,
            decision_sha256=self.decision_sha256,
            required=self.provenance_state == "current",
        )
        _validate_nullable_id(self.document_id, name="document_id", pattern=_DOCUMENT_ID)
        _validate_nullable_id(self.conversion_id, name="conversion_id", pattern=_CONVERSION_ID)
        validate_storage_provenance_relationship(self.source_id, self.document_id)
        if not isinstance(self.storage_backend, StorageBackend):
            raise ValueError("storage_backend muss ein StorageBackend sein")
        if type(self.storage_object_id) is not str or not self.storage_object_id:
            raise ValueError("storage_object_id darf nicht leer sein")
        if self.public_reference is not None and type(self.public_reference) is not str:
            raise ValueError("public_reference muss String oder None sein")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.1.0",
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "storage_backend": self.storage_backend.value,
            "storage_object_id": self.storage_object_id,
            "sha256": self.sha256,
            "bytes": self.size,
            "source_id": self.source_id,
            "source_sha256": self.source_sha256,
            "document_id": self.document_id,
            "conversion_id": self.conversion_id,
            "decision_sha256": self.decision_sha256,
            "provenance_state": self.provenance_state,
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
