#!/usr/bin/env python3
"""Safe, deterministic file, path, and hashing primitives.

Provenance:
- adapted from ``H234598/Cheatsheets:scripts/io_utils.py`` at commit
  ``7db8f713aca07e67b481f9fbcb00553f6a555495`` (blob
  ``28c388e9e36d3642168dfa9cb3a40075cf027dda``);
- the existing ``.part``/``os.replace`` download pattern comes from
  ``H234598/desinfect`` at commit
  ``fbcc6e850fec1f4592ca519fa3e5141b11a95e60``.

The implementation tightens those patterns with Unicode/POSIX normalization,
symlink rejection, parent-directory fsync, collision detection, and a project-
specific generated-root sentinel.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
from typing import Any, Iterable
import unicodedata
import uuid

GENERATED_ROOT_SENTINEL = ".desinfect-generated-root"
DEFAULT_CHUNK_SIZE = 1024 * 1024
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class UnsafePathError(ValueError):
    """A path escapes or weakens an explicitly allowed root boundary."""


class PathCollisionError(UnsafePathError):
    """Two distinct paths collapse under the portable collision key."""


def normalize_posix_path(value: str | Path | PurePosixPath) -> str:
    """Return a portable NFC POSIX path or reject unsafe syntax.

    Absolute paths, traversal, backslashes, NUL bytes, empty components, and
    Windows drive prefixes are rejected before a path can be used as a durable
    identifier or repository-relative location.
    """

    raw = os.fspath(value)
    if not isinstance(raw, str):
        raw = str(raw)
    if "\x00" in raw:
        raise UnsafePathError("Pfad enthält ein NUL-Zeichen")
    if "\\" in raw:
        raise UnsafePathError(f"Backslashes sind in kanonischen Pfaden unzulässig: {raw}")
    if "//" in raw or raw.endswith("/"):
        raise UnsafePathError(f"Leere Pfadkomponente ist unzulässig: {raw}")
    normalized = unicodedata.normalize("NFC", raw)
    if normalized.startswith("/") or _WINDOWS_DRIVE.match(normalized):
        raise UnsafePathError(f"Absoluter Pfad ist unzulässig: {raw}")
    parts = PurePosixPath(normalized).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise UnsafePathError(f"Unsicherer relativer Pfad: {raw}")
    return PurePosixPath(*parts).as_posix()


def portable_collision_key(value: str | Path | PurePosixPath) -> str:
    """Return the NFC/casefold key used to detect cross-platform collisions."""

    return unicodedata.normalize("NFC", normalize_posix_path(value)).casefold()


def detect_path_collisions(values: Iterable[str | Path | PurePosixPath]) -> None:
    """Reject case-insensitive or Unicode-equivalent path collisions."""

    seen: dict[str, str] = {}
    for value in values:
        normalized = normalize_posix_path(value)
        key = portable_collision_key(normalized)
        previous = seen.get(key)
        if previous is not None and previous != normalized:
            raise PathCollisionError(
                f"Portable Pfadkollision zwischen {previous!r} und {normalized!r}"
            )
        seen[key] = normalized


def _reject_symlink_components(path: Path, root: Path) -> None:
    """Reject an existing symlink in the root-to-path component chain."""

    resolved_root = root.resolve(strict=True)
    if root.is_symlink():
        raise UnsafePathError(f"Erlaubter Wurzelpfad darf kein Symlink sein: {root}")
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise UnsafePathError(f"Pfad liegt außerhalb des erlaubten Wurzelpfads: {path}") from exc

    current = root.absolute()
    for part in relative.parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise UnsafePathError(f"Symlink-Komponente ist unzulässig: {current}")
    # Ensure the resolved candidate remains below the resolved root as a second,
    # race-resistant boundary check for existing components.
    resolved = path.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafePathError(f"Pfad verlässt den erlaubten Wurzelpfad: {path}") from exc


def ensure_within(path: Path, root: Path, *, reject_symlinks: bool = True) -> Path:
    """Resolve *path* and guarantee that it remains below *root*.

    Relative paths are interpreted below *root*. Existing symlink components are
    rejected by default, including a symlink used as the root itself.
    """

    root = root.absolute()
    if not root.exists():
        root.mkdir(parents=True, exist_ok=True)
    candidate = path if path.is_absolute() else root / path
    if reject_symlinks:
        _reject_symlink_components(candidate, root)
    resolved_root = root.resolve(strict=True)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise UnsafePathError(f"Pfad liegt außerhalb des erlaubten Wurzelpfads: {path}") from exc
    return resolved


def sha256_file(path: Path, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """Compute a streamed SHA-256 digest without loading the entire file."""

    if chunk_size <= 0:
        raise ValueError("chunk_size muss positiv sein")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(payload: bytes) -> str:
    """Return the SHA-256 digest of a byte string."""

    return hashlib.sha256(payload).hexdigest()


def stable_json_dumps(payload: Any, *, indent: int = 2) -> str:
    """Serialize JSON deterministically as UTF-8-friendly text plus newline."""

    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=indent,
            sort_keys=True,
            separators=(",", ": "),
        )
        + "\n"
    )


def fsync_directory(path: Path) -> None:
    """Synchronize directory metadata after an atomic replacement on POSIX."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    allowed_root: Path | None = None,
    mode: int = 0o644,
) -> None:
    """Atomically write bytes via an exclusive sibling ``.part`` file.

    The file payload and containing directory are synchronized before success is
    reported. On failure, the previous target remains intact and the temporary
    file is removed.
    """

    if allowed_root is not None:
        path = ensure_within(path, allowed_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise UnsafePathError(f"Zieldatei darf kein Symlink sein: {path}")
    part = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
    try:
        with part.open("xb") as handle:
            os.chmod(part, mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(part, path)
        fsync_directory(path.parent)
    finally:
        part.unlink(missing_ok=True)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    allowed_root: Path | None = None,
    mode: int = 0o644,
) -> None:
    """Atomically write UTF-8 text."""

    atomic_write_bytes(path, text.encode("utf-8"), allowed_root=allowed_root, mode=mode)


def mark_generated_root(path: Path) -> None:
    """Create and mark a directory as safe for generated-tree replacement."""

    path.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path / GENERATED_ROOT_SENTINEL,
        "generated by desinfect\n",
        allowed_root=path,
    )


def assert_generated_root(path: Path) -> None:
    """Require the project sentinel before deleting or replacing a tree."""

    sentinel = path / GENERATED_ROOT_SENTINEL
    if path.is_symlink() or not sentinel.is_file() or sentinel.is_symlink():
        raise UnsafePathError(
            f"Generiertes Ziel ist nicht durch {GENERATED_ROOT_SENTINEL} markiert: {path}"
        )


def safe_remove_generated_tree(path: Path, allowed_root: Path) -> None:
    """Remove a marked generated tree that is strictly below *allowed_root*."""

    resolved = ensure_within(path, allowed_root)
    if resolved == allowed_root.resolve(strict=True):
        raise UnsafePathError("Der erlaubte Wurzelpfad selbst darf nicht gelöscht werden")
    assert_generated_root(resolved)
    shutil.rmtree(resolved)
    fsync_directory(resolved.parent)


def source_date_epoch() -> int:
    """Return the reproducible build epoch or zero as a deterministic fallback."""

    raw = os.environ.get("SOURCE_DATE_EPOCH", "0").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("SOURCE_DATE_EPOCH muss eine ganze Zahl sein") from exc
    if value < 0:
        raise ValueError("SOURCE_DATE_EPOCH darf nicht negativ sein")
    return value


def generated_at_iso() -> str:
    """Return the reproducible build timestamp in canonical UTC form."""

    return datetime.fromtimestamp(source_date_epoch(), tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
