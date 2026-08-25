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
held directory-descriptor boundaries, no-follow traversal, parent-directory
fsync, collision detection, and a project-specific generated-root sentinel.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any, Iterable, Iterator
import unicodedata
import uuid

GENERATED_ROOT_SENTINEL = ".desinfect-generated-root"
DEFAULT_CHUNK_SIZE = 1024 * 1024
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


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


def relative_path_beneath(path: Path, root: Path) -> PurePosixPath:
    """Return a normalized syntactic path below *root* without following links."""

    root_absolute = Path(os.path.abspath(root))
    candidate = path if path.is_absolute() else root_absolute / path
    candidate_absolute = Path(os.path.abspath(candidate))
    try:
        relative = candidate_absolute.relative_to(root_absolute)
    except ValueError as exc:
        raise UnsafePathError(f"Pfad liegt außerhalb des erlaubten Wurzelpfads: {path}") from exc
    if not relative.parts:
        raise UnsafePathError("Der erlaubte Wurzelpfad selbst ist kein gültiges Ziel")
    return PurePosixPath(normalize_posix_path(relative.as_posix()))


def _open_directory(name: str | Path, *, dir_fd: int | None = None) -> int:
    """Open one directory component without following its final symlink."""

    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS | _NOFOLLOW, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise UnsafePathError(f"Symlink- oder Nicht-Verzeichnis-Komponente: {name}") from exc
        raise
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise UnsafePathError(f"Pfadkomponente ist kein Verzeichnis: {name}")
    return descriptor


def _duplicate_fd_root(root: Path) -> int | None:
    """Duplicate an exact process FD path after checking it names a directory."""

    raw = os.fspath(root)
    if isinstance(raw, bytes):
        raw = os.fsdecode(raw)
    absolute = Path(os.path.abspath(raw))
    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            relative = absolute.relative_to(base)
        except ValueError:
            continue
        if raw != str(absolute) or len(relative.parts) != 1 or not relative.parts[0].isdigit():
            raise UnsafePathError(f"Kein exakter FD-Wurzelpfad: {root}")
        try:
            descriptor_number = int(relative.parts[0])
            if str(descriptor_number) != relative.parts[0]:
                raise UnsafePathError(f"Kein exakter FD-Wurzelpfad: {root}")
            descriptor = os.dup(descriptor_number)
        except (ValueError, OverflowError, OSError) as exc:
            raise UnsafePathError(f"FD-Wurzelpfad ist nicht verfügbar: {root}") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise UnsafePathError(f"FD-Wurzelpfad ist kein Verzeichnis: {root}")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    return None


@contextmanager
def open_root_directory(root: Path, *, create: bool = False) -> Iterator[int]:
    """Hold a no-follow descriptor for an allowed root directory."""

    descriptor = _duplicate_fd_root(root)
    if descriptor is None:
        root = Path(os.path.abspath(root))
        if create:
            root.mkdir(parents=True, exist_ok=True)
        descriptor = _open_directory(root)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def open_directory_beneath(
    root_fd: int,
    parts: Iterable[str],
    *,
    create: bool = False,
    mode: int = 0o755,
) -> int:
    """Open a directory chain relative to a held root without following links."""

    current = os.dup(root_fd)
    try:
        for raw_part in parts:
            part = normalize_posix_path(raw_part)
            if "/" in part:
                raise UnsafePathError(f"Nur einzelne Pfadkomponenten sind erlaubt: {raw_part}")
            if create:
                try:
                    os.mkdir(part, mode=mode, dir_fd=current)
                except FileExistsError:
                    pass
            next_descriptor = _open_directory(part, dir_fd=current)
            os.close(current)
            current = next_descriptor
        return current
    except BaseException:
        os.close(current)
        raise


def fd_directory_path(descriptor: int) -> Path:
    """Return an FD-backed directory path or fail closed on unsupported hosts."""

    for base in (Path("/proc/self/fd"), Path("/dev/fd")):
        candidate = base / str(descriptor)
        if base.is_dir() and candidate.exists():
            return candidate
    raise UnsafePathError(
        "Die Plattform stellt keinen FD-basierten Pfad für sicheres Staging bereit"
    )


def entry_exists(parent_fd: int, name: str) -> bool:
    """Check for an entry relative to a held directory without following links."""

    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def fsync_directory_fd(descriptor: int) -> None:
    """Synchronize metadata through an already-open directory descriptor."""

    os.fsync(descriptor)


def fsync_directory(path: Path) -> None:
    """Synchronize directory metadata without following the final component."""

    descriptor = _open_directory(path)
    try:
        fsync_directory_fd(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_at(parent_fd: int, name: str, payload: bytes, mode: int) -> None:
    """Atomically replace one regular entry relative to a held directory."""

    if "/" in name or name in {"", ".", ".."}:
        raise UnsafePathError(f"Ungültiger Dateiname für descriptor-relative Ablage: {name}")
    try:
        existing = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing is not None and not stat.S_ISREG(existing.st_mode):
        raise UnsafePathError(f"Ziel ist keine reguläre Datei: {name}")

    part = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.part"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | _NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(part, flags, mode, dir_fd=parent_fd)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            part,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        fsync_directory_fd(parent_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(part, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def atomic_write_bytes(
    path: Path,
    payload: bytes,
    *,
    allowed_root: Path | None = None,
    mode: int = 0o644,
) -> None:
    """Atomically write bytes beneath a held no-follow root descriptor.

    Temporary-file creation and final replacement are relative to the same held
    parent directory descriptor, so an ancestor path renamed or replaced with a
    symlink after validation cannot redirect the write outside the allowed root.
    """

    root = allowed_root if allowed_root is not None else path.parent
    relative = relative_path_beneath(path, root)
    with open_root_directory(root, create=True) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1], create=True)
        try:
            _atomic_write_at(parent_fd, relative.name, payload, mode)
        finally:
            os.close(parent_fd)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    allowed_root: Path | None = None,
    mode: int = 0o644,
) -> None:
    """Atomically write UTF-8 text."""

    atomic_write_bytes(path, text.encode("utf-8"), allowed_root=allowed_root, mode=mode)


def mark_generated_root_fd(directory_fd: int) -> None:
    """Create the generated-root sentinel inside a held directory descriptor."""

    _atomic_write_at(
        directory_fd,
        GENERATED_ROOT_SENTINEL,
        b"generated by desinfect\n",
        0o644,
    )


def assert_generated_root_fd(directory_fd: int) -> None:
    """Require a regular, non-symlink generated-root sentinel."""

    try:
        metadata = os.stat(
            GENERATED_ROOT_SENTINEL,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError as exc:
        raise UnsafePathError(
            f"Generiertes Ziel ist nicht durch {GENERATED_ROOT_SENTINEL} markiert"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise UnsafePathError(f"Sentinel {GENERATED_ROOT_SENTINEL} ist keine reguläre Datei")


def mark_generated_root(path: Path, *, allowed_root: Path | None = None) -> None:
    """Create and securely mark a directory as generated output."""

    root = allowed_root if allowed_root is not None else path.parent
    relative = relative_path_beneath(path, root)
    with open_root_directory(root, create=True) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1], create=True)
        try:
            try:
                os.mkdir(relative.name, mode=0o755, dir_fd=parent_fd)
            except FileExistsError:
                pass
            directory_fd = _open_directory(relative.name, dir_fd=parent_fd)
            try:
                mark_generated_root_fd(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.close(parent_fd)


def assert_generated_root(path: Path, *, allowed_root: Path | None = None) -> None:
    """Require the project sentinel without following path symlinks."""

    root = allowed_root if allowed_root is not None else path.parent
    relative = relative_path_beneath(path, root)
    with open_root_directory(root) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1])
        try:
            directory_fd = _open_directory(relative.name, dir_fd=parent_fd)
            try:
                assert_generated_root_fd(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            os.close(parent_fd)


def validate_tree_no_symlinks_fd(directory_fd: int) -> None:
    """Recursively reject every symlink below a held directory descriptor."""

    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafePathError(f"Symlink in generiertem Baum ist unzulässig: {name}")
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(name, dir_fd=directory_fd)
            try:
                validate_tree_no_symlinks_fd(child_fd)
            finally:
                os.close(child_fd)


def _remove_tree_contents_fd(directory_fd: int) -> None:
    """Recursively remove directory contents without following symlinks."""

    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise UnsafePathError(f"Symlink in generiertem Baum ist unzulässig: {name}")
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = _open_directory(name, dir_fd=directory_fd)
            try:
                _remove_tree_contents_fd(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def clear_generated_tree_fd(directory_fd: int) -> None:
    """Clear one held generated tree without unlinking its mutable directory name."""

    assert_generated_root_fd(directory_fd)
    validate_tree_no_symlinks_fd(directory_fd)
    _remove_tree_contents_fd(directory_fd)
    fsync_directory_fd(directory_fd)


def remove_tree_at(parent_fd: int, name: str, *, require_sentinel: bool = True) -> None:
    """Remove one descriptor-relative directory tree with optional sentinel gate."""

    directory_fd = _open_directory(name, dir_fd=parent_fd)
    try:
        if require_sentinel:
            assert_generated_root_fd(directory_fd)
        validate_tree_no_symlinks_fd(directory_fd)
        _remove_tree_contents_fd(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)
    fsync_directory_fd(parent_fd)


def safe_remove_generated_tree(path: Path, allowed_root: Path) -> None:
    """Remove a marked generated tree strictly beneath a held allowed root."""

    relative = relative_path_beneath(path, allowed_root)
    with open_root_directory(allowed_root) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1])
        try:
            remove_tree_at(parent_fd, relative.name, require_sentinel=True)
        finally:
            os.close(parent_fd)


def ensure_within(
    path: Path,
    root: Path,
    *,
    reject_symlinks: bool = True,
    allow_missing_parents: bool = False,
) -> Path:
    """Return a path below *root* after descriptor-based component validation.

    This helper is suitable for validation and display. Security-sensitive writes
    must continue to operate through the held descriptors used by this module.
    """

    relative = relative_path_beneath(path, root)
    with open_root_directory(root, create=True) as root_fd:
        if reject_symlinks:
            try:
                parent_fd = open_directory_beneath(root_fd, relative.parts[:-1])
            except FileNotFoundError:
                if allow_missing_parents:
                    return Path(os.path.abspath(root)) / Path(relative.as_posix())
                raise
            try:
                try:
                    metadata = os.stat(
                        relative.name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    metadata = None
                if metadata is not None and stat.S_ISLNK(metadata.st_mode):
                    raise UnsafePathError(f"Symlink-Komponente ist unzulässig: {relative.name}")
            finally:
                os.close(parent_fd)
    return Path(os.path.abspath(root)) / Path(relative.as_posix())


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


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Eingabedatei enthält einen doppelten Schlüssel")
        result[key] = value
    return result


def _reject_nonfinite_json(_value: str) -> None:
    raise ValueError("Eingabedatei enthält einen nichtendlichen Wert")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _reject_nonfinite_json(value)
    return parsed


def read_bounded_utf8_text_beneath(
    root: Path,
    relative: PurePosixPath,
    *,
    max_bytes: int,
) -> str:
    """Read one regular UTF-8 file through held no-follow descriptors."""

    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError("Dateigrößenlimit muss positiv sein")
    if not isinstance(relative, PurePosixPath):
        raise UnsafePathError("Dateipfad muss ein relativer POSIX-Pfad sein")
    raw_relative = relative.as_posix()
    if normalize_posix_path(raw_relative) != raw_relative:
        raise UnsafePathError(f"Nichtkanonischer relativer Dateipfad: {raw_relative}")
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if type(no_follow) is not int or no_follow == 0:
        raise OSError(errno.ENOTSUP, "Plattform unterstützt O_NOFOLLOW nicht")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | no_follow | getattr(os, "O_NONBLOCK", 0)
    with open_root_directory(root) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1])
        try:
            try:
                descriptor = os.open(relative.name, flags, dir_fd=parent_fd)
            except OSError as exc:
                if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                    raise ValueError("Eingabepfad ist keine lesbare reguläre Datei") from exc
                raise
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Eingabepfad ist keine reguläre Datei")
                if metadata.st_size > max_bytes:
                    raise ValueError("Eingabedatei überschreitet Größenlimit")
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining:
                    chunk = os.read(descriptor, min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                if len(raw) > max_bytes:
                    raise ValueError("Eingabedatei überschreitet Größenlimit")
            finally:
                os.close(descriptor)
        finally:
            os.close(parent_fd)
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Eingabedatei ist kein gültiges UTF-8") from exc


def parse_strict_json_object(text: str) -> dict[str, Any]:
    """Parse one finite JSON object while rejecting duplicate keys."""

    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
            parse_float=_finite_json_float,
        )
    except json.JSONDecodeError as exc:
        raise ValueError("Eingabedatei enthält kein gültiges JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON-Wurzel muss ein Objekt sein")
    return value


def read_bounded_json_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    """Read one no-follow, bounded UTF-8 JSON object."""

    try:
        text = read_bounded_utf8_text_beneath(
            path.parent,
            PurePosixPath(path.name),
            max_bytes=max_bytes,
        )
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno == errno.ENOTSUP:
            raise
        raise OSError("Eingabepfad ist keine lesbare reguläre Datei") from exc
    return parse_strict_json_object(text)


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

    return (
        datetime.fromtimestamp(source_date_epoch(), tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
