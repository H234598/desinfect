"""Tests for safe path, hash, JSON, and atomic file primitives."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from scripts.rki_pipeline import io_utils
from scripts.rki_pipeline.io_utils import (
    PathCollisionError,
    UnsafePathError,
    atomic_write_bytes,
    detect_path_collisions,
    ensure_within,
    normalize_posix_path,
    sha256_file,
    stable_json_dumps,
)


def test_normalize_posix_path_rejects_unsafe_forms() -> None:
    """Reject absolute, traversal, Windows, NUL, and backslash syntax."""

    for value in (
        "/etc/passwd",
        "../escape",
        "a/../b",
        "a//b",
        "a/",
        "C:/temp/x",
        "a\\b",
        "a\x00b",
    ):
        with pytest.raises(UnsafePathError):
            normalize_posix_path(value)


def test_normalize_posix_path_uses_nfc() -> None:
    """Normalize canonically equivalent Unicode spellings."""

    assert normalize_posix_path("Cafe\u0301/file.txt") == "Café/file.txt"


def test_detect_path_collisions_rejects_casefold_and_unicode_aliases() -> None:
    """Detect cross-platform collisions before durable files are created."""

    with pytest.raises(PathCollisionError):
        detect_path_collisions(["Data/File.txt", "data/file.TXT"])
    with pytest.raises(PathCollisionError):
        detect_path_collisions(["Cafe\u0301/x", "Café/X"])


def test_ensure_within_rejects_escape_and_symlink(tmp_path: Path) -> None:
    """Reject root escape and any existing symlink component."""

    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(UnsafePathError):
        ensure_within(Path("../escape"), root)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        ensure_within(Path("link/file.txt"), root)


def test_sha256_file_streams_and_validates_chunk_size(tmp_path: Path) -> None:
    """Hash large data correctly without accepting an invalid chunk size."""

    payload = b"desinfect" * 10000
    path = tmp_path / "payload.bin"
    path.write_bytes(payload)
    assert sha256_file(path, chunk_size=7) == hashlib.sha256(payload).hexdigest()
    with pytest.raises(ValueError):
        sha256_file(path, chunk_size=0)


def test_stable_json_is_sorted_utf8_and_newline_terminated() -> None:
    """Produce deterministic human-reviewable JSON."""

    assert stable_json_dumps({"z": 1, "a": "Ä"}) == '{\n  "a": "Ä",\n  "z": 1\n}\n'


def test_atomic_write_replaces_file_and_removes_part(tmp_path: Path) -> None:
    """Replace a file atomically and leave no temporary sibling behind."""

    target = tmp_path / "value.txt"
    target.write_text("old", encoding="utf-8")
    atomic_write_bytes(target, b"new", allowed_root=tmp_path)
    assert target.read_bytes() == b"new"
    assert not list(tmp_path.glob("*.part"))
    assert not list(tmp_path.glob(".*.part"))


def test_atomic_write_failure_preserves_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fault injection before replace must preserve the old complete target."""

    target = tmp_path / "value.txt"
    target.write_text("old", encoding="utf-8")
    original_replace = os.replace

    def fail_replace(source: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
        """Simulate an atomic replacement failure before the target changes."""

        del source, destination
        raise OSError("injected replace failure")

    monkeypatch.setattr(io_utils.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        atomic_write_bytes(target, b"new", allowed_root=tmp_path)
    monkeypatch.setattr(io_utils.os, "replace", original_replace)
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".*.part"))
