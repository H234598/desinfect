"""Immutable contracts for deterministic, rights-bound ZIP archives."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re

from scripts.rki_pipeline.io_utils import (
    detect_path_collisions,
    normalize_posix_path,
    stable_json_dumps,
)
from scripts.rki_pipeline.storage.base import PreparedObject


ARCHIVE_FORMAT_VERSION = "1"
RESERVED_MEMBERS = frozenset({"MANIFEST.json", "README.md", "SHA256SUMS.txt"})
ARCHIVE_KINDS = frozenset(
    {
        "week-pdf",
        "week-markdown",
        "month-pdf",
        "month-markdown",
        "year-pdf",
        "year-markdown",
    }
)

_ARCHIVE_ID = re.compile(r"^[a-z][a-z0-9]*(?:-[A-Za-z0-9.]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VISIBILITIES = frozenset({"public", "repository_authorized", "internal", "restricted"})
_ZIP_MIN = 315_532_800
_ZIP_MAX = 4_354_819_198


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_entries: int = 10_000
    max_member_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 4 * 1024 * 1024 * 1024
    max_archive_bytes: int = 4 * 1024 * 1024 * 1024
    max_compression_ratio: int = 100

    def __post_init__(self) -> None:
        for field in (
            "max_entries",
            "max_member_bytes",
            "max_total_bytes",
            "max_archive_bytes",
            "max_compression_ratio",
        ):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} muss eine positive Ganzzahl sein")


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    path: str
    prepared: PreparedObject

    def __post_init__(self) -> None:
        if type(self.path) is not str:
            raise ValueError("path muss ein String sein")
        if normalize_posix_path(self.path) != self.path:
            raise ValueError("path muss kanonisch sein")
        if type(self.prepared) is not PreparedObject:
            raise ValueError("prepared muss ein exaktes PreparedObject sein")


@dataclass(frozen=True, slots=True)
class ArchiveSpec:
    archive_id: str
    period: str
    kind: str
    visibility: str
    source_date_epoch: int
    entries: tuple[ArchiveEntry, ...]

    def __post_init__(self) -> None:
        if type(self.archive_id) is not str or _ARCHIVE_ID.fullmatch(self.archive_id) is None:
            raise ValueError("archive_id ist nicht kanonisch")
        if type(self.period) is not str or not self.period:
            raise ValueError("period muss ein nichtleerer String sein")
        if type(self.kind) is not str or self.kind not in ARCHIVE_KINDS:
            raise ValueError("kind ist unbekannt")
        if type(self.visibility) is not str or self.visibility not in _VISIBILITIES:
            raise ValueError("visibility ist unbekannt")
        if type(self.source_date_epoch) is not int:
            raise ValueError("source_date_epoch muss eine Ganzzahl sein")
        if type(self.entries) is not tuple:
            raise ValueError("entries muss ein Tupel sein")
        if any(type(entry) is not ArchiveEntry for entry in self.entries):
            raise ValueError("entries müssen exakte ArchiveEntry-Werte sein")

        paths = tuple(entry.path for entry in self.entries)
        if len(set(paths)) != len(paths):
            raise ValueError("Doppelte Archivpfade sind unzulässig")
        try:
            detect_path_collisions(paths)
        except ValueError as exc:
            raise ValueError("Portable Kollision im Archiv") from exc
        for entry in self.entries:
            if entry.path in RESERVED_MEMBERS:
                raise ValueError("Reservierter Metadatenname im Archiv")
            if entry.path.casefold().endswith(".zip"):
                raise ValueError("ZIP-Mitglieder sind im Archiv unzulässig")
            if entry.prepared.visibility != self.visibility:
                raise ValueError("Gemischte Sichtbarkeit im Archiv")


@dataclass(frozen=True, slots=True)
class ArchiveBuild:
    path: Path
    input_fingerprint: str
    output_sha256: str
    size: int
    entries: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_archive_result(
            path=self.path,
            input_fingerprint=self.input_fingerprint,
            output_sha256=self.output_sha256,
            size=self.size,
            entries=self.entries,
        )


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    path: Path
    input_fingerprint: str
    output_sha256: str
    size: int
    entries: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_archive_result(
            path=self.path,
            input_fingerprint=self.input_fingerprint,
            output_sha256=self.output_sha256,
            size=self.size,
            entries=self.entries,
        )


@dataclass(frozen=True, slots=True)
class ArchiveMaterialization:
    root: Path
    zip_path: Path
    manifest_path: Path
    build: ArchiveBuild
    changed: bool

    def __post_init__(self) -> None:
        for field in ("root", "zip_path", "manifest_path"):
            if not isinstance(getattr(self, field), Path):
                raise ValueError(f"{field} muss ein Path sein")
        if type(self.build) is not ArchiveBuild:
            raise ValueError("build muss ein exakter ArchiveBuild sein")
        if type(self.changed) is not bool:
            raise ValueError("changed muss bool sein")


def archive_input_fingerprint(spec: ArchiveSpec) -> str:
    """Return a canonical SHA-256 digest of one archive's immutable inputs."""

    if type(spec) is not ArchiveSpec:
        raise ValueError("spec muss ein exakter ArchiveSpec sein")
    entries = sorted(spec.entries, key=lambda entry: entry.path)
    payload = {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "archive": {
            "archive_id": spec.archive_id,
            "period": spec.period,
            "kind": spec.kind,
        },
        "zip_datetime": _zip_datetime(spec.source_date_epoch),
        "visibility": spec.visibility,
        "entries": [
            {
                "path": entry.path,
                "bytes": entry.prepared.size,
                "sha256": entry.prepared.sha256,
            }
            for entry in entries
        ],
    }
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


def _zip_datetime(epoch: int) -> tuple[int, int, int, int, int, int]:
    """Return a ZIP-compatible UTC timestamp with its two-second precision."""

    if type(epoch) is not int:
        raise ValueError("epoch muss eine Ganzzahl sein")
    timestamp = min(max(epoch, _ZIP_MIN), _ZIP_MAX)
    value = datetime.fromtimestamp(timestamp, timezone.utc)
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second - (value.second % 2),
    )


def _validate_archive_result(
    *,
    path: Path,
    input_fingerprint: str,
    output_sha256: str,
    size: int,
    entries: tuple[str, ...],
) -> None:
    if not isinstance(path, Path):
        raise ValueError("path muss ein Path sein")
    for field, value in (("input_fingerprint", input_fingerprint), ("output_sha256", output_sha256)):
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise ValueError(f"{field} muss ein kleingeschriebener SHA-256 sein")
    if type(size) is not int or size < 0:
        raise ValueError("size muss eine nichtnegative Ganzzahl sein")
    if type(entries) is not tuple:
        raise ValueError("entries muss ein Tupel sein")
    if any(type(entry) is not str or normalize_posix_path(entry) != entry for entry in entries):
        raise ValueError("entries müssen kanonische Pfade sein")
