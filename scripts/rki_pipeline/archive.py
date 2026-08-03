"""Immutable contracts for deterministic, rights-bound ZIP archives."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from typing import Any
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from scripts.rki_pipeline.io_utils import (
    assert_generated_root_fd,
    detect_path_collisions,
    fd_directory_path,
    fsync_directory_fd,
    GENERATED_ROOT_SENTINEL,
    normalize_posix_path,
    open_directory_beneath,
    open_root_directory,
    relative_path_beneath,
    stable_json_dumps,
    UnsafePathError,
)
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document
from scripts.rki_pipeline.staging import StagingState, staged_directory
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    RightsStorageAuthorizer,
    StorageAuthorizationError,
    StorageError,
    authorize_storage_operation,
    hash_file,
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
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_ZIP_VERSION = 20
_BUNDLE_ZIP = "archive.zip"
_BUNDLE_MANIFEST = "archive-manifest.json"
_BUNDLE_FILES = frozenset({GENERATED_ROOT_SENTINEL, _BUNDLE_ZIP, _BUNDLE_MANIFEST})
_MAX_SIDECAR_BYTES = 8 * 1024 * 1024


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
        "SHA256SUMS.txt": _checksums(records).encode("utf-8"),
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
    descriptor: int | None = None
    try:
        descriptor = os.open(path, _READ_FLAGS)
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ArchiveSecurityError("Archivquelle ist keine reguläre Datei")
        if initial.st_size > limits.max_archive_bytes:
            raise ArchiveSecurityError("Archivgröße überschreitet das Limit")
        measured_size, output_sha256 = _hash_descriptor(descriptor)
        if measured_size != initial.st_size:
            raise ArchiveIntegrityError("Archivgröße änderte sich während der Hash-Prüfung")
        if output_sha256 != expected_output_sha256:
            raise ArchiveIntegrityError("Archiv-SHA-256 stimmt nicht mit dem erwarteten Ergebnis überein")

        with os.fdopen(os.dup(descriptor), "rb") as archive_handle:
            with ZipFile(archive_handle) as archive:
                inspection = _inspect_open_archive(
                    archive,
                    path=path,
                    size=measured_size,
                    output_sha256=output_sha256,
                    expected_fingerprint=expected_fingerprint,
                    limits=limits,
                )
        _verify_archive_identity(
            path,
            descriptor,
            initial=initial,
            expected_output_sha256=expected_output_sha256,
            limits=limits,
        )
    except ArchiveError:
        raise
    except (BadZipFile, OSError, EOFError, RuntimeError, UnicodeError) as exc:
        if descriptor is None:
            raise ArchiveSecurityError("Archivquelle ist keine reguläre Datei") from exc
        raise ArchiveIntegrityError("ZIP-Struktur oder CRC ist beschädigt") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return inspection


def materialize_archive(
    spec: ArchiveSpec,
    target: Path,
    *,
    temp_root: Path,
    ledger: EffectLedger,
    authorizer: RightsStorageAuthorizer,
    limits: ArchiveLimits = ArchiveLimits(),
) -> ArchiveMaterialization:
    """Atomically publish one validated ZIP and canonical sidecar below ``temp_root``."""

    if type(spec) is not ArchiveSpec:
        raise ValueError("spec muss ein exakter ArchiveSpec sein")
    if not isinstance(target, Path) or not isinstance(temp_root, Path):
        raise ValueError("target und temp_root müssen Path-Werte sein")
    if type(ledger) is not EffectLedger:
        raise ValueError("ledger muss ein exaktes EffectLedger sein")
    root = temp_root.resolve()
    if ledger.mode is not RunMode.MATERIALIZE or ledger.temp_root != root:
        raise ValueError("Archiv-Materialisierung benötigt passenden Materialize-Ledger/root")
    if type(authorizer) is not RightsStorageAuthorizer:
        raise ValueError("authorizer muss ein exakter RightsStorageAuthorizer sein")
    if type(limits) is not ArchiveLimits:
        raise ValueError("limits muss ein exaktes ArchiveLimits sein")
    try:
        relative = relative_path_beneath(target, root)
    except (OSError, UnsafePathError) as exc:
        raise ArchiveSecurityError("Archivziel liegt außerhalb temp_root") from exc
    bundle_root = root / Path(relative.as_posix())

    # A no-op is output mutation too: prove current rights and source identity first.
    _load_authorized_entries(spec, authorizer=authorizer, limits=limits)
    expected_fingerprint = archive_input_fingerprint(spec)
    try:
        existing = _load_existing_bundle(
            root,
            relative,
            display_root=bundle_root,
            spec=spec,
            expected_fingerprint=expected_fingerprint,
            limits=limits,
        )
    except ArchiveIntegrityError:
        existing = None
    if existing is not None:
        return ArchiveMaterialization(
            root=bundle_root,
            zip_path=bundle_root / _BUNDLE_ZIP,
            manifest_path=bundle_root / _BUNDLE_MANIFEST,
            build=existing,
            changed=False,
        )

    event_count = len(ledger.events)
    staging_state = StagingState()
    zip_path = bundle_root / _BUNDLE_ZIP
    manifest_path = bundle_root / _BUNDLE_MANIFEST
    try:
        with staged_directory(
            bundle_root,
            allowed_root=root,
            replace_existing=True,
            state=staging_state,
        ) as stage:
            staged_build = build_archive(
                spec,
                stage / _BUNDLE_ZIP,
                authorizer=authorizer,
                limits=limits,
            )
            sidecar = _archive_sidecar(spec, staged_build)
            sidecar_bytes = stable_json_dumps(sidecar).encode("utf-8")
            stage_fd = os.open(
                stage,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            try:
                _write_bundle_file(stage_fd, _BUNDLE_MANIFEST, sidecar_bytes)
                staged_result = _inspect_bundle_fd(
                    stage_fd,
                    display_root=bundle_root,
                    spec=spec,
                    expected_fingerprint=expected_fingerprint,
                    limits=limits,
                )
                fsync_directory_fd(stage_fd)
            finally:
                os.close(stage_fd)
            ledger.record(
                EffectKind.TEMP_FILE,
                zip_path.as_posix(),
                sha256=staged_result.output_sha256,
                size=staged_result.size,
            )
            ledger.record(
                EffectKind.TEMP_FILE,
                manifest_path.as_posix(),
                sha256=hashlib.sha256(sidecar_bytes).hexdigest(),
                size=len(sidecar_bytes),
            )
    except BaseException:
        if not staging_state.published:
            del ledger.events[event_count:]
        raise
    return ArchiveMaterialization(
        root=bundle_root,
        zip_path=zip_path,
        manifest_path=manifest_path,
        build=staged_result,
        changed=True,
    )


def _archive_sidecar(spec: ArchiveSpec, build: ArchiveBuild) -> dict[str, Any]:
    value = {
        "schema_version": "1.0.0",
        "archive_id": spec.archive_id,
        "period": spec.period,
        "kind": spec.kind,
        "entries": list(build.entries),
        "input_fingerprint": build.input_fingerprint,
        "output_sha256": build.output_sha256,
        "storage_reference": None,
    }
    try:
        validate_document("archive-manifest", value)
    except SchemaContractError as exc:
        raise ArchiveIntegrityError("Archiv-Sidecar verletzt den Schema-Vertrag") from exc
    return value


def _load_existing_bundle(
    root: Path,
    relative: PurePosixPath,
    *,
    display_root: Path,
    spec: ArchiveSpec,
    expected_fingerprint: str,
    limits: ArchiveLimits,
) -> ArchiveBuild | None:
    with open_root_directory(root, create=True) as root_fd:
        try:
            parent_fd = open_directory_beneath(root_fd, relative.parts[:-1])
        except FileNotFoundError:
            return None
        try:
            try:
                metadata = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(metadata.st_mode):
                raise ArchiveSecurityError("Bestehendes Archiv-Bundle ist kein reguläres Verzeichnis")
            try:
                bundle_fd = open_directory_beneath(parent_fd, (relative.name,))
            except UnsafePathError as exc:
                raise ArchiveSecurityError("Bestehendes Archiv-Bundle ist ein Symlink") from exc
            try:
                return _inspect_bundle_fd(
                    bundle_fd,
                    display_root=display_root,
                    spec=spec,
                    expected_fingerprint=expected_fingerprint,
                    limits=limits,
                )
            finally:
                os.close(bundle_fd)
        finally:
            os.close(parent_fd)


def _inspect_bundle_fd(
    bundle_fd: int,
    *,
    display_root: Path,
    spec: ArchiveSpec,
    expected_fingerprint: str,
    limits: ArchiveLimits,
) -> ArchiveBuild:
    names = frozenset(os.listdir(bundle_fd))
    if names != _BUNDLE_FILES:
        raise ArchiveIntegrityError("Archiv-Bundle enthält unbekannte oder fehlende Dateien")
    try:
        assert_generated_root_fd(bundle_fd)
    except UnsafePathError as exc:
        raise ArchiveSecurityError("Archiv-Bundle-Sentinel ist unsicher") from exc
    for name in (_BUNDLE_ZIP, _BUNDLE_MANIFEST):
        metadata = os.stat(name, dir_fd=bundle_fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArchiveSecurityError(f"Archiv-Bundle-Datei {name!r} ist keine reguläre Datei")

    sidecar = _strict_sidecar(
        _read_bundle_file(bundle_fd, _BUNDLE_MANIFEST, maximum=_MAX_SIDECAR_BYTES)
    )
    expected_entries = [entry.path for entry in sorted(spec.entries, key=lambda entry: entry.path)]
    if (
        sidecar["archive_id"] != spec.archive_id
        or sidecar["period"] != spec.period
        or sidecar["kind"] != spec.kind
        or sidecar["entries"] != expected_entries
        or sidecar["input_fingerprint"] != expected_fingerprint
    ):
        raise ArchiveIntegrityError("Archiv-Sidecar stimmt nicht mit den erwarteten Eingaben überein")
    archive_path = fd_directory_path(bundle_fd) / _BUNDLE_ZIP
    try:
        inspection = validate_archive(
            archive_path,
            expected_fingerprint=expected_fingerprint,
            expected_output_sha256=sidecar["output_sha256"],
            limits=limits,
        )
    except ArchiveError as exc:
        raise ArchiveIntegrityError("Bestehendes Archiv im Bundle ist ungültig") from exc
    if list(inspection.entries) != sidecar["entries"]:
        raise ArchiveIntegrityError("Archiv und Sidecar enthalten verschiedene Einträge")
    return ArchiveBuild(
        path=display_root / _BUNDLE_ZIP,
        input_fingerprint=inspection.input_fingerprint,
        output_sha256=inspection.output_sha256,
        size=inspection.size,
        entries=inspection.entries,
    )


def _strict_sidecar(payload: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Doppelter JSON-Schlüssel")
            value[key] = item
        return value

    def reject_constant(value: str) -> Any:
        raise ValueError(f"Nichtendliche JSON-Zahl: {value}")

    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ArchiveIntegrityError("Archiv-Sidecar enthält ungültiges JSON") from exc
    if type(value) is not dict or stable_json_dumps(value).encode("utf-8") != payload:
        raise ArchiveIntegrityError("Archiv-Sidecar-JSON ist nicht kanonisch")
    try:
        validate_document("archive-manifest", value)
    except SchemaContractError as exc:
        raise ArchiveIntegrityError("Archiv-Sidecar verletzt den Schema-Vertrag") from exc
    return value


def _read_bundle_file(bundle_fd: int, name: str, *, maximum: int) -> bytes:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=bundle_fd)
        initial = os.fstat(descriptor)
        if not stat.S_ISREG(initial.st_mode):
            raise ArchiveSecurityError(f"Archiv-Bundle-Datei {name!r} ist keine reguläre Datei")
        if initial.st_size > maximum:
            raise ArchiveSecurityError(f"Archiv-Bundle-Datei {name!r} überschreitet das Limit")
        chunks: list[bytes] = []
        remaining = initial.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ArchiveIntegrityError(f"Archiv-Bundle-Datei {name!r} ist unvollständig")
            chunks.append(chunk)
            remaining -= len(chunk)
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=bundle_fd, follow_symlinks=False)
        if (
            (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
            != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            or (final.st_dev, final.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ArchiveIntegrityError(f"Archiv-Bundle-Datei {name!r} änderte sich beim Lesen")
        return b"".join(chunks)
    except ArchiveError:
        raise
    except OSError as exc:
        raise ArchiveSecurityError(f"Archiv-Bundle-Datei {name!r} ist nicht sicher lesbar") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_bundle_file(bundle_fd: int, name: str, payload: bytes) -> None:
    descriptor: int | None = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    try:
        descriptor = os.open(name, flags, 0o644, dir_fd=bundle_fd)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Sidecar-Write machte keinen Fortschritt")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            os.close(descriptor)


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
        payload = _read_prepared_payload(entry)
        loaded.append((entry, payload))
    return tuple(loaded)


def _read_prepared_payload(entry: ArchiveEntry) -> bytes:
    prepared = entry.prepared
    descriptor: int | None = None
    parent_descriptor: int | None = None
    try:
        relative = relative_path_beneath(
            Path(os.path.abspath(prepared.path)),
            Path(os.path.abspath(prepared.temp_root)),
        )
        with open_root_directory(prepared.temp_root) as root_descriptor:
            parent_descriptor = open_directory_beneath(root_descriptor, relative.parts[:-1])
            descriptor = os.open(relative.name, _READ_FLAGS, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ArchiveSecurityError(f"Payloadquelle für {entry.path!r} ist keine reguläre Datei")
        if metadata.st_size != prepared.size:
            raise ArchiveIntegrityError(f"Payload {entry.path!r} hat eine unerwartete Größe")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
            digest.update(chunk)
        payload = b"".join(chunks)
        if len(payload) != prepared.size or digest.hexdigest() != prepared.sha256:
            raise ArchiveIntegrityError(
                f"Payload {entry.path!r} stimmt nicht mit PreparedObject überein"
            )
        return payload
    except ArchiveError:
        raise
    except (OSError, UnsafePathError) as exc:
        raise ArchiveSecurityError(
            f"Symlink- oder unsichere Pfadkomponente für Payload {entry.path!r}"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor is not None:
            os.close(parent_descriptor)


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    size = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _verify_archive_identity(
    path: Path,
    descriptor: int,
    *,
    initial: os.stat_result,
    expected_output_sha256: str,
    limits: ArchiveLimits,
) -> None:
    current = os.fstat(descriptor)
    if current.st_size > limits.max_archive_bytes:
        raise ArchiveSecurityError("Archivgröße überschreitet das Limit")
    if (current.st_dev, current.st_ino, current.st_size) != (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
    ):
        raise ArchiveSecurityError("Archivdatei änderte Größe oder Identität während der Prüfung")
    measured_size, measured_sha256 = _hash_descriptor(descriptor)
    after_hash = os.fstat(descriptor)
    if after_hash.st_size > limits.max_archive_bytes:
        raise ArchiveSecurityError("Archivgröße überschreitet das Limit")
    if (after_hash.st_dev, after_hash.st_ino, after_hash.st_size) != (
        current.st_dev,
        current.st_ino,
        current.st_size,
    ):
        raise ArchiveSecurityError("Archivdatei änderte Größe oder Identität während der Prüfung")
    if measured_size != after_hash.st_size or measured_sha256 != expected_output_sha256:
        raise ArchiveIntegrityError("Archivdatei änderte Inhalt während der Prüfung")
    try:
        bound = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise ArchiveSecurityError("Archivpfad wurde während der Prüfung ausgetauscht") from exc
    if not stat.S_ISREG(bound.st_mode) or (bound.st_dev, bound.st_ino, bound.st_size) != (
        after_hash.st_dev,
        after_hash.st_ino,
        after_hash.st_size,
    ):
        raise ArchiveSecurityError("Archivpfad wurde während der Prüfung ausgetauscht")


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
        if (
            info.create_version != _ZIP_VERSION
            or info.extract_version != _ZIP_VERSION
            or info.reserved != 0
            or info.volume != 0
        ):
            raise ArchiveSecurityError(
                f"ZIP-Mitglied {info.filename!r} hat unerwartete Version oder ZIP64-Metadaten"
            )
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
        _validate_local_header(archive, info)

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
    expected_checksums = _checksums(records).encode("utf-8")
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


def _validate_local_header(archive: ZipFile, info: ZipInfo) -> None:
    handle = archive.fp
    if handle is None:
        raise ArchiveIntegrityError("ZIP-Datei ist während der Strukturprüfung geschlossen")
    position = handle.tell()
    try:
        handle.seek(info.header_offset)
        header = handle.read(30)
        if len(header) != 30:
            raise ArchiveIntegrityError(f"Lokaler ZIP-Header für {info.filename!r} ist unvollständig")
        (
            signature,
            extract_version,
            flags,
            compression,
            _modified_time,
            _modified_date,
            crc,
            compressed_size,
            file_size,
            name_size,
            extra_size,
        ) = struct.unpack("<4s5H3I2H", header)
        handle.read(name_size)
        local_extra = handle.read(extra_size)
    finally:
        handle.seek(position)
    if signature != b"PK\x03\x04":
        raise ArchiveIntegrityError(f"Lokaler ZIP-Header für {info.filename!r} ist ungültig")
    if (
        extract_version != _ZIP_VERSION
        or compressed_size == 0xFFFFFFFF
        or file_size == 0xFFFFFFFF
        or extra_size != 0
        or local_extra
    ):
        raise ArchiveSecurityError(
            f"Lokaler ZIP-Header für {info.filename!r} enthält ZIP64 oder unerwartete Version"
        )
    if (
        flags != info.flag_bits
        or compression != info.compress_type
        or crc != info.CRC
        or compressed_size != info.compress_size
        or file_size != info.file_size
    ):
        raise ArchiveIntegrityError(
            f"Lokaler und zentraler ZIP-Header für {info.filename!r} widersprechen sich"
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
