"""Tests for safe path, hash, JSON, and atomic file primitives."""

from __future__ import annotations

from contextlib import contextmanager
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

    def fail_replace(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        **kwargs: object,
    ) -> None:
        """Simulate an atomic replacement failure before the target changes."""

        del source, destination, kwargs
        raise OSError("injected replace failure")

    monkeypatch.setattr(io_utils.os, "replace", fail_replace)
    with pytest.raises(OSError, match="injected"):
        atomic_write_bytes(target, b"new", allowed_root=tmp_path)
    monkeypatch.setattr(io_utils.os, "replace", original_replace)
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".*.part"))


def test_atomic_write_resists_symlink_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A held parent FD keeps a concurrent ancestor swap inside the original root."""

    root = tmp_path / "root"
    original_parent = root / "nested"
    original_parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = original_parent / "value.txt"
    original_replace = os.replace
    swapped = False

    def race_replace(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        **kwargs: object,
    ) -> None:
        """Swap the pathname ancestor immediately before descriptor-relative replace."""

        nonlocal swapped
        if not swapped:
            swapped = True
            original_parent.rename(root / "nested-real")
            original_parent.symlink_to(outside, target_is_directory=True)
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr(io_utils.os, "replace", race_replace)
    atomic_write_bytes(target, b"anchored", allowed_root=root)

    assert (root / "nested-real" / "value.txt").read_bytes() == b"anchored"
    assert not (outside / "value.txt").exists()


def test_atomic_write_accepts_exact_fd_root_after_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An FD-backed root stays bound when its pathname ancestor becomes a symlink."""

    root = tmp_path / "root"
    stage = root / "stage"
    stage.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    original_root = root
    root_real = tmp_path / "root-real"
    original_open_root = io_utils.open_root_directory
    swapped = False

    @contextmanager
    def swap_ancestor(path: Path, *, create: bool = False):
        nonlocal swapped
        if not swapped:
            swapped = True
            original_root.rename(root_real)
            original_root.symlink_to(outside, target_is_directory=True)
        with original_open_root(path, create=create) as descriptor:
            yield descriptor

    with io_utils.open_root_directory(stage) as descriptor:
        fd_root = io_utils.fd_directory_path(descriptor)
        target = fd_root / "value.txt"
        monkeypatch.setattr(io_utils, "open_root_directory", swap_ancestor)
        atomic_write_bytes(target, b"anchored", allowed_root=fd_root)

    assert (root_real / "stage/value.txt").read_bytes() == b"anchored"
    assert not (outside / "value.txt").exists()


@pytest.mark.parametrize("base", ("/proc/self/fd", "/dev/fd"))
def test_open_root_accepts_exact_directory_fd_alias(tmp_path: Path, base: str) -> None:
    """Both supported FD aliases accept the exact held directory descriptor."""

    if not Path(base).is_dir():
        pytest.skip(f"FD alias unavailable: {base}")
    with io_utils.open_root_directory(tmp_path) as descriptor:
        with io_utils.open_root_directory(Path(base) / str(descriptor)) as duplicate:
            assert os.fstat(duplicate).st_ino == os.fstat(descriptor).st_ino


def test_open_root_rejects_noncanonical_fd_root_forms(tmp_path: Path) -> None:
    """Lexical normalization must never turn a non-FD path into an FD root."""

    with io_utils.open_root_directory(tmp_path) as descriptor:
        fd = str(descriptor)
        invalid = (
            f"/proc/self/fd/0{fd}",
            f"/proc/self/fd/{fd}/extra",
            f"/tmp/../proc/self/fd/{fd}",
            f"/proc/self/fd/{fd}/../{fd}",
            f"/proc/self/fd/{'9' * 5000}",
            f"/proc/self/fd/{2**63}",
        )
        for raw in invalid:
            with pytest.raises(UnsafePathError):
                with io_utils.open_root_directory(Path(raw)):
                    pass

    closed = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    os.close(closed)
    with pytest.raises(UnsafePathError):
        with io_utils.open_root_directory(Path("/proc/self/fd") / str(closed)):
            pass

    file_path = tmp_path / "not-a-directory"
    file_path.write_text("file", encoding="utf-8")
    file_fd = os.open(file_path, os.O_RDONLY)
    try:
        with pytest.raises(UnsafePathError):
            with io_utils.open_root_directory(Path("/proc/self/fd") / str(file_fd)):
                pass
    finally:
        os.close(file_fd)

    symlink = tmp_path / "final-symlink"
    symlink.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(UnsafePathError):
        with io_utils.open_root_directory(symlink):
            pass
