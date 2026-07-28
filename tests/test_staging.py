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
