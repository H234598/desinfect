"""Tests for the offline fixture manifest policy."""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest

from scripts.validate_fixture_manifest import FIXTURE_ROOT, MANIFEST, validate


def test_repository_fixture_manifest_is_valid() -> None:
    """Validate the committed synthetic fixture corpus."""

    validate()


def _copy_fixture_tree(tmp_path: Path) -> tuple[Path, Path]:
    """Copy the committed fixture corpus into an isolated test directory."""

    root = tmp_path / "fixtures"
    shutil.copytree(FIXTURE_ROOT, root)
    return root, root / "manifest.json"


def test_fixture_hash_drift_is_rejected(tmp_path: Path) -> None:
    """A changed fixture must update its manifest explicitly."""

    root, manifest = _copy_fixture_tree(tmp_path)
    (root / "rights" / "unknown.json").write_text('{"state":"approved"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="Hash driftet|Größe driftet"):
        validate(root, manifest)


def test_unregistered_fixture_is_rejected(tmp_path: Path) -> None:
    """No payload may appear outside the reviewed fixture manifest."""

    root, manifest = _copy_fixture_tree(tmp_path)
    (root / "extra.txt").write_text("not registered", encoding="utf-8")
    with pytest.raises(ValueError, match="extras"):
        validate(root, manifest)


def test_fixture_symlink_is_rejected(tmp_path: Path) -> None:
    """Symlinks cannot smuggle external data into the fixture corpus."""

    root, manifest = _copy_fixture_tree(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("external", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    with pytest.raises(ValueError, match="Symlink"):
        validate(root, manifest)
