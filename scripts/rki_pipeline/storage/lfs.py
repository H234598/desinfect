#!/usr/bin/env python3
"""Git-LFS tracking, pointer/object integrity, budgets, and local adapter."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
from pathlib import Path
import re
import warnings

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
from scripts.rki_pipeline.storage.config import LfsConfig

_REQUIRED_TRACKING = (
    "rki/Bulletins/**/*.pdf filter=lfs diff=lfs merge=lfs -text",
    "rki/Bulletins/**/Markdown/**/*.md filter=lfs diff=lfs merge=lfs -text",
    "rki/Bulletins/**/*.zip filter=lfs diff=lfs merge=lfs -text",
)
_POINTER_RE = re.compile(
    r"\Aversion https://git-lfs\.github\.com/spec/v1\n"
    r"oid sha256:(?P<oid>[0-9a-f]{64})\n"
    r"size (?P<size>0|[1-9][0-9]*)\n?\Z"
)


class LfsIntegrityError(StorageError):
    """A tracking rule, pointer, or local LFS object is missing or corrupt."""


class LfsBudgetError(StorageError):
    """A configured per-run or total LFS budget would be exceeded."""


def _missing_parent_directories(path: Path, *, root: Path) -> tuple[Path, ...]:
    """Capture absent parents below root, deepest first, before one write."""

    missing: list[Path] = []
    parent = path.parent
    while parent != root:
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise LfsIntegrityError(
                "Rollback-Pfad liegt außerhalb des Repositoryroots"
            ) from exc
        if parent.exists() or parent.is_symlink():
            break
        missing.append(parent)
        parent = parent.parent
    return tuple(missing)


def _rollback_owned_file(
    path: Path,
    *,
    payload: bytes,
    missing_parents: tuple[Path, ...],
) -> None:
    """Remove only the exact bytes and empty parents created by this call."""

    if path.is_symlink():
        raise LfsIntegrityError(f"Rollback-Ziel wurde durch Symlink ersetzt: {path}")
    if path.exists():
        if not path.is_file():
            raise LfsIntegrityError(f"Rollback-Ziel ist keine reguläre Datei: {path}")
        measured_size, measured_hash = hash_file(path)
        expected_hash = hashlib.sha256(payload).hexdigest()
        if (measured_size, measured_hash) != (len(payload), expected_hash):
            raise LfsIntegrityError(f"Rollback-Ziel enthält fremde Bytes: {path}")
        path.unlink()
    for parent in missing_parents:
        try:
            parent.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            if exc.errno in {errno.ENOTEMPTY, errno.EEXIST}:
                break
            raise LfsIntegrityError(
                f"Neu angelegtes Rollback-Verzeichnis ist nicht entfernbar: {parent}"
            ) from exc


@dataclass(frozen=True, slots=True)
class LfsPointer:
    oid: str
    size: int

    def to_text(self) -> str:
        return (
            "version https://git-lfs.github.com/spec/v1\n"
            f"oid sha256:{self.oid}\n"
            f"size {self.size}\n"
        )


@dataclass(frozen=True, slots=True)
class LfsInventory:
    objects: int
    bytes: int

    def __post_init__(self) -> None:
        if type(self.objects) is not int or self.objects < 0:
            raise ValueError("objects muss eine nichtnegative Ganzzahl sein")
        if type(self.bytes) is not int or self.bytes < 0:
            raise ValueError("bytes muss eine nichtnegative Ganzzahl sein")


@dataclass(frozen=True, slots=True)
class LfsBudget:
    max_run_objects: int
    max_run_bytes: int
    warn_total_bytes: int
    block_total_bytes: int

    @classmethod
    def from_config(cls, config: LfsConfig) -> LfsBudget:
        return cls(
            config.max_run_objects,
            config.max_run_bytes,
            config.warn_total_bytes,
            config.block_total_bytes,
        )


def validate_lfs_tracking(path: Path) -> tuple[str, ...]:
    try:
        lines = tuple(
            line.strip()
            for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    except OSError as exc:
        raise LfsIntegrityError(f".gitattributes ist nicht lesbar: {path}") from exc
    if lines != _REQUIRED_TRACKING:
        raise LfsIntegrityError("Git-LFS-Trackingregeln weichen vom kanonischen Vertrag ab")
    return lines


def parse_lfs_pointer(text: str | bytes) -> LfsPointer:
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LfsIntegrityError("LFS-Pointer ist kein UTF-8") from exc
    if type(text) is not str:
        raise LfsIntegrityError("LFS-Pointer muss Text sein")
    match = _POINTER_RE.fullmatch(text)
    if match is None:
        raise LfsIntegrityError("Ungültiger Git-LFS-Pointer")
    return LfsPointer(match.group("oid"), int(match.group("size")))


def lfs_object_path(repository_root: Path, oid: str) -> Path:
    if len(oid) != 64 or any(character not in "0123456789abcdef" for character in oid):
        raise LfsIntegrityError("Ungültige LFS-OID")
    return Path(repository_root) / ".git" / "lfs" / "objects" / oid[:2] / oid[2:4] / oid


def verify_lfs_object(repository_root: Path, *, oid: str, size: int) -> Path:
    path = lfs_object_path(repository_root, oid)
    if not path.exists():
        raise LfsIntegrityError(f"Lokales LFS-Objekt fehlt: {oid}")
    measured_size, measured_hash = hash_file(path)
    if measured_size != size:
        raise LfsIntegrityError(f"LFS-Objektgröße stimmt nicht: {measured_size} != {size}")
    if measured_hash != oid:
        raise LfsIntegrityError(f"LFS-Objekt-SHA-256 stimmt nicht: {measured_hash} != {oid}")
    return path


def inventory_lfs_objects(repository_root: Path) -> LfsInventory:
    root = Path(repository_root) / ".git" / "lfs" / "objects"
    if not root.exists():
        return LfsInventory(0, 0)
    seen: set[str] = set()
    total = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise LfsIntegrityError(f"Symlink im LFS-Objektbestand: {path}")
        if not path.is_file() or len(path.name) != 64 or path.name in seen:
            continue
        measured_size, measured_hash = hash_file(path)
        if measured_hash != path.name:
            raise LfsIntegrityError(
                f"LFS-Objektpfad und SHA-256 driften: {path.name} != {measured_hash}"
            )
        seen.add(path.name)
        total += measured_size
    return LfsInventory(len(seen), total)


def check_lfs_budget(budget: LfsBudget, *, run: LfsInventory, total: LfsInventory) -> None:
    if run.objects > budget.max_run_objects:
        raise LfsBudgetError(f"Laufobjekte {run.objects} überschreiten {budget.max_run_objects}")
    if run.bytes > budget.max_run_bytes:
        raise LfsBudgetError(f"Laufbytes {run.bytes} überschreiten {budget.max_run_bytes}")
    if total.bytes > budget.block_total_bytes:
        raise LfsBudgetError(
            f"LFS-Blockschwelle überschritten: {total.bytes} > {budget.block_total_bytes}"
        )
    if total.bytes > budget.warn_total_bytes:
        warnings.warn(
            f"LFS-Warnschwelle überschritten: {total.bytes} > {budget.warn_total_bytes}",
            RuntimeWarning,
            stacklevel=2,
        )


def _pointer_from_path(path: Path) -> LfsPointer | None:
    if path.is_symlink():
        raise LfsIntegrityError(f"Symlink als LFS-Artefakt ist unzulässig: {path}")
    if path.stat().st_size > 1024:
        return None
    payload = path.read_bytes()
    if not payload.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
        return None
    return parse_lfs_pointer(payload)


class LfsStorageAdapter:
    """Local Git-LFS working-tree adapter; never commits, pushes, or transfers."""

    backend = StorageBackend.LFS

    def __init__(
        self,
        *,
        repository_root: Path,
        config: LfsConfig,
        authorizer: RightsStorageAuthorizer,
    ) -> None:
        try:
            self.repository_root = Path(repository_root).resolve(strict=True)
        except OSError as exc:
            raise LfsIntegrityError(f"Repositoryroot ist nicht auflösbar: {repository_root}") from exc
        if not self.repository_root.is_dir():
            raise LfsIntegrityError(f"Repositoryroot ist kein Verzeichnis: {repository_root}")
        self.config = config
        self.artifact_root = normalize_posix_path(config.artifact_root)
        if type(authorizer) is not RightsStorageAuthorizer:
            raise LfsIntegrityError(
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

    def _relative_path(self, logical_key: str) -> str:
        normalized = normalize_posix_path(logical_key)
        if normalized == self.artifact_root or normalized.startswith(f"{self.artifact_root}/"):
            return normalized
        return normalize_posix_path(f"{self.artifact_root}/{normalized}")

    def _target(self, logical_key: str) -> Path:
        return self.repository_root / self._relative_path(logical_key)

    def _validated_source(self, path: Path) -> tuple[Path, str]:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.repository_root / candidate
        if candidate.is_symlink():
            raise LfsIntegrityError(f"Symlink als LFS-Artefakt ist unzulässig: {candidate}")
        try:
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(self.repository_root).as_posix()
        except (OSError, ValueError) as exc:
            raise LfsIntegrityError("LFS-Pfad liegt außerhalb des Repositoryroots") from exc
        if not resolved.is_file():
            raise LfsIntegrityError(f"LFS-Artefakt ist keine reguläre Datei: {resolved}")
        return resolved, normalize_posix_path(relative)

    @staticmethod
    def _prepared_payload(prepared: PreparedObject) -> bytes:
        path = Path(prepared.path)
        if path.is_symlink() or not path.is_file():
            raise LfsIntegrityError("Vorbereitetes LFS-Objekt ist keine reguläre Datei")
        payload = path.read_bytes()
        measured_hash = hashlib.sha256(payload).hexdigest()
        if (len(payload), measured_hash) != (prepared.size, prepared.sha256):
            raise LfsIntegrityError(
                "Vorbereitetes LFS-Objekt besitzt falsche Größe/SHA-256"
            )
        return payload

    @staticmethod
    def _run_inventory(ledger: EffectLedger, prepared_size: int) -> LfsInventory:
        prior = [event for event in ledger.events if event.kind is EffectKind.LFS]
        if any(event.size is None for event in prior):
            raise LfsBudgetError("LFS-Ledger enthält ein Ereignis ohne Größe")
        return LfsInventory(
            objects=len(prior) + 1,
            bytes=sum(event.size or 0 for event in prior) + prepared_size,
        )

    def exists(self, intent: StorageIntent) -> StorageReference | None:
        self.authorize(intent, operation="exists")
        target = self._target(intent.logical_key)
        if not target.exists() and not target.is_symlink():
            return None
        reference = self.reference_for_path(
            target,
            artifact_id=intent.artifact_id,
            source_id=intent.source_id,
            source_sha256=intent.source_sha256,
            document_id=intent.document_id,
            conversion_id=intent.conversion_id,
            decision_sha256=intent.decision_sha256,
            provenance_state="current",
            visibility=intent.visibility,
            rights_state=intent.rights_state,
        )
        if (reference.sha256, reference.size) != (intent.sha256, intent.size):
            raise LfsIntegrityError("Vorhandenes LFS-Ziel besitzt anderen Inhalt")
        return reference

    def materialize(self, intent: StorageIntent, *, temp_root: Path, ledger: EffectLedger) -> PreparedObject:
        if ledger.mode is not RunMode.MATERIALIZE:
            raise LfsIntegrityError("LFS-Materialisierung benötigt RunMode materialize")
        self.authorize(intent, operation="materialize")
        payload = read_verified_payload(
            intent.source_path,
            sha256=intent.sha256,
            size=intent.size,
        )
        target = Path(temp_root) / normalize_posix_path(intent.logical_key)
        self.authorize(intent, operation="materialize")
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
        if ledger.mode is not RunMode.MATERIALIZE:
            raise LfsIntegrityError("LFS-Export benötigt RunMode materialize")
        if reference.storage_backend is not StorageBackend.LFS:
            raise LfsIntegrityError("Referenz gehört nicht zum LFS-Backend")
        self.authorize(reference, operation="export")
        source, _relative = self._validated_source(
            self.repository_root / normalize_posix_path(reference.relative_path)
        )
        pointer = _pointer_from_path(source)
        payload_source = (
            verify_lfs_object(self.repository_root, oid=pointer.oid, size=pointer.size)
            if pointer is not None
            else source
        )
        payload = read_verified_payload(
            payload_source,
            sha256=reference.sha256,
            size=reference.size,
        )
        target = Path(temp_root) / normalize_posix_path(reference.relative_path)
        self.authorize(reference, operation="export")
        atomic_write_bytes(target, payload, allowed_root=Path(temp_root))
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

    def apply(self, prepared: PreparedObject, *, ledger: EffectLedger) -> StorageReference:
        if ledger.mode is not RunMode.APPLY:
            raise LfsIntegrityError("LFS-Publikation benötigt RunMode apply")
        self.authorize(prepared, operation="apply")
        validate_lfs_tracking(self.repository_root / ".gitattributes")
        payload = self._prepared_payload(prepared)
        relative = self._relative_path(prepared.logical_key)
        target = self.repository_root / relative
        if target.exists() or target.is_symlink():
            existing = self.reference_for_path(
                target,
                artifact_id=prepared.artifact_id,
                source_id=prepared.source_id,
                source_sha256=prepared.source_sha256,
                document_id=prepared.document_id,
                conversion_id=prepared.conversion_id,
                decision_sha256=prepared.decision_sha256,
                provenance_state="current",
                visibility=prepared.visibility,
                rights_state=prepared.rights_state,
            )
            if (existing.sha256, existing.size) != (prepared.sha256, prepared.size):
                raise LfsIntegrityError(
                    f"Vorhandenes LFS-Ziel besitzt anderen Inhalt: {relative}"
                )
            self.authorize(prepared, operation="apply")
            return existing

        total_before = inventory_lfs_objects(self.repository_root)
        object_path = lfs_object_path(self.repository_root, prepared.sha256)
        object_exists = object_path.exists() or object_path.is_symlink()
        if object_exists:
            verify_lfs_object(
                self.repository_root,
                oid=prepared.sha256,
                size=prepared.size,
            )
        total_after = LfsInventory(
            total_before.objects + (0 if object_exists else 1),
            total_before.bytes + (0 if object_exists else prepared.size),
        )
        check_lfs_budget(
            LfsBudget.from_config(self.config),
            run=self._run_inventory(ledger, prepared.size),
            total=total_after,
        )

        pointer = LfsPointer(prepared.sha256, prepared.size).to_text().encode("utf-8")
        object_missing_parents = _missing_parent_directories(
            object_path,
            root=self.repository_root,
        )
        target_missing_parents = _missing_parent_directories(
            target,
            root=self.repository_root,
        )
        self.authorize(prepared, operation="apply")
        object_created = False
        pointer_write_started = False
        try:
            if not object_exists:
                atomic_write_bytes(
                    object_path,
                    payload,
                    allowed_root=self.repository_root,
                )
                object_created = True
            self.authorize(prepared, operation="apply")
            pointer_write_started = True
            atomic_write_bytes(target, pointer, allowed_root=self.repository_root)
        except Exception:
            cleanup_failure: Exception | None = None
            for should_cleanup, owned_path, owned_payload, missing_parents in (
                (pointer_write_started, target, pointer, target_missing_parents),
                (object_created, object_path, payload, object_missing_parents),
            ):
                if not should_cleanup:
                    continue
                try:
                    _rollback_owned_file(
                        owned_path,
                        payload=owned_payload,
                        missing_parents=missing_parents,
                    )
                except Exception as cleanup_exc:
                    cleanup_failure = cleanup_failure or cleanup_exc
            if cleanup_failure is not None:
                raise LfsIntegrityError(
                    "LFS-Zwischeneffekt konnte nicht sicher zurückgerollt werden"
                ) from cleanup_failure
            raise
        ledger.record(EffectKind.REPOSITORY_FILE, relative, sha256=prepared.sha256, size=prepared.size)
        ledger.record(EffectKind.LFS, relative, sha256=prepared.sha256, size=prepared.size)
        reference = StorageReference(
            artifact_id=prepared.artifact_id,
            relative_path=relative,
            storage_backend=StorageBackend.LFS,
            storage_object_id=f"sha256:{prepared.sha256}",
            sha256=prepared.sha256,
            size=prepared.size,
            source_id=prepared.source_id,
            source_sha256=prepared.source_sha256,
            document_id=prepared.document_id,
            conversion_id=prepared.conversion_id,
            decision_sha256=prepared.decision_sha256,
            provenance_state="current",
            visibility=prepared.visibility,
            rights_state=prepared.rights_state,
            public_reference=None,
        )
        self.verify(reference)
        return reference

    def reference_for_path(
        self,
        path: Path,
        *,
        artifact_id: str,
        source_id: str | None,
        source_sha256: str | None,
        document_id: str | None,
        conversion_id: str | None,
        decision_sha256: str | None,
        provenance_state: str,
        visibility: str,
        rights_state: str,
    ) -> StorageReference:
        validated, relative = self._validated_source(path)
        pointer = _pointer_from_path(validated)
        if pointer is None:
            size, sha256 = hash_file(validated)
        else:
            verify_lfs_object(self.repository_root, oid=pointer.oid, size=pointer.size)
            size, sha256 = pointer.size, pointer.oid
        return StorageReference(
            artifact_id=artifact_id,
            relative_path=relative,
            storage_backend=StorageBackend.LFS,
            storage_object_id=f"sha256:{sha256}",
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
            public_reference=None,
        )

    def verify(self, reference: StorageReference) -> None:
        if reference.storage_backend is not StorageBackend.LFS:
            raise LfsIntegrityError("Referenz gehört nicht zum LFS-Backend")
        self.authorize(reference, operation="verify")
        target, _relative = self._validated_source(
            self.repository_root / normalize_posix_path(reference.relative_path)
        )
        self.authorize(reference, operation="verify")
        pointer = _pointer_from_path(target)
        if pointer is not None:
            if (pointer.oid, pointer.size) != (reference.sha256, reference.size):
                raise LfsIntegrityError("LFS-Pointer und Referenz driften")
            verify_lfs_object(self.repository_root, oid=pointer.oid, size=pointer.size)
            return
        measured_size, measured_hash = hash_file(target)
        if (measured_hash, measured_size) != (reference.sha256, reference.size):
            raise LfsIntegrityError("LFS-Working-Tree-Datei und Referenz driften")

    def list_references(self) -> tuple[StorageReference, ...]:
        root = self.repository_root / self.artifact_root
        if not root.exists():
            return ()
        if root.is_symlink():
            raise LfsIntegrityError(f"Symlink im LFS-Artefaktbestand: {root}")
        references: list[StorageReference] = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise LfsIntegrityError(f"Symlink im LFS-Artefaktbestand: {path}")
            relative = path.relative_to(root)
            is_canonical_markdown = (
                path.suffix == ".md" and "Markdown" in relative.parts
            )
            if not path.is_file() or (
                path.suffix not in {".pdf", ".zip"} and not is_canonical_markdown
            ):
                continue
            digest = hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()[:24]
            references.append(
                self.reference_for_path(
                    path,
                    artifact_id=f"lfs-{digest}",
                    source_id=None,
                    source_sha256=None,
                    document_id=None,
                    conversion_id=None,
                    decision_sha256=None,
                    provenance_state="legacy_needs_review",
                    visibility="repository_authorized",
                    rights_state="unknown",
                )
            )
        return tuple(references)
