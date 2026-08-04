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
from typing import Callable, Iterator
import uuid

from scripts.rki_pipeline.io_utils import (
    assert_generated_root_fd,
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

    rows: list[tuple[object, ...]] = []
    for name in sorted(os.listdir(directory_fd)):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
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
                rows.extend(_directory_generation(child_fd, relative))
            finally:
                os.close(child_fd)
    return tuple(rows)


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


def _target_lock(parent_fd: int, target_name: str) -> int:
    """Acquire persistent advisory lock for one descriptor-relative target.

    The lock inode intentionally remains in the generated parent directory. It
    is opened no-follow and never unlinked, avoiding a replacement race between
    concurrent publishers that use this staging primitive.
    """

    name = f".{target_name}.lock"
    descriptor = os.open(
        name,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=parent_fd,
    )
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise StagingError(f"Target-Lock ist keine reguläre Datei: {name}")
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    return descriptor


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
    publication_state = state if state is not None else StagingState()
    publication_state.published = False
    publication_state.no_change = False
    publication_state.no_change_validated = False

    with open_root_directory(allowed_root, create=True) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1], create=True)
        lock_fd = _target_lock(parent_fd, target_name)
        staging_fd: int | None = None
        staging_created = False
        staging_marked = False
        target_moved = False
        staging_published = False
        publication_committed = False
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
                if publication_validator is not None:
                    target_fd = _open_child_directory(parent_fd, target_name)
                    try:
                        publication_validator(target_fd)
                    finally:
                        os.close(target_fd)
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
                if staging_published and entry_exists(parent_fd, target_name):
                    remove_tree_at(parent_fd, target_name, require_sentinel=True)
                    staging_published = False
                    publication_state.published = False
                if target_moved and entry_exists(parent_fd, backup_name):
                    _rename_noreplace(parent_fd, backup_name, target_name)
                    target_moved = False
                    fsync_directory_fd(parent_fd)
            except BaseException as rollback_error:
                raise StagingError(
                    "Staging fehlgeschlagen und Rollback konnte nicht sicher "
                    f"abgeschlossen werden: {rollback_error}"
                ) from rollback_error
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
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
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
