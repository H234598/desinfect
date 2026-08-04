#!/usr/bin/env python3
"""FD-anchored, sentinel-protected directory staging with rollback."""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
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
        raise StagingUnsupportedError(
            "Atomare NOREPLACE-Veröffentlichung wird nicht unterstützt"
        )
    raise StagingError(
        f"Atomare NOREPLACE-Veröffentlichung fehlgeschlagen: {os.strerror(error)}"
    )


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
    if tuple(sorted(os.listdir(directory_fd))) != names or not _metadata_matches(
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
        return (("", metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size,
                 metadata.st_mtime_ns, metadata.st_ctime_ns), *_directory_generation(target_fd))
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
            raise StagingConflictError(f"Ziel ist bereits in einer Stagingtransaktion: {target_name}")
        _TARGET_REGISTRY.add(key)
    locked = False
    try:
        try:
            fcntl.flock(parent_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError as exc:
            raise StagingConflictError(f"Staging-Parent ist belegt: {target_name}") from exc
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
    identity: tuple[int, int, int],
) -> int | None:
    """Claim target and hold its quarantine descriptor through cleanup."""

    if not entry_exists(parent_fd, target_name):
        return None
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
        raise StagingConflictError("Fremdes Ziel konnte nach Quarantäne-Claim nicht zurückgestellt werden") from exc
    raise StagingConflictError("Target-Name wurde durch eine fremde Generation ersetzt")


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
            os.close(staging_fd)
            staging_fd = None

            with _publication_signal_guard():
                if _target_generation(parent_fd, target_name) != expected_generation:
                    raise StagingConflictError(f"Zielgeneration wurde parallel geändert: {target_name}")
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
                target_fd = _open_child_directory(parent_fd, target_name)
                try:
                    published_identity = _directory_identity(target_fd)
                finally:
                    os.close(target_fd)
                if publication_validator is not None:
                    target_fd = _open_child_directory(parent_fd, target_name)
                    try:
                        publication_validator(target_fd)
                    finally:
                        os.close(target_fd)
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
        except BaseException:
            if staging_fd is not None:
                os.close(staging_fd)
                staging_fd = None
            if publication_committed:
                # A later backup-cleanup error must never replace the durable new
                # target with a potentially partially removed old backup.
                raise
            try:
                rollback_error: BaseException | None = None
                if staging_published and published_identity is not None:
                    quarantine_fd = _claim_owned_target(
                        parent_fd,
                        target_name,
                        quarantine_name,
                        published_identity,
                    )
                    if quarantine_fd is not None:
                        try:
                            clear_generated_tree_fd(quarantine_fd)
                            if not _named_identity_matches(
                                parent_fd,
                                quarantine_name,
                                published_identity,
                            ):
                                rollback_error = StagingConflictError(
                                    "Eigene Quarantäne wurde parallel ersetzt"
                                )
                            staging_published = False
                            publication_state.published = False
                        finally:
                            os.close(quarantine_fd)
                if target_moved:
                    if not entry_exists(parent_fd, backup_name):
                        rollback_error = rollback_error or StagingConflictError(
                            f"Rollback-Backup fehlt: {backup_name}"
                        )
                    elif entry_exists(parent_fd, target_name):
                        rollback_error = rollback_error or StagingConflictError(
                            f"Concurrent Target verhindert sicheren Rollback: {target_name}"
                        )
                    else:
                        _rename_noreplace(parent_fd, backup_name, target_name)
                        target_moved = False
                        fsync_directory_fd(parent_fd)
                if rollback_error is not None:
                    raise rollback_error
            except BaseException as rollback_failure:
                raise StagingError(
                    "Staging fehlgeschlagen und Rollback konnte nicht sicher "
                    f"abgeschlossen werden: {rollback_failure}"
                ) from rollback_failure
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
            transaction_lock.__exit__(*sys.exc_info())
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
