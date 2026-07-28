#!/usr/bin/env python3
"""Sentinel-protected, same-filesystem directory staging with rollback."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
from typing import Iterator
import uuid

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
    assert_generated_root,
    ensure_within,
    fsync_directory,
    mark_generated_root,
    safe_remove_generated_tree,
)


class StagingError(RuntimeError):
    """A staging transaction cannot safely reach or restore its target."""


def _device_id(path: Path) -> int:
    """Return the filesystem device identifier for a path."""

    return path.stat().st_dev


def ensure_same_filesystem(left: Path, right: Path) -> None:
    """Reject a non-atomic cross-device directory replacement."""

    if _device_id(left) != _device_id(right):
        raise StagingError(f"Cross-device-Austausch ist nicht atomar: {left} -> {right}")


@contextmanager
def staged_directory(
    target: Path,
    *,
    allowed_root: Path,
    force: bool = False,
) -> Iterator[Path]:
    """Build a generated directory in staging and replace *target* atomically.

    Existing targets and stale backups must carry the desinfect sentinel. Any
    exception restores the previous complete target. Staging is created beside
    the target so ``os.replace`` remains on one filesystem.
    """

    allowed_root = allowed_root.absolute()
    allowed_root.mkdir(parents=True, exist_ok=True)
    target = ensure_within(target, allowed_root)
    if target == allowed_root.resolve(strict=True):
        raise UnsafePathError("Stagingziel darf nicht der erlaubte Wurzelpfad selbst sein")
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.staging-", dir=target.parent))
    backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
    mark_generated_root(staging)
    ensure_same_filesystem(staging, target.parent)
    target_moved = False
    staging_published = False

    try:
        yield staging
        assert_generated_root(staging)
        if target.exists():
            assert_generated_root(target)
            if backup.exists():
                if not force:
                    raise StagingError(f"Stale Backup existiert: {backup}")
                safe_remove_generated_tree(backup, allowed_root)
            os.replace(target, backup)
            target_moved = True
            fsync_directory(target.parent)
        os.replace(staging, target)
        staging_published = True
        fsync_directory(target.parent)
        if backup.exists():
            safe_remove_generated_tree(backup, allowed_root)
    except BaseException:
        try:
            if staging_published and target.exists():
                assert_generated_root(target)
                safe_remove_generated_tree(target, allowed_root)
            if target_moved and backup.exists():
                os.replace(backup, target)
                fsync_directory(target.parent)
        except BaseException as rollback_error:
            message = (
                "Staging fehlgeschlagen und Rollback konnte nicht sicher "
                f"abgeschlossen werden: {rollback_error}"
            )
            raise StagingError(message) from rollback_error
        raise
    finally:
        if staging.exists():
            safe_remove_generated_tree(staging, allowed_root)
        if backup.exists():
            # A leftover backup means the transaction did not prove a safe final
            # state. Preserve it unless ``force`` explicitly authorizes cleanup.
            if force:
                safe_remove_generated_tree(backup, allowed_root)
            else:
                raise StagingError(f"Backup blieb nach Stagingtransaktion bestehen: {backup}")
