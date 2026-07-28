"""Tests for the offline fixture manifest policy."""

from __future__ import annotations

import hashlib
import json
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
    with pytest.raises(ValueError, match=r"Hash driftet|Größe driftet"):
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


def test_manifest_cannot_raise_the_canonical_size_cap(tmp_path: Path) -> None:
    """The reviewed 64 KiB cap cannot be weakened by editing manifest data."""

    root, manifest = _copy_fixture_tree(tmp_path)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["max_file_bytes"] = 65_537
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="65536"):
        validate(root, manifest)


@pytest.mark.parametrize("prefix", ["ghp", "gho", "ghu", "ghs", "ghr"])
def test_all_github_token_families_are_rejected(tmp_path: Path, prefix: str) -> None:
    """Detect personal, OAuth, user, server, and refresh token prefixes."""

    root, manifest = _copy_fixture_tree(tmp_path)
    path = root / "rights" / "unknown.json"
    payload = f"{prefix}_".encode() + b"A" * 24
    path.write_bytes(payload)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    entry = next(item for item in data["entries"] if item["path"] == "rights/unknown.json")
    entry["bytes"] = len(payload)
    entry["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Secretmuster"):
        validate(root, manifest)
