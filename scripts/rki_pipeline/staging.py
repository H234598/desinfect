#!/usr/bin/env python3
"""FD-anchored, sentinel-protected directory staging with rollback."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sys
from typing import Iterator
import uuid

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
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


@contextmanager
def staged_directory(
    target: Path,
    *,
    allowed_root: Path,
    force: bool = False,
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

    with open_root_directory(allowed_root, create=True) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1], create=True)
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

            if entry_exists(parent_fd, target_name):
                _assert_marked_directory(parent_fd, target_name)
                os.replace(
                    target_name,
                    backup_name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                )
                target_moved = True
                fsync_directory_fd(parent_fd)

            os.replace(
                staging_name,
                target_name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            staging_created = False
            staging_published = True
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
                if target_moved and entry_exists(parent_fd, backup_name):
                    os.replace(
                        backup_name,
                        target_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
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
