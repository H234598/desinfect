"""Tests for sentinel-protected directory staging and rollback."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.rki_pipeline.io_utils import UnsafePathError, mark_generated_root
from scripts.rki_pipeline import staging as staging_module
from scripts.rki_pipeline.staging import StagingError, staged_directory


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


def test_staged_directory_rejects_symlink_in_generated_output(tmp_path: Path) -> None:
    """A generated output tree containing a symlink is never published."""

    target = tmp_path / "site"
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(UnsafePathError, match="Symlink"):
        with staged_directory(target, allowed_root=tmp_path) as stage:
            (stage / "link.txt").symlink_to(outside)
    assert not target.exists()
