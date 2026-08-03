"""Immutable contracts for deterministic, rights-bound ZIP archives."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import stat
from typing import Any
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from scripts.rki_pipeline.io_utils import (
    detect_path_collisions,
    normalize_posix_path,
    stable_json_dumps,
)
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    RightsStorageAuthorizer,
    StorageAuthorizationError,
    StorageError,
    authorize_storage_operation,
    hash_file,
    read_verified_payload,
)


ARCHIVE_FORMAT_VERSION = "1"
RESERVED_MEMBERS = frozenset({"MANIFEST.json", "README.md", "SHA256SUMS.txt"})
_RESERVED_MEMBER_KEYS = frozenset(member.casefold() for member in RESERVED_MEMBERS)
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
_EXPECTED_MODE = stat.S_IFREG | 0o644


class ArchiveError(ValueError):
    """Base failure for deterministic archive contracts."""


class ArchiveSecurityError(ArchiveError):
    """Unsafe source, member metadata, path, or resource use."""


class ArchiveIntegrityError(ArchiveError):
    """Archive bytes disagree with their declared identity."""


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
            if "/" not in entry.path and entry.path.casefold() in _RESERVED_MEMBER_KEYS:
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


def build_archive(
    spec: ArchiveSpec,
    destination: Path,
    *,
    authorizer: RightsStorageAuthorizer,
    limits: ArchiveLimits = ArchiveLimits(),
) -> ArchiveBuild:
    """Build and validate one rights-authorized deterministic ZIP archive."""

    if type(spec) is not ArchiveSpec:
        raise ValueError("spec muss ein exakter ArchiveSpec sein")
    if not isinstance(destination, Path):
        raise ValueError("destination muss ein Path sein")
    if type(limits) is not ArchiveLimits:
        raise ValueError("limits muss ein exaktes ArchiveLimits sein")
    if destination.is_symlink():
        raise ArchiveSecurityError("Archivziel darf kein Symlink sein")

    loaded = _load_authorized_entries(spec, authorizer=authorizer, limits=limits)
    fingerprint = archive_input_fingerprint(spec)
    records = tuple(
        {"path": entry.path, "bytes": entry.prepared.size, "sha256": entry.prepared.sha256}
        for entry, _ in loaded
    )
    manifest = {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "archive_id": spec.archive_id,
        "period": spec.period,
        "kind": spec.kind,
        "visibility": spec.visibility,
        "zip_datetime": _zip_datetime(spec.source_date_epoch),
        "input_fingerprint": fingerprint,
        "entries": records,
    }
    metadata = {
        "MANIFEST.json": stable_json_dumps(manifest).encode("utf-8"),
        "README.md": _readme(manifest).encode("utf-8"),
        "SHA256SUMS.txt": _checksums(records).encode("ascii"),
    }
    payloads = {entry.path: payload for entry, payload in loaded}

    try:
        with ZipFile(destination, "w", compression=ZIP_STORED, allowZip64=False) as archive:
            for name in sorted((*metadata, *payloads)):
                archive.writestr(
                    _zip_info(name, spec.source_date_epoch),
                    metadata[name] if name in metadata else payloads[name],
                )
    except (OSError, BadZipFile, ValueError) as exc:
        raise ArchiveIntegrityError("Archiv konnte nicht geschrieben werden") from exc

    try:
        archive_size, output_sha256 = hash_file(destination)
    except StorageError as exc:
        raise ArchiveIntegrityError("Geschriebenes Archiv ist keine reguläre Datei") from exc
    validate_archive(
        destination,
        expected_fingerprint=fingerprint,
        expected_output_sha256=output_sha256,
        limits=limits,
    )
    return ArchiveBuild(
        path=destination,
        input_fingerprint=fingerprint,
        output_sha256=output_sha256,
        size=archive_size,
        entries=tuple(entry.path for entry, _ in loaded),
    )


def validate_archive(
    path: Path,
    *,
    expected_fingerprint: str,
    expected_output_sha256: str,
    limits: ArchiveLimits = ArchiveLimits(),
) -> ArchiveInspection:
    """Strictly inspect one ZIP without extracting any member."""

    if not isinstance(path, Path):
        raise ValueError("path muss ein Path sein")
    for field, value in (
        ("expected_fingerprint", expected_fingerprint),
        ("expected_output_sha256", expected_output_sha256),
    ):
        if type(value) is not str or _SHA256.fullmatch(value) is None:
            raise ValueError(f"{field} muss ein kleingeschriebener SHA-256 sein")
    if type(limits) is not ArchiveLimits:
        raise ValueError("limits muss ein exaktes ArchiveLimits sein")
    if path.is_symlink() or not path.is_file():
        raise ArchiveSecurityError("Archivquelle ist keine reguläre Datei")

    archive_size = path.stat().st_size
    if archive_size > limits.max_archive_bytes:
        raise ArchiveSecurityError("Archivgröße überschreitet das Limit")
    try:
        measured_size, output_sha256 = hash_file(path)
    except StorageError as exc:
        raise ArchiveSecurityError("Archivquelle ist keine reguläre Datei") from exc
    if output_sha256 != expected_output_sha256:
        raise ArchiveIntegrityError("Archiv-SHA-256 stimmt nicht mit dem erwarteten Ergebnis überein")

    try:
        with ZipFile(path) as archive:
            inspection = _inspect_open_archive(
                archive,
                path=path,
                size=measured_size,
                output_sha256=output_sha256,
                expected_fingerprint=expected_fingerprint,
                limits=limits,
            )
    except ArchiveError:
        raise
    except (BadZipFile, OSError, EOFError, RuntimeError, UnicodeError) as exc:
        raise ArchiveIntegrityError("ZIP-Struktur oder CRC ist beschädigt") from exc
    return inspection


def _load_authorized_entries(
    spec: ArchiveSpec,
    *,
    authorizer: RightsStorageAuthorizer,
    limits: ArchiveLimits,
) -> tuple[tuple[ArchiveEntry, bytes], ...]:
    entries = tuple(sorted(spec.entries, key=lambda entry: entry.path))
    if len(entries) > limits.max_entries:
        raise ArchiveSecurityError("Anzahl der Archivmitglieder überschreitet das Limit")
    total = 0
    loaded: list[tuple[ArchiveEntry, bytes]] = []
    for entry in entries:
        if entry.prepared.size > limits.max_member_bytes:
            raise ArchiveSecurityError(f"Mitglied {entry.path!r} überschreitet das Größenlimit")
        total += entry.prepared.size
        if total > limits.max_total_bytes:
            raise ArchiveSecurityError("Gesamtgröße der Payloads überschreitet das Limit")
        try:
            authorize_storage_operation(authorizer, entry.prepared, operation="archive")
        except StorageAuthorizationError as exc:
            raise ArchiveSecurityError(f"Rechteentscheidung für {entry.path!r} ist ungültig") from exc
        if entry.prepared.path.is_symlink() or not entry.prepared.path.is_file():
            raise ArchiveSecurityError(f"Payloadquelle für {entry.path!r} ist keine reguläre Datei")
        try:
            payload = read_verified_payload(
                entry.prepared.path,
                sha256=entry.prepared.sha256,
                size=entry.prepared.size,
            )
        except StorageError as exc:
            raise ArchiveIntegrityError(f"Payload {entry.path!r} stimmt nicht mit PreparedObject überein") from exc
        loaded.append((entry, payload))
    return tuple(loaded)


def _zip_info(name: str, epoch: int) -> ZipInfo:
    info = ZipInfo(name, _zip_datetime(epoch))
    info.create_system = 3
    info.compress_type = ZIP_STORED
    info.external_attr = _EXPECTED_MODE << 16
    info.extra = b""
    info.comment = b""
    return info


def _readme(manifest: dict[str, Any]) -> str:
    return "\n".join(
        (
            f"# {manifest['archive_id']}",
            "",
            f"Archive format: {manifest['format_version']}",
            f"Period: {manifest['period']}",
            f"Kind: {manifest['kind']}",
            f"Visibility: {manifest['visibility']}",
            f"Input fingerprint: {manifest['input_fingerprint']}",
            "",
        )
    )


def _checksums(records: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    return "".join(f"{record['sha256']}  {record['path']}\n" for record in records)


def _inspect_open_archive(
    archive: ZipFile,
    *,
    path: Path,
    size: int,
    output_sha256: str,
    expected_fingerprint: str,
    limits: ArchiveLimits,
) -> ArchiveInspection:
    if archive.comment:
        raise ArchiveSecurityError("ZIP-Kommentar ist unzulässig")
    infos = archive.infolist()
    names = tuple(info.filename for info in infos)
    if len(infos) > limits.max_entries + len(RESERVED_MEMBERS):
        raise ArchiveSecurityError("Anzahl der Archivmitglieder überschreitet das Limit")
    if len(set(names)) != len(names):
        raise ArchiveSecurityError("Doppelte ZIP-Mitglieder sind unzulässig")
    if names != tuple(sorted(names)):
        raise ArchiveSecurityError("ZIP-Mitglieder stehen nicht in kanonischer Reihenfolge")
    try:
        normalized_names = tuple(normalize_posix_path(name) for name in names)
        if normalized_names != names:
            raise ArchiveSecurityError("ZIP-Mitgliedsname ist nicht kanonisch")
        detect_path_collisions(names)
    except ArchiveSecurityError:
        raise
    except ValueError as exc:
        message = "Portable Kollision oder nichtkanonischer ZIP-Mitgliedsname"
        raise ArchiveSecurityError(message) from exc

    total = 0
    for info in infos:
        if info.flag_bits & 1:
            raise ArchiveSecurityError(f"ZIP-Mitglied {info.filename!r} ist verschlüsselt")
        expected_flag = 0 if info.filename.isascii() else 0x800
        if info.flag_bits != expected_flag:
            raise ArchiveSecurityError(f"ZIP-Mitglied {info.filename!r} hat unerwartete Flags")
        if info.file_size > limits.max_member_bytes:
            raise ArchiveSecurityError(f"Mitglied {info.filename!r} überschreitet das Größenlimit")
        total += info.file_size
        if total > limits.max_total_bytes:
            raise ArchiveSecurityError("Gesamtgröße der ZIP-Mitglieder überschreitet das Limit")
        ratio = info.file_size / info.compress_size if info.compress_size else float("inf") if info.file_size else 1
        if ratio > limits.max_compression_ratio:
            raise ArchiveSecurityError("Kompressionsverhältnis überschreitet das Limit")
        if info.compress_type != ZIP_STORED:
            raise ArchiveSecurityError(f"ZIP-Mitglied {info.filename!r} verwendet unerlaubte Kompression")
        if info.create_system != 3:
            raise ArchiveSecurityError(f"ZIP-Mitglied {info.filename!r} hat unerwartetes Erzeugersystem")
        if info.external_attr != _EXPECTED_MODE << 16:
            raise ArchiveSecurityError(f"ZIP-Mitglied {info.filename!r} hat unerwarteten Modus")
        if info.is_dir() or not stat.S_ISREG(info.external_attr >> 16):
            raise ArchiveSecurityError(f"ZIP-Mitglied {info.filename!r} ist keine reguläre Datei")
        if info.extra:
            raise ArchiveSecurityError(f"ZIP-Mitglied {info.filename!r} enthält Extra-Metadaten")
        if info.comment:
            raise ArchiveSecurityError(f"ZIP-Mitglied {info.filename!r} enthält einen Kommentar")
        if info.filename.casefold().endswith(".zip"):
            raise ArchiveSecurityError("Verschachtelte ZIP-Mitglieder sind unzulässig")

    metadata_names = set(RESERVED_MEMBERS)
    present_names = set(names)
    if not metadata_names.issubset(present_names):
        raise ArchiveIntegrityError("Erforderliche Archivmetadaten fehlen")
    payload_names = tuple(name for name in names if name not in metadata_names)
    if len(payload_names) > limits.max_entries:
        raise ArchiveSecurityError("Anzahl der Payload-Mitglieder überschreitet das Limit")

    manifest_bytes = _read_member_bytes(archive, archive.getinfo("MANIFEST.json"))
    manifest = _strict_manifest(manifest_bytes)
    expected_datetime = tuple(manifest["zip_datetime"])
    if any(info.date_time != expected_datetime for info in infos):
        raise ArchiveSecurityError("ZIP-Mitglied hat unerwartete Zeit-Metadaten")
    if manifest["input_fingerprint"] != expected_fingerprint:
        raise ArchiveIntegrityError("Manifest-Fingerprint stimmt nicht mit dem erwarteten Fingerprint überein")
    if _manifest_fingerprint(manifest) != expected_fingerprint:
        raise ArchiveIntegrityError("Manifest-Fingerprint bindet Identität oder Sichtbarkeit nicht korrekt")

    records = manifest["entries"]
    record_names = tuple(record["path"] for record in records)
    if record_names != payload_names:
        raise ArchiveIntegrityError("Manifest-Einträge stimmen nicht mit Payload-Mitgliedern überein")
    checksum_bytes = _read_member_bytes(archive, archive.getinfo("SHA256SUMS.txt"))
    expected_checksums = _checksums(records).encode("ascii")
    if checksum_bytes != expected_checksums:
        raise ArchiveIntegrityError("Checksum-Datei ist malformed oder stimmt nicht mit dem Manifest überein")
    readme_bytes = _read_member_bytes(archive, archive.getinfo("README.md"))
    if readme_bytes != _readme(manifest).encode("utf-8"):
        raise ArchiveIntegrityError("README stimmt nicht mit dem Manifest überein")

    infos_by_name = {info.filename: info for info in infos}
    for record in records:
        measured_size, measured_sha256 = _stream_member_identity(
            archive, infos_by_name[record["path"]]
        )
        if measured_size != record["bytes"]:
            raise ArchiveIntegrityError(f"Payload {record['path']!r} hat eine falsche Größe")
        if measured_sha256 != record["sha256"]:
            raise ArchiveIntegrityError(f"Payload {record['path']!r} hat eine falsche SHA-256")

    return ArchiveInspection(
        path=path,
        input_fingerprint=expected_fingerprint,
        output_sha256=output_sha256,
        size=size,
        entries=payload_names,
    )


def _read_member_bytes(archive: ZipFile, info: ZipInfo) -> bytes:
    try:
        with archive.open(info) as handle:
            chunks = tuple(iter(lambda: handle.read(1024 * 1024), b""))
    except (BadZipFile, OSError, EOFError, RuntimeError) as exc:
        raise ArchiveIntegrityError(f"CRC oder Inhalt von {info.filename!r} ist beschädigt") from exc
    payload = b"".join(chunks)
    if len(payload) != info.file_size:
        raise ArchiveIntegrityError(f"Mitglied {info.filename!r} wurde nicht vollständig gelesen")
    return payload


def _stream_member_identity(archive: ZipFile, info: ZipInfo) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    try:
        with archive.open(info) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except (BadZipFile, OSError, EOFError, RuntimeError) as exc:
        raise ArchiveIntegrityError(f"CRC oder Inhalt von {info.filename!r} ist beschädigt") from exc
    if size != info.file_size:
        raise ArchiveIntegrityError(f"Mitglied {info.filename!r} wurde nicht vollständig gelesen")
    return size, digest.hexdigest()


def _strict_manifest(payload: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Doppelter JSON-Schlüssel")
            result[key] = value
        return result

    def reject_constant(value: str) -> Any:
        raise ValueError(f"Nichtendliche JSON-Zahl: {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArchiveIntegrityError("Manifest enthält ungültiges JSON") from exc
    if type(value) is not dict or stable_json_dumps(value).encode("utf-8") != payload:
        raise ArchiveIntegrityError("Manifest-JSON ist nicht kanonisch")
    try:
        _validate_manifest_shape(value)
    except ArchiveIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise ArchiveIntegrityError("Manifest enthält semantisch ungültige Werte") from exc
    return value


def _validate_manifest_shape(manifest: dict[str, Any]) -> None:
    if set(manifest) != {
        "format_version",
        "archive_id",
        "period",
        "kind",
        "visibility",
        "zip_datetime",
        "input_fingerprint",
        "entries",
    }:
        raise ArchiveIntegrityError("Manifest-Felder sind unvollständig oder unbekannt")
    if manifest["format_version"] != ARCHIVE_FORMAT_VERSION:
        raise ArchiveIntegrityError("Manifest-Formatversion ist unbekannt")
    if type(manifest["archive_id"]) is not str or _ARCHIVE_ID.fullmatch(manifest["archive_id"]) is None:
        raise ArchiveIntegrityError("Manifest-Archiv-ID ist nicht kanonisch")
    if type(manifest["period"]) is not str or not manifest["period"]:
        raise ArchiveIntegrityError("Manifest-Periode ist ungültig")
    if manifest["kind"] not in ARCHIVE_KINDS or type(manifest["kind"]) is not str:
        raise ArchiveIntegrityError("Manifest-Art ist unbekannt")
    if manifest["visibility"] not in _VISIBILITIES or type(manifest["visibility"]) is not str:
        raise ArchiveIntegrityError("Manifest-Sichtbarkeit ist unbekannt")
    zip_datetime = manifest["zip_datetime"]
    if (
        type(zip_datetime) is not list
        or len(zip_datetime) != 6
        or any(type(part) is not int for part in zip_datetime)
        or zip_datetime[-1] % 2
    ):
        raise ArchiveIntegrityError("Manifest-ZIP-Zeit ist ungültig")
    try:
        datetime(*zip_datetime, tzinfo=timezone.utc)
    except ValueError as exc:
        raise ArchiveIntegrityError("Manifest-ZIP-Zeit ist ungültig") from exc
    if not (1980 <= zip_datetime[0] <= 2107):
        raise ArchiveIntegrityError("Manifest-ZIP-Zeit liegt außerhalb des ZIP-Bereichs")
    if type(manifest["input_fingerprint"]) is not str or _SHA256.fullmatch(manifest["input_fingerprint"]) is None:
        raise ArchiveIntegrityError("Manifest-Fingerprint ist ungültig")
    records = manifest["entries"]
    if type(records) is not list:
        raise ArchiveIntegrityError("Manifest-Einträge müssen eine Liste sein")
    names: list[str] = []
    for record in records:
        if type(record) is not dict or set(record) != {"path", "bytes", "sha256"}:
            raise ArchiveIntegrityError("Manifest-Payload-Eintrag ist ungültig")
        name, byte_count, sha256 = record["path"], record["bytes"], record["sha256"]
        if type(name) is not str or normalize_posix_path(name) != name:
            raise ArchiveIntegrityError("Manifest-Payload-Pfad ist nicht kanonisch")
        if type(byte_count) is not int or byte_count < 0:
            raise ArchiveIntegrityError("Manifest-Payload-Größe ist ungültig")
        if type(sha256) is not str or _SHA256.fullmatch(sha256) is None:
            raise ArchiveIntegrityError("Manifest-Payload-SHA-256 ist ungültig")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise ArchiveIntegrityError("Manifest-Payload-Einträge sind nicht eindeutig sortiert")
    try:
        detect_path_collisions(names)
    except ValueError as exc:
        raise ArchiveIntegrityError("Manifest enthält portable Pfadkollision") from exc


def _manifest_fingerprint(manifest: dict[str, Any]) -> str:
    payload = {
        "format_version": manifest["format_version"],
        "archive": {
            "archive_id": manifest["archive_id"],
            "period": manifest["period"],
            "kind": manifest["kind"],
        },
        "zip_datetime": manifest["zip_datetime"],
        "visibility": manifest["visibility"],
        "entries": manifest["entries"],
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
