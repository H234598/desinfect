"""Tests for sentinel-protected directory staging and rollback."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil

import pytest

from scripts.rki_pipeline.io_utils import UnsafePathError, mark_generated_root
from scripts.rki_pipeline import staging as staging_module
from scripts.rki_pipeline.staging import (
    StagingConflictError,
    StagingError,
    StagingUnsupportedError,
    staged_directory,
)


def _generated(path: Path, payload: str) -> None:
    """Create a small sentinel-marked generated directory for a test."""

    mark_generated_root(path)
    (path / "value.txt").write_text(payload, encoding="utf-8")


def test_staged_directory_publishes_complete_tree(tmp_path: Path) -> None:
    """Replace a marked target only after the context exits successfully."""

    target = tmp_path / "site"
    _generated(target, "old")
    with staged_directory(target, allowed_root=tmp_path) as stage:
        (stage / "value.txt").write_text("new", encoding="utf-8")
        assert (target / "value.txt").read_text(encoding="utf-8") == "old"
    assert (target / "value.txt").read_text(encoding="utf-8") == "new"


def test_staged_directory_failure_preserves_old_tree(tmp_path: Path) -> None:
    """An exception inside staging leaves the previous target untouched."""

    target = tmp_path / "site"
    _generated(target, "old")
    with pytest.raises(RuntimeError, match="injected"):
        with staged_directory(target, allowed_root=tmp_path) as stage:
            (stage / "value.txt").write_text("new", encoding="utf-8")
            raise RuntimeError("injected")
    assert (target / "value.txt").read_text(encoding="utf-8") == "old"


def test_staged_directory_rejects_unmarked_target(tmp_path: Path) -> None:
    """Never replace a directory that lacks the desinfect sentinel."""

    target = tmp_path / "site"
    target.mkdir()
    (target / "private.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(UnsafePathError):
        with staged_directory(target, allowed_root=tmp_path):
            pass
    assert (target / "private.txt").read_text(encoding="utf-8") == "keep"


def test_staged_directory_rejects_cross_device(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a replacement if staging and target parent appear on different devices."""

    target = tmp_path / "site"
    calls = 0

    def fake_device(path: Path) -> int:
        """Return alternating device identifiers for cross-device simulation."""

        nonlocal calls
        calls += 1
        return calls

    monkeypatch.setattr(staging_module, "_device_id", fake_device)
    with pytest.raises(StagingError, match="Cross-device"):
        with staged_directory(target, allowed_root=tmp_path):
            pass


def test_rename_noreplace_reports_unsupported_kernel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnsupportedRename:
        argtypes = None
        restype = None

        def __call__(self, *args: object) -> int:
            del args
            return -1

    class Library:
        renameat2 = UnsupportedRename()

    monkeypatch.setattr(staging_module.ctypes, "CDLL", lambda *args, **kwargs: Library())
    monkeypatch.setattr(
        staging_module.ctypes,
        "get_errno",
        lambda: staging_module.errno.ENOSYS,
    )

    with pytest.raises(StagingUnsupportedError, match="nicht unterstützt"):
        staging_module._rename_noreplace(3, "source", "target")


def test_staged_directory_cleans_setup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure after creating staging does not leak the owned temporary directory."""

    target = tmp_path / "site"

    def fail_mark(descriptor: int) -> None:
        """Inject a setup failure before the sentinel is written."""

        del descriptor
        raise OSError("injected setup failure")

    monkeypatch.setattr(staging_module, "mark_generated_root_fd", fail_mark)
    with pytest.raises(OSError, match="injected setup failure"):
        with staged_directory(target, allowed_root=tmp_path):
            pass
    assert not list(tmp_path.glob(".site.staging-*"))


def test_staged_directory_detects_deterministic_stale_backup(tmp_path: Path) -> None:
    """A prior-run backup is surfaced and only force-authorized cleanup removes it."""

    target = tmp_path / "site"
    backup = tmp_path / ".site.backup"
    _generated(backup, "stale")

    with pytest.raises(StagingError, match="Stale Backup"):
        with staged_directory(target, allowed_root=tmp_path):
            pass
    assert backup.is_dir()

    with staged_directory(target, allowed_root=tmp_path, force=True) as stage:
        (stage / "value.txt").write_text("new", encoding="utf-8")
    assert not backup.exists()
    assert (target / "value.txt").read_text(encoding="utf-8") == "new"


def test_staged_directory_resists_symlink_ancestor_swap(tmp_path: Path) -> None:
    """Caller writes and final rename remain anchored after an ancestor substitution."""

    root = tmp_path / "root"
    parent = root / "nested"
    parent.mkdir(parents=True)
    target = parent / "site"
    _generated(target, "old")
    outside = tmp_path / "outside"
    outside.mkdir()

    with staged_directory(target, allowed_root=root) as stage:
        (stage / "value.txt").write_text("new", encoding="utf-8")
        parent.rename(root / "nested-real")
        parent.symlink_to(outside, target_is_directory=True)

    assert (root / "nested-real" / "site" / "value.txt").read_text(encoding="utf-8") == "new"
    assert not (outside / "site").exists()


def test_staged_directory_rejects_target_generation_change(tmp_path: Path) -> None:
    """Never replace a target whose nested generation changed during staging."""

    target = tmp_path / "site"
    _generated(target, "old")
    nested = target / "nested"
    nested.mkdir()
    (nested / "value.txt").write_text("old", encoding="utf-8")

    with pytest.raises(StagingConflictError, match=r"parallel|Generation"):
        with staged_directory(target, allowed_root=tmp_path) as stage:
            (stage / "value.txt").write_text("new", encoding="utf-8")
            (nested / "value.txt").replace(nested / "replacement.txt")

    assert (nested / "replacement.txt").read_text(encoding="utf-8") == "old"
    assert (target / "value.txt").read_text(encoding="utf-8") != "new"


def test_staged_directory_validates_explicit_no_change_generation(tmp_path: Path) -> None:
    """No-op skips publication only after target generation is revalidated."""

    target = tmp_path / "site"
    _generated(target, "old")
    state = staging_module.StagingState()
    with staged_directory(target, allowed_root=tmp_path, state=state) as stage:
        (stage / "value.txt").write_text("discard", encoding="utf-8")
        state.no_change = True

    assert state.no_change_validated is True
    assert (target / "value.txt").read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.staging-*"))


def test_staged_directory_rejects_mutation_after_no_change_mark(tmp_path: Path) -> None:
    """A post-signature target mutation cannot return an unvalidated no-op."""

    target = tmp_path / "site"
    _generated(target, "old")
    state = staging_module.StagingState()
    with pytest.raises(StagingConflictError, match="generation"):
        with staged_directory(target, allowed_root=tmp_path, state=state):
            state.no_change = True
            (target / "value.txt").write_text("external", encoding="utf-8")

    assert state.no_change_validated is False
    assert (target / "value.txt").read_text(encoding="utf-8") == "external"


def test_staged_directory_rolls_back_when_publication_validator_rejects(
    tmp_path: Path,
) -> None:
    """A post-rename validator binds published bytes before commit cleanup."""

    target = tmp_path / "site"
    _generated(target, "old")

    def reject(_target_fd: int) -> None:
        raise RuntimeError("signature mismatch")

    with pytest.raises(RuntimeError, match="signature mismatch"):
        with staged_directory(
            target,
            allowed_root=tmp_path,
            publication_validator=reject,
        ) as stage:
            (stage / "value.txt").write_text("new", encoding="utf-8")

    assert (target / "value.txt").read_text(encoding="utf-8") == "old"
    quarantines = list(tmp_path.glob(".site.quarantine-*"))
    assert len(quarantines) == 1
    assert list(quarantines[0].iterdir()) == []


def test_publication_fsync_and_commit_stay_inside_signal_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Signal deferral covers rename through durable publication commit."""

    events: list[str] = []
    real_rename = staging_module._rename_noreplace
    real_fsync = staging_module.fsync_directory_fd

    @contextmanager
    def guard():
        events.append("guard-enter")
        try:
            yield
        finally:
            events.append("guard-exit")

    def rename(parent_fd: int, source: str, target: str) -> None:
        events.append("rename")
        real_rename(parent_fd, source, target)

    def fsync(parent_fd: int) -> None:
        events.append("fsync-commit")
        real_fsync(parent_fd)

    monkeypatch.setattr(staging_module, "_publication_signal_guard", guard)
    monkeypatch.setattr(staging_module, "_rename_noreplace", rename)
    monkeypatch.setattr(staging_module, "fsync_directory_fd", fsync)

    with staged_directory(tmp_path / "site", allowed_root=tmp_path) as stage:
        (stage / "value.txt").write_text("new", encoding="utf-8")

    assert events == ["guard-enter", "rename", "fsync-commit", "guard-exit"]


def test_staged_directory_holds_exact_published_fd_through_adversarial_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deleted publication inode stays pinned; validator FD owns rollback identity."""

    target = tmp_path / "site"
    _generated(target, "old")
    deleted = tmp_path / "deleted-publication"
    validator_fd: int | None = None
    published_inode: int | None = None
    exact_fd_seen = False
    original_claim = staging_module._claim_owned_target

    def reject_and_replace(target_fd: int) -> None:
        nonlocal validator_fd, published_inode
        validator_fd = target_fd
        published_inode = os.fstat(target_fd).st_ino
        target.rename(deleted)
        shutil.rmtree(deleted)
        _generated(target, "foreign")
        assert target.stat().st_ino != published_inode
        raise RuntimeError("validator replaced deleted target")

    def claim(
        parent_fd: int,
        target_name: str,
        quarantine_name: str,
        owned_fd: int,
    ) -> int | None:
        nonlocal exact_fd_seen
        assert owned_fd == validator_fd
        assert os.fstat(owned_fd).st_ino == published_inode
        exact_fd_seen = True
        return original_claim(parent_fd, target_name, quarantine_name, owned_fd)

    monkeypatch.setattr(staging_module, "_claim_owned_target", claim)

    with pytest.raises(StagingError, match="Rollback"):
        with staged_directory(
            target,
            allowed_root=tmp_path,
            publication_validator=reject_and_replace,
        ) as stage:
            (stage / "value.txt").write_text("ours", encoding="utf-8")

    assert (target / "value.txt").read_text(encoding="utf-8") == "foreign"
    assert exact_fd_seen is True
    assert validator_fd is not None
    with pytest.raises(OSError):
        os.fstat(validator_fd)


@pytest.mark.parametrize("validator_raises", (False, True))
def test_staged_directory_never_removes_concurrent_target_after_validator_rename(
    tmp_path: Path, validator_raises: bool
) -> None:
    """Target ownership is rebound after validator-side name replacement."""

    target = tmp_path / "site"
    _generated(target, "old")
    moved = tmp_path / "validator-owned"

    def replace_target(_target_fd: int) -> None:
        target.rename(moved)
        _generated(target, "concurrent")
        if validator_raises:
            raise RuntimeError("validator replaced target")

    with pytest.raises(StagingError):
        with staged_directory(
            target,
            allowed_root=tmp_path,
            publication_validator=replace_target,
        ) as stage:
            (stage / "value.txt").write_text("ours", encoding="utf-8")

    assert (target / "value.txt").read_text(encoding="utf-8") == "concurrent"
    assert moved.is_dir()


def test_staged_directory_never_removes_replaced_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rollback cleanup remains bound to the claimed quarantine inode."""

    target = tmp_path / "site"
    _generated(target, "old")
    ours_away = tmp_path / "ours-away"
    foreign: Path | None = None
    original_claim = staging_module._claim_owned_target

    def swap_after_claim(
        parent_fd: int,
        target_name: str,
        quarantine_name: str,
        owned_fd: int,
    ) -> int | None:
        nonlocal foreign
        descriptor = original_claim(parent_fd, target_name, quarantine_name, owned_fd)
        if descriptor is not None:
            quarantine = tmp_path / quarantine_name
            quarantine.rename(ours_away)
            foreign = quarantine
            _generated(quarantine, "foreign")
        return descriptor

    monkeypatch.setattr(staging_module, "_claim_owned_target", swap_after_claim)

    def reject(_target_fd: int) -> None:
        raise RuntimeError("signature mismatch")

    with pytest.raises(StagingError, match="Rollback"):
        with staged_directory(
            target,
            allowed_root=tmp_path,
            publication_validator=reject,
        ) as stage:
            (stage / "value.txt").write_text("ours", encoding="utf-8")

    assert foreign is not None
    assert (foreign / "value.txt").read_text(encoding="utf-8") == "foreign"
    assert list(ours_away.iterdir()) == []


def test_staged_directory_reports_missing_backup_during_rollback(tmp_path: Path) -> None:
    """A moved-away backup can never masquerade as a successful rollback."""

    target = tmp_path / "site"
    _generated(target, "old")
    moved_backup = tmp_path / "lost-backup"

    def reject(_target_fd: int) -> None:
        (tmp_path / ".site.backup").rename(moved_backup)
        raise RuntimeError("signature mismatch")

    with pytest.raises(StagingError, match="Backup fehlt"):
        with staged_directory(
            target,
            allowed_root=tmp_path,
            publication_validator=reject,
        ) as stage:
            (stage / "value.txt").write_text("new", encoding="utf-8")

    assert not target.exists()
    assert (moved_backup / "value.txt").read_text(encoding="utf-8") == "old"
    quarantines = list(tmp_path.glob(".site.quarantine-*"))
    assert len(quarantines) == 1
    assert list(quarantines[0].iterdir()) == []


def test_staged_directory_rejects_nested_same_target_without_lock_artifact(tmp_path: Path) -> None:
    """Same-target reentry fails fast and leaves no persistent lock file."""

    target = tmp_path / "site"
    with staged_directory(target, allowed_root=tmp_path):
        with pytest.raises(StagingConflictError, match="Stagingtransaktion"):
            with staged_directory(target, allowed_root=tmp_path):
                pass

    assert not list(tmp_path.glob("*.lock"))


def test_staged_directory_parent_lock_rejects_sibling_target_fast(tmp_path: Path) -> None:
    """Parent-wide flock serializes sibling publications without lock artifacts."""

    with staged_directory(tmp_path / "site-a", allowed_root=tmp_path):
        with pytest.raises(StagingConflictError, match="Parent"):
            with staged_directory(tmp_path / "site-b", allowed_root=tmp_path):
                pass

    assert not list(tmp_path.glob("*.lock"))


@pytest.mark.parametrize(
    "error_number",
    (staging_module.errno.ENOLCK, staging_module.errno.EOPNOTSUPP),
)
def test_staged_directory_reports_unsupported_parent_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    """Unsupported filesystem locking is a stable staging contract error."""

    def unsupported_lock(_descriptor: int, _operation: int) -> None:
        raise OSError(error_number, "unsupported lock")

    monkeypatch.setattr(staging_module.fcntl, "flock", unsupported_lock)

    with pytest.raises(StagingUnsupportedError, match="nicht unterstützt"):
        with staged_directory(tmp_path / "site", allowed_root=tmp_path):
            pass


def test_staged_directory_preserves_unrelated_parent_lock_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Descriptor and permission failures keep their original OSError meaning."""

    def invalid_descriptor(_descriptor: int, _operation: int) -> None:
        raise OSError(staging_module.errno.EBADF, "invalid descriptor")

    monkeypatch.setattr(staging_module.fcntl, "flock", invalid_descriptor)

    with pytest.raises(OSError) as error:
        with staged_directory(tmp_path / "site", allowed_root=tmp_path):
            pass

    assert error.value.errno == staging_module.errno.EBADF


def test_staged_directory_clears_publication_state_when_owned_target_vanishes(
    tmp_path: Path,
) -> None:
    """Restored backup reports no publication after validator removes new target."""

    target = tmp_path / "site"
    removed = tmp_path / "removed-publication"
    _generated(target, "old")
    state = staging_module.StagingState()

    def remove_and_reject(_target_fd: int) -> None:
        target.rename(removed)
        shutil.rmtree(removed)
        raise RuntimeError("validator removed target")

    with pytest.raises(RuntimeError, match="validator removed target"):
        with staged_directory(
            target,
            allowed_root=tmp_path,
            state=state,
            publication_validator=remove_and_reject,
        ) as stage:
            (stage / "value.txt").write_text("new", encoding="utf-8")

    assert state.published is False
    assert (target / "value.txt").read_text(encoding="utf-8") == "old"


def test_staged_directory_rejects_symlink_in_generated_output(tmp_path: Path) -> None:
    """A generated output tree containing a symlink is never published."""

    target = tmp_path / "site"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(UnsafePathError, match="Symlink"):
        with staged_directory(target, allowed_root=tmp_path) as stage:
            (stage / "link.txt").symlink_to(outside)
    assert not target.exists()
