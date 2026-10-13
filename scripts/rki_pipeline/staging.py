#!/usr/bin/env python3
"""FD-anchored, sentinel-protected directory staging with rollback."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
from enum import Enum
import fcntl
import os
from pathlib import Path
from pathlib import PurePosixPath
import signal
import stat
import sys
import threading
from typing import Callable, Iterator
import uuid

from scripts.rki_pipeline.io_utils import (
    assert_generated_root_fd,
    clear_generated_tree_fd,
    entry_exists,
    fd_directory_path,
    fsync_directory_fd,
    list_directory_fd,
    mark_generated_root_fd,
    open_directory_beneath,
    open_root_directory,
    relative_path_beneath,
    remove_tree_at,
    validate_tree_no_symlinks_fd,
)


class StagingError(RuntimeError):
    """A staging transaction cannot safely reach or restore its target."""


class StagingConflictError(StagingError):
    """A create-if-absent publication found a concurrently published target."""


class StagingUnsupportedError(StagingError):
    """Host filesystem cannot provide atomic no-replace publication."""


_TARGET_REGISTRY: set[tuple[int, int, str]] = set()
_TARGET_REGISTRY_GUARD = threading.Lock()


@dataclass(slots=True)
class StagingState:
    """Observable publication state for ledger/error reconciliation."""

    published: bool = False
    no_change: bool = False
    no_change_validated: bool = False


@dataclass(slots=True)
class _CoordinatedTarget:
    relative: PurePosixPath
    parent_fd: int
    target_name: str
    staging_name: str
    backup_name: str
    quarantine_name: str
    expected_generation: tuple[tuple[object, ...], ...] | None
    staging_fd: int
    target_moved: bool = False
    staging_published: bool = False
    published_identity: tuple[int, int, int] | None = None


class _QuarantineRemnant(Enum):
    NONE = "none"
    VERIFIED = "verified"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class _RollbackDiagnostic:
    target_name: str
    quarantine_name: str
    phase: str
    error: BaseException
    quarantine_remnant: _QuarantineRemnant

    def render(self) -> str:
        message = f"target={self.target_name}; phase={self.phase}; error={self.error}"
        if self.quarantine_remnant is _QuarantineRemnant.VERIFIED:
            message += f"; sicherer Quarantänerest={self.quarantine_name}"
        elif self.quarantine_remnant is _QuarantineRemnant.UNKNOWN:
            message += "; Quarantänereststatus=ungeklärt"
        return message


@dataclass(frozen=True, slots=True)
class _RollbackOutcome:
    staging_published: bool
    target_moved: bool
    diagnostics: tuple[_RollbackDiagnostic, ...]


def _coordinated_publication_checkpoint(relative: PurePosixPath) -> None:
    """Fault-injection seam after one coordinated target rename."""

    del relative


def _rename_noreplace(parent_fd: int, source: str, target: str) -> None:
    """Atomically publish one directory only when target does not exist."""

    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError as exc:
        raise StagingUnsupportedError(
            "Atomare NOREPLACE-Veröffentlichung wird nicht unterstützt"
        ) from exc
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.EEXIST, errno.ENOTEMPTY}:
        raise StagingConflictError(f"Ziel wurde parallel veröffentlicht: {target}")
    if error in {
        errno.ENOSYS,
        errno.EINVAL,
        getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
    }:
        raise StagingUnsupportedError("Atomare NOREPLACE-Veröffentlichung wird nicht unterstützt")
    raise StagingError(f"Atomare NOREPLACE-Veröffentlichung fehlgeschlagen: {os.strerror(error)}")


@contextmanager
def _publication_signal_guard() -> Iterator[None]:
    """Defer cancellation signals until rename and publication state agree."""

    blocked = {
        candidate
        for candidate in (
            getattr(signal, "SIGINT", None),
            getattr(signal, "SIGTERM", None),
            getattr(signal, "SIGHUP", None),
        )
        if candidate is not None
    }
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, blocked)
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


def _device_id(value: Path | int) -> int:
    """Return the filesystem device identifier for a path or descriptor."""

    if isinstance(value, int):
        return os.fstat(value).st_dev
    return value.stat().st_dev


def ensure_same_filesystem(left: Path | int, right: Path | int) -> None:
    """Reject a non-atomic cross-device directory replacement."""

    if _device_id(left) != _device_id(right):
        raise StagingError(f"Cross-device-Austausch ist nicht atomar: {left} -> {right}")


def _open_child_directory(parent_fd: int, name: str) -> int:
    """Open one child directory without following a symlink."""

    return open_directory_beneath(parent_fd, (name,))


def _assert_marked_directory(parent_fd: int, name: str) -> None:
    """Require a descriptor-relative directory to carry the generated sentinel."""

    descriptor = _open_child_directory(parent_fd, name)
    try:
        assert_generated_root_fd(descriptor)
    finally:
        os.close(descriptor)


def _directory_generation(directory_fd: int, prefix: str = "") -> tuple[tuple[object, ...], ...]:
    """Snapshot every owned entry without following links."""

    root_before = os.fstat(directory_fd)
    if not stat.S_ISDIR(root_before.st_mode):
        raise StagingError("Zielgeneration enthält kein Verzeichnis")
    rows: list[tuple[object, ...]] = []
    names = tuple(sorted(os.listdir(directory_fd)))
    metadata_by_name: dict[str, os.stat_result] = {}
    for name in names:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        metadata_by_name[name] = metadata
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISLNK(metadata.st_mode):
            raise StagingError(f"Symlink im Zielbaum ist unzulässig: {relative}")
        if not stat.S_ISREG(metadata.st_mode) and not stat.S_ISDIR(metadata.st_mode):
            raise StagingError(f"Zielbaum enthält keinen regulären Eintrag: {relative}")
        rows.append(
            (
                relative,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        )
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_child_directory(directory_fd, name)
            try:
                if not _metadata_matches(metadata, os.fstat(child_fd)):
                    raise StagingConflictError("Zielgeneration änderte sich während der Prüfung")
                rows.extend(_directory_generation(child_fd, relative))
                if not _metadata_matches(metadata, os.fstat(child_fd)):
                    raise StagingConflictError("Zielgeneration änderte sich während der Prüfung")
            finally:
                os.close(child_fd)
    if tuple(sorted(list_directory_fd(directory_fd))) != names or not _metadata_matches(
        root_before, os.fstat(directory_fd)
    ):
        raise StagingConflictError("Zielgeneration änderte sich während der Prüfung")
    for name, before in metadata_by_name.items():
        if not _metadata_matches(before, os.stat(name, dir_fd=directory_fd, follow_symlinks=False)):
            raise StagingConflictError("Zielgeneration änderte sich während der Prüfung")
    return tuple(rows)


def _metadata_matches(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare complete generation metadata relevant to descriptor ownership."""

    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _target_generation(parent_fd: int, name: str) -> tuple[tuple[object, ...], ...] | None:
    """Return absence or no-follow metadata for target's complete generation."""

    if not entry_exists(parent_fd, name):
        return None
    target_fd = _open_child_directory(parent_fd, name)
    try:
        metadata = os.fstat(target_fd)
        return (
            (
                "",
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            ),
            *_directory_generation(target_fd),
        )
    finally:
        os.close(target_fd)


def _claimed_generation_matches(
    expected: tuple[tuple[object, ...], ...] | None,
    claimed: tuple[tuple[object, ...], ...] | None,
) -> bool:
    """Compare a claimed backup, allowing its root ctime/mtime rename effects."""

    if expected is None or claimed is None or not expected or not claimed:
        return expected == claimed
    return expected[0][:5] == claimed[0][:5] and expected[1:] == claimed[1:]


@contextmanager
def _target_transaction_lock(parent_fd: int, target_name: str) -> Iterator[None]:
    """Acquire nonblocking parent-FD lock plus in-process same-target registry."""

    metadata = os.fstat(parent_fd)
    key = (metadata.st_dev, metadata.st_ino, target_name)
    with _TARGET_REGISTRY_GUARD:
        if key in _TARGET_REGISTRY:
            raise StagingConflictError(
                f"Ziel ist bereits in einer Stagingtransaktion: {target_name}"
            )
        _TARGET_REGISTRY.add(key)
    locked = False
    try:
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise StagingConflictError(f"Staging-Parent ist belegt: {target_name}") from exc
        except OSError as exc:
            if exc.errno in {errno.ENOLCK, errno.EOPNOTSUPP}:
                raise StagingUnsupportedError("Staging-Parent-Lock wird nicht unterstützt") from exc
            raise
        yield
    finally:
        if locked:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
        with _TARGET_REGISTRY_GUARD:
            _TARGET_REGISTRY.discard(key)


def _directory_identity(directory_fd: int) -> tuple[int, int, int]:
    """Return immutable ownership identity for one open directory."""

    metadata = os.fstat(directory_fd)
    if not stat.S_ISDIR(metadata.st_mode):
        raise StagingError("Publiziertes Ziel ist kein Verzeichnis")
    return metadata.st_dev, metadata.st_ino, metadata.st_mode


def _named_identity_matches(parent_fd: int, name: str, identity: tuple[int, int, int]) -> bool:
    """Bind current descriptor-relative name to a previously owned directory."""

    try:
        descriptor = _open_child_directory(parent_fd, name)
    except FileNotFoundError:
        return False
    try:
        return _directory_identity(descriptor) == identity
    finally:
        os.close(descriptor)


def _claim_owned_target(
    parent_fd: int,
    target_name: str,
    quarantine_name: str,
    owned_fd: int,
) -> int | None:
    """Claim target iff it is the exact publication held by ``owned_fd``."""

    if not entry_exists(parent_fd, target_name):
        return None
    identity = _directory_identity(owned_fd)
    _rename_noreplace(parent_fd, target_name, quarantine_name)
    fsync_directory_fd(parent_fd)
    descriptor = _open_child_directory(parent_fd, quarantine_name)
    try:
        if _directory_identity(descriptor) == identity:
            return descriptor
    except BaseException:
        os.close(descriptor)
        raise
    os.close(descriptor)
    try:
        _rename_noreplace(parent_fd, quarantine_name, target_name)
        fsync_directory_fd(parent_fd)
    except StagingError as exc:
        raise StagingConflictError(
            "Fremdes Ziel konnte nach Quarantäne-Claim nicht zurückgestellt werden"
        ) from exc
    raise StagingConflictError("Target-Name wurde durch eine fremde Generation ersetzt")


def _owned_quarantine_state(
    parent_fd: int,
    quarantine_name: str,
    identity: tuple[int, int, int],
    *,
    missing_state: _QuarantineRemnant,
) -> _QuarantineRemnant:
    """Classify an exact descriptor-relative quarantine binding."""

    try:
        if not entry_exists(parent_fd, quarantine_name):
            return missing_state
        if _named_identity_matches(parent_fd, quarantine_name, identity):
            return _QuarantineRemnant.VERIFIED
    except Exception:
        return _QuarantineRemnant.UNKNOWN
    return _QuarantineRemnant.UNKNOWN


def _rollback_published_target(
    *,
    parent_fd: int,
    target_name: str,
    backup_name: str,
    quarantine_name: str,
    published_fd: int | None,
    staging_published: bool,
    target_moved: bool,
    remove_quarantine: bool,
) -> _RollbackOutcome:
    """Claim own publication, then restore backup regardless of cleanup failures."""

    failures: list[tuple[str, BaseException]] = []
    quarantine_remnant = _QuarantineRemnant.NONE
    if staging_published:
        if published_fd is None:
            failures.append(
                (
                    "claim",
                    StagingConflictError("Publiziertes Ziel besitzt keinen gehaltenen FD"),
                )
            )
        else:
            published_identity = _directory_identity(published_fd)
            try:
                quarantine_fd = _claim_owned_target(
                    parent_fd,
                    target_name,
                    quarantine_name,
                    published_fd,
                )
            except BaseException as exc:
                quarantine_remnant = _owned_quarantine_state(
                    parent_fd,
                    quarantine_name,
                    published_identity,
                    missing_state=_QuarantineRemnant.NONE,
                )
                if quarantine_remnant is _QuarantineRemnant.VERIFIED:
                    staging_published = False
                elif quarantine_remnant is _QuarantineRemnant.NONE:
                    try:
                        if not _named_identity_matches(
                            parent_fd,
                            target_name,
                            published_identity,
                        ):
                            quarantine_remnant = _QuarantineRemnant.UNKNOWN
                    except Exception:
                        quarantine_remnant = _QuarantineRemnant.UNKNOWN
                failures.append(("claim", exc))
            else:
                staging_published = False
                if quarantine_fd is not None:
                    quarantine_identity = _directory_identity(quarantine_fd)
                    quarantine_remnant = _QuarantineRemnant.VERIFIED
                    try:
                        clear_generated_tree_fd(quarantine_fd)
                        if not _named_identity_matches(
                            parent_fd,
                            quarantine_name,
                            quarantine_identity,
                        ):
                            raise StagingConflictError("Eigene Quarantäne wurde parallel ersetzt")
                        elif remove_quarantine:
                            os.rmdir(quarantine_name, dir_fd=parent_fd)
                            quarantine_remnant = _QuarantineRemnant.NONE
                    except BaseException as exc:
                        quarantine_remnant = _owned_quarantine_state(
                            parent_fd,
                            quarantine_name,
                            quarantine_identity,
                            missing_state=_QuarantineRemnant.UNKNOWN,
                        )
                        failures.append(("quarantine_cleanup", exc))
                    finally:
                        os.close(quarantine_fd)

    if target_moved:
        try:
            if not entry_exists(parent_fd, backup_name):
                raise StagingConflictError(f"Rollback-Backup fehlt: {backup_name}")
            if entry_exists(parent_fd, target_name):
                raise StagingConflictError(
                    f"Concurrent Target verhindert sicheren Rollback: {target_name}"
                )
            _rename_noreplace(parent_fd, backup_name, target_name)
            target_moved = False
            fsync_directory_fd(parent_fd)
        except BaseException as exc:
            failures.append(("backup_restore", exc))

    diagnostics = tuple(
        _RollbackDiagnostic(
            target_name=target_name,
            quarantine_name=quarantine_name,
            phase=phase,
            error=error,
            quarantine_remnant=quarantine_remnant,
        )
        for phase, error in failures
    )
    return _RollbackOutcome(staging_published, target_moved, diagnostics)


def _rollback_failure(
    label: str,
    primary: BaseException,
    diagnostics: tuple[_RollbackDiagnostic, ...],
) -> StagingError:
    """Keep primary visible while attaching structured secondary diagnostics."""

    rendered = " | ".join(diagnostic.render() for diagnostic in diagnostics)
    return StagingError(
        f"{label}; Primärfehler={type(primary).__name__}: {primary}; Rollbackdiagnostik={rendered}"
    )


def _normal_rollback_failure_or_note(
    label: str,
    primary: BaseException,
    diagnostics: tuple[_RollbackDiagnostic, ...],
) -> StagingError | None:
    """Wrap normal failures, but preserve control-flow exception identity."""

    if isinstance(primary, Exception):
        return _rollback_failure(label, primary, diagnostics)
    rendered = " | ".join(diagnostic.render() for diagnostic in diagnostics)
    primary.add_note(f"{label}; Rollbackdiagnostik={rendered}")
    return None


@contextmanager
def staged_directory(
    target: Path,
    *,
    allowed_root: Path,
    force: bool = False,
    replace_existing: bool = True,
    state: StagingState | None = None,
    publication_validator: Callable[[int], None] | None = None,
) -> Iterator[Path]:
    """Build a generated directory and atomically replace *target*.

    Every create, rename, validation, cleanup, and rollback operation is relative
    to a held parent-directory descriptor beneath ``allowed_root``. The yielded
    path is backed by the open staging descriptor, so a concurrent ancestor
    rename or symlink substitution cannot redirect caller writes.
    """

    relative = relative_path_beneath(target, allowed_root)
    target_name = relative.name
    staging_name = f".{target_name}.staging-{uuid.uuid4().hex}"
    backup_name = f".{target_name}.backup"
    quarantine_name = f".{target_name}.quarantine-{uuid.uuid4().hex}"
    publication_state = state if state is not None else StagingState()
    publication_state.published = False
    publication_state.no_change = False
    publication_state.no_change_validated = False

    with open_root_directory(allowed_root, create=True) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1], create=True)
        transaction_lock = _target_transaction_lock(parent_fd, target_name)
        try:
            transaction_lock.__enter__()
        except BaseException:
            os.close(parent_fd)
            raise
        staging_fd: int | None = None
        published_fd: int | None = None
        staging_created = False
        staging_marked = False
        target_moved = False
        staging_published = False
        publication_committed = False
        published_identity: tuple[int, int, int] | None = None
        try:
            if entry_exists(parent_fd, backup_name):
                _assert_marked_directory(parent_fd, backup_name)
                if not force:
                    raise StagingError(f"Stale Backup existiert: {backup_name}")
                remove_tree_at(parent_fd, backup_name, require_sentinel=True)

            expected_generation = _target_generation(parent_fd, target_name)

            os.mkdir(staging_name, mode=0o755, dir_fd=parent_fd)
            staging_created = True
            staging_fd = _open_child_directory(parent_fd, staging_name)
            mark_generated_root_fd(staging_fd)
            staging_marked = True
            ensure_same_filesystem(staging_fd, parent_fd)
            stage_path = fd_directory_path(staging_fd)

            yield stage_path

            assert_generated_root_fd(staging_fd)
            validate_tree_no_symlinks_fd(staging_fd)

            with _publication_signal_guard():
                if _target_generation(parent_fd, target_name) != expected_generation:
                    raise StagingConflictError(
                        f"Zielgeneration wurde parallel geändert: {target_name}"
                    )
                if publication_state.no_change:
                    publication_state.no_change_validated = True
                    return
                if expected_generation is not None:
                    if not replace_existing:
                        raise StagingConflictError(
                            f"Ziel wurde parallel veröffentlicht: {target_name}"
                        )
                    _assert_marked_directory(parent_fd, target_name)
                    _rename_noreplace(parent_fd, target_name, backup_name)
                    target_moved = True
                    if not _claimed_generation_matches(
                        expected_generation, _target_generation(parent_fd, backup_name)
                    ):
                        _rename_noreplace(parent_fd, backup_name, target_name)
                        target_moved = False
                        raise StagingConflictError(
                            f"Zielgeneration wurde parallel geändert: {target_name}"
                        )
                    fsync_directory_fd(parent_fd)
                _rename_noreplace(parent_fd, staging_name, target_name)
                staging_created = False
                staging_published = True
                publication_state.published = True
                published_fd = staging_fd
                staging_fd = None
                published_identity = _directory_identity(published_fd)
                if publication_validator is not None:
                    publication_validator(published_fd)
                if not _named_identity_matches(parent_fd, target_name, published_identity):
                    raise StagingConflictError(
                        f"Publiziertes Ziel wurde parallel ersetzt: {target_name}"
                    )
                fsync_directory_fd(parent_fd)
                publication_committed = True

            if entry_exists(parent_fd, backup_name):
                try:
                    remove_tree_at(parent_fd, backup_name, require_sentinel=True)
                except BaseException as cleanup_error:
                    raise StagingError(
                        "Neues Ziel wurde sicher veröffentlicht, aber das alte Backup "
                        f"konnte nicht vollständig entfernt werden: {cleanup_error}"
                    ) from cleanup_error
                target_moved = False

            if entry_exists(parent_fd, backup_name):
                raise StagingError(f"Backup blieb nach Stagingtransaktion bestehen: {backup_name}")
        except BaseException as primary:
            if staging_fd is not None:
                os.close(staging_fd)
                staging_fd = None
            if publication_committed:
                # A later backup-cleanup error must never replace the durable new
                # target with a potentially partially removed old backup.
                raise
            outcome = _rollback_published_target(
                parent_fd=parent_fd,
                target_name=target_name,
                backup_name=backup_name,
                quarantine_name=quarantine_name,
                published_fd=published_fd,
                staging_published=staging_published,
                target_moved=target_moved,
                remove_quarantine=False,
            )
            staging_published = outcome.staging_published
            target_moved = outcome.target_moved
            publication_state.published = staging_published
            if outcome.diagnostics:
                failure = _normal_rollback_failure_or_note(
                    "Staging fehlgeschlagen und Rollbackdiagnostik liegt vor",
                    primary,
                    outcome.diagnostics,
                )
                if failure is not None:
                    raise failure from primary
            raise
        finally:
            if staging_fd is not None:
                os.close(staging_fd)
            cleanup_error: BaseException | None = None
            if staging_created and entry_exists(parent_fd, staging_name):
                try:
                    remove_tree_at(
                        parent_fd,
                        staging_name,
                        require_sentinel=staging_marked,
                    )
                except BaseException as exc:
                    cleanup_error = exc
            active_error = sys.exc_info()[0] is not None
            try:
                transaction_lock.__exit__(*sys.exc_info())
            finally:
                if published_fd is not None:
                    os.close(published_fd)
                os.close(parent_fd)
            if cleanup_error is not None:
                if active_error:
                    current = sys.exc_info()[1]
                    if current is not None:
                        current.add_note(f"Zusätzlicher Staging-Cleanupfehler: {cleanup_error}")
                else:
                    raise StagingError(
                        f"Stagingverzeichnis konnte nicht bereinigt werden: {cleanup_error}"
                    ) from cleanup_error


@contextmanager
def coordinated_staged_directories(
    targets: tuple[Path, ...],
    *,
    allowed_root: Path,
    force: bool = False,
) -> Iterator[tuple[Path, ...]]:
    """Stage and publish non-overlapping generated trees as one rollback unit."""

    relatives = tuple(relative_path_beneath(target, allowed_root) for target in targets)
    if not relatives or len(set(relatives)) != len(relatives):
        raise StagingError("Koordinierte Publikationsziele müssen eindeutig sein")
    for index, left in enumerate(relatives):
        for right in relatives[index + 1 :]:
            if (
                left.parts[: len(right.parts)] == right.parts
                or right.parts[: len(left.parts)] == left.parts
            ):
                raise StagingError("Koordinierte Publikationsziele dürfen nicht überlappen")

    transactions: list[_CoordinatedTarget] = []
    locks: list[object] = []
    parent_fds: list[int] = []
    committed = False
    with open_root_directory(allowed_root, create=True) as root_fd:
        try:
            parent_identities: set[tuple[int, int]] = set()
            for relative in relatives:
                parent_fd = open_directory_beneath(root_fd, relative.parts[:-1], create=True)
                metadata = os.fstat(parent_fd)
                identity = (metadata.st_dev, metadata.st_ino)
                if identity in parent_identities:
                    os.close(parent_fd)
                    raise StagingError(
                        "Koordinierte Publikationsziele benötigen verschiedene Parentverzeichnisse"
                    )
                parent_identities.add(identity)
                target_name = relative.name
                lock = _target_transaction_lock(parent_fd, target_name)
                try:
                    lock.__enter__()
                except BaseException:
                    os.close(parent_fd)
                    raise
                locks.append(lock)
                parent_fds.append(parent_fd)
                backup_name = f".{target_name}.backup"
                if entry_exists(parent_fd, backup_name):
                    _assert_marked_directory(parent_fd, backup_name)
                    if not force:
                        raise StagingError(f"Stale Backup existiert: {backup_name}")
                    remove_tree_at(parent_fd, backup_name, require_sentinel=True)
                expected = _target_generation(parent_fd, target_name)
                if expected is not None:
                    _assert_marked_directory(parent_fd, target_name)
                staging_name = f".{target_name}.staging-{uuid.uuid4().hex}"
                os.mkdir(staging_name, mode=0o755, dir_fd=parent_fd)
                try:
                    staging_fd = _open_child_directory(parent_fd, staging_name)
                except BaseException:
                    remove_tree_at(parent_fd, staging_name, require_sentinel=False)
                    raise
                try:
                    mark_generated_root_fd(staging_fd)
                    ensure_same_filesystem(staging_fd, parent_fd)
                except BaseException:
                    os.close(staging_fd)
                    remove_tree_at(parent_fd, staging_name, require_sentinel=False)
                    raise
                transactions.append(
                    _CoordinatedTarget(
                        relative=relative,
                        parent_fd=parent_fd,
                        target_name=target_name,
                        staging_name=staging_name,
                        backup_name=backup_name,
                        quarantine_name=f".{target_name}.quarantine-{uuid.uuid4().hex}",
                        expected_generation=expected,
                        staging_fd=staging_fd,
                    )
                )

            yield tuple(fd_directory_path(item.staging_fd) for item in transactions)

            for item in transactions:
                assert_generated_root_fd(item.staging_fd)
                validate_tree_no_symlinks_fd(item.staging_fd)
                if _target_generation(item.parent_fd, item.target_name) != item.expected_generation:
                    raise StagingConflictError(
                        f"Zielgeneration wurde parallel geändert: {item.target_name}"
                    )

            with _publication_signal_guard():
                for item in transactions:
                    if (
                        _target_generation(item.parent_fd, item.target_name)
                        != item.expected_generation
                    ):
                        raise StagingConflictError(
                            f"Zielgeneration wurde parallel geändert: {item.target_name}"
                        )
                    if item.expected_generation is not None:
                        _rename_noreplace(
                            item.parent_fd,
                            item.target_name,
                            item.backup_name,
                        )
                        item.target_moved = True
                        if not _claimed_generation_matches(
                            item.expected_generation,
                            _target_generation(item.parent_fd, item.backup_name),
                        ):
                            _rename_noreplace(
                                item.parent_fd,
                                item.backup_name,
                                item.target_name,
                            )
                            item.target_moved = False
                            raise StagingConflictError(
                                f"Zielgeneration wurde parallel geändert: {item.target_name}"
                            )
                        fsync_directory_fd(item.parent_fd)
                    _rename_noreplace(
                        item.parent_fd,
                        item.staging_name,
                        item.target_name,
                    )
                    item.staging_published = True
                    item.published_identity = _directory_identity(item.staging_fd)
                    if not _named_identity_matches(
                        item.parent_fd,
                        item.target_name,
                        item.published_identity,
                    ):
                        raise StagingConflictError(
                            f"Publiziertes Ziel wurde parallel ersetzt: {item.target_name}"
                        )
                    fsync_directory_fd(item.parent_fd)
                    _coordinated_publication_checkpoint(item.relative)
                for item in transactions:
                    assert item.published_identity is not None
                    if not _named_identity_matches(
                        item.parent_fd,
                        item.target_name,
                        item.published_identity,
                    ):
                        raise StagingConflictError(
                            f"Publiziertes Ziel wurde parallel ersetzt: {item.target_name}"
                        )
                committed = True

            for item in transactions:
                if entry_exists(item.parent_fd, item.backup_name):
                    try:
                        remove_tree_at(
                            item.parent_fd,
                            item.backup_name,
                            require_sentinel=True,
                        )
                    except BaseException as cleanup_error:
                        raise StagingError(
                            "Neue Ziele wurden sicher veröffentlicht, aber ein altes Backup "
                            f"konnte nicht vollständig entfernt werden: {cleanup_error}"
                        ) from cleanup_error
                    item.target_moved = False
        except BaseException as primary:
            if not committed:
                rollback_diagnostics: list[_RollbackDiagnostic] = []
                for item in reversed(transactions):
                    outcome = _rollback_published_target(
                        parent_fd=item.parent_fd,
                        target_name=item.target_name,
                        backup_name=item.backup_name,
                        quarantine_name=item.quarantine_name,
                        published_fd=item.staging_fd,
                        staging_published=item.staging_published,
                        target_moved=item.target_moved,
                        remove_quarantine=True,
                    )
                    item.staging_published = outcome.staging_published
                    item.target_moved = outcome.target_moved
                    rollback_diagnostics.extend(outcome.diagnostics)
                if rollback_diagnostics:
                    failure = _normal_rollback_failure_or_note(
                        "Koordinierte Publikation fehlgeschlagen und Rollbackdiagnostik liegt vor",
                        primary,
                        tuple(rollback_diagnostics),
                    )
                    if failure is not None:
                        raise failure from primary
            raise
        finally:
            cleanup_error: BaseException | None = None
            for item in transactions:
                if not item.staging_published and entry_exists(item.parent_fd, item.staging_name):
                    try:
                        remove_tree_at(
                            item.parent_fd,
                            item.staging_name,
                            require_sentinel=True,
                        )
                    except BaseException as exc:
                        cleanup_error = cleanup_error or exc
                os.close(item.staging_fd)
            active = sys.exc_info()
            for lock in reversed(locks):
                try:
                    lock.__exit__(*active)  # type: ignore[attr-defined]
                except BaseException as exc:
                    cleanup_error = cleanup_error or exc
            for parent_fd in parent_fds:
                os.close(parent_fd)
            if cleanup_error is not None:
                if active[1] is not None:
                    active[1].add_note(
                        f"Zusätzlicher koordinierter Staging-Cleanupfehler: {cleanup_error}"
                    )
                else:
                    raise StagingError(
                        f"Koordiniertes Staging konnte nicht bereinigt werden: {cleanup_error}"
                    ) from cleanup_error


@contextmanager
def preview_directory(target: Path, *, allowed_root: Path) -> Iterator[Path]:
    """Yield an FD-anchored generated directory without publishing it."""

    state = StagingState()
    with staged_directory(target, allowed_root=allowed_root, state=state) as stage:
        state.no_change = True
        yield stage
