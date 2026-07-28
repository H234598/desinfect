#!/usr/bin/env python3
"""Deny-first policy for every automatic repository write operation."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import fnmatch
import os
from pathlib import Path
import stat
import subprocess
import tomllib
from typing import Iterable

from scripts.rki_pipeline.io_utils import (
    detect_path_collisions,
    normalize_posix_path,
)

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = ROOT / "config" / "automatic-write-paths.toml"
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


class WritePolicyError(RuntimeError):
    """An automatic write is denied, unlisted, or structurally unsafe."""


@dataclass(frozen=True, slots=True)
class WriteOperation:
    """One planned or staged path with its pre- and post-image Git modes."""

    path: str
    change: str = "modify"
    git_mode: str | None = None
    previous_git_mode: str | None = None


@dataclass(frozen=True, slots=True)
class WritePolicy:
    """Validated immutable allow/deny and structural-write constraints."""

    allow: tuple[str, ...]
    deny: tuple[str, ...]
    max_operations: int
    reject_symlinks: bool
    reject_gitlinks: bool


def _validate_pattern(pattern: str) -> str:
    if (
        not isinstance(pattern, str)
        or not pattern
        or pattern.startswith("/")
        or "\\" in pattern
    ):
        raise WritePolicyError(f"Ungültiges Policy-Muster: {pattern!r}")
    if "\x00" in pattern or any(part == ".." for part in pattern.split("/")):
        raise WritePolicyError(f"Unsicheres Policy-Muster: {pattern!r}")
    return pattern


def load_policy(path: Path = DEFAULT_POLICY_PATH) -> WritePolicy:
    """Load and validate the immutable deny-first policy."""

    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise WritePolicyError("Unbekannte Schreibpolicy-Version")
    allow = tuple(_validate_pattern(value) for value in data.get("allow", []))
    deny = tuple(_validate_pattern(value) for value in data.get("deny", []))
    if (
        not allow
        or not deny
        or len(set(allow)) != len(allow)
        or len(set(deny)) != len(deny)
    ):
        raise WritePolicyError(
            "Allow-/Deny-Muster müssen eindeutig und nicht leer sein"
        )
    max_operations = data.get("max_operations")
    if type(max_operations) is not int or not 1 <= max_operations <= 100_000:
        raise WritePolicyError(
            "max_operations liegt außerhalb des sicheren Bereichs"
        )
    return WritePolicy(
        allow=allow,
        deny=deny,
        max_operations=max_operations,
        reject_symlinks=data.get("reject_symlinks") is True,
        reject_gitlinks=data.get("reject_gitlinks") is True,
    )


def _matches(path: str, pattern: str) -> bool:
    # fnmatchcase intentionally treats '*' as matching '/', making a terminal
    # '/**' an explicit recursive subtree rule independent of host platform.
    return fnmatch.fnmatchcase(path, pattern)


def classify_path(path: str, policy: WritePolicy) -> str:
    """Return denied, allowed, or unlisted; deny always wins."""

    normalized = normalize_posix_path(path)
    if any(_matches(normalized, pattern) for pattern in policy.deny):
        return "denied"
    if any(_matches(normalized, pattern) for pattern in policy.allow):
        return "allowed"
    return "unlisted"


def _open_directory_at(name: str | Path, *, dir_fd: int | None = None) -> int:
    """Open one directory without following its final symlink."""

    if _NOFOLLOW == 0:
        raise WritePolicyError(
            "Die Plattform unterstützt O_NOFOLLOW nicht; "
            "automatische Writes bleiben fail-closed"
        )
    try:
        descriptor = os.open(
            name,
            _DIRECTORY_FLAGS | _NOFOLLOW,
            dir_fd=dir_fd,
        )
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise WritePolicyError(
                f"Symlink- oder Nicht-Verzeichnis-Komponente: {name}"
            ) from exc
        raise
    metadata = os.fstat(descriptor)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(descriptor)
        raise WritePolicyError(f"Pfadkomponente ist kein Verzeichnis: {name}")
    return descriptor


def _assert_no_filesystem_symlinks(
    repository_root: Path,
    relative_path: str,
) -> None:
    """Reject every existing symlink component through held no-follow FDs.

    Non-existing suffixes are allowed because Git does not track directories;
    their eventual creation/replacement must use the P01 descriptor-relative
    atomic IO primitives, which also apply ``O_NOFOLLOW``.
    """

    parts = tuple(normalize_posix_path(relative_path).split("/"))
    root = Path(os.path.abspath(repository_root))
    current_fd = _open_directory_at(root)
    try:
        for index, part in enumerate(parts):
            try:
                metadata = os.stat(
                    part,
                    dir_fd=current_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if stat.S_ISLNK(metadata.st_mode):
                raise WritePolicyError(
                    f"Filesystem-Symlink ist unzulässig: {relative_path}"
                )
            if index < len(parts) - 1:
                if not stat.S_ISDIR(metadata.st_mode):
                    raise WritePolicyError(
                        "Pfadkomponente ist kein Verzeichnis: "
                        f"{relative_path}"
                    )
                next_fd = _open_directory_at(part, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
    finally:
        os.close(current_fd)


def _reject_structural_modes(
    operation: WriteOperation,
    normalized: str,
    policy: WritePolicy,
) -> None:
    """Reject symlink/gitlink modes in either the pre- or post-image."""

    for label, mode in (
        ("Vorabbild", operation.previous_git_mode),
        ("Nachabbild", operation.git_mode),
    ):
        if mode == "120000" and policy.reject_symlinks:
            raise WritePolicyError(
                f"Symlinkänderung im {label} ist unzulässig: {normalized}"
            )
        if mode == "160000" and policy.reject_gitlinks:
            raise WritePolicyError(
                f"Gitlink/Submoduländerung im {label} ist unzulässig: "
                f"{normalized}"
            )


def validate_operations(
    operations: Iterable[WriteOperation],
    policy: WritePolicy | None = None,
    *,
    repository_root: Path = ROOT,
) -> tuple[WriteOperation, ...]:
    """Validate planned writes before staging or git index mutation."""

    selected = policy or load_policy()
    materialized = tuple(operations)
    if len(materialized) > selected.max_operations:
        raise WritePolicyError("Zu viele automatische Dateioperationen")
    normalized_paths = [
        normalize_posix_path(operation.path)
        for operation in materialized
    ]
    detect_path_collisions(normalized_paths)
    if len(normalized_paths) != len(set(normalized_paths)):
        raise WritePolicyError("Doppelte automatische Dateioperation")

    for operation, normalized in zip(
        materialized,
        normalized_paths,
        strict=True,
    ):
        classification = classify_path(normalized, selected)
        if classification != "allowed":
            raise WritePolicyError(
                f"Automatischer Pfad ist {classification}: {normalized}"
            )
        _reject_structural_modes(operation, normalized, selected)
        if selected.reject_symlinks:
            _assert_no_filesystem_symlinks(repository_root, normalized)
    return materialized


def _run_git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            check=True,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WritePolicyError(
            f"Git-Prüfung fehlgeschlagen: {' '.join(args)}"
        ) from exc


def _mode(value: str) -> str | None:
    return None if value == "000000" else value


def staged_operations(root: Path = ROOT) -> tuple[WriteOperation, ...]:
    """Read staged paths plus pre/post modes from the cached raw diff."""

    raw = _run_git(
        root,
        "diff",
        "--cached",
        "--raw",
        "-z",
        "--no-abbrev",
        "--diff-filter=ACMRDTUXB",
    )
    tokens = raw.split(b"\0")
    operations: list[WriteOperation] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        header = tokens[index].decode("ascii", errors="strict")
        index += 1
        fields = header.split()
        if len(fields) != 5 or not fields[0].startswith(":"):
            raise WritePolicyError(f"Ungültiger Raw-Diff-Header: {header!r}")
        previous_mode = _mode(fields[0][1:])
        current_mode = _mode(fields[1])
        status = fields[4]
        if not status:
            raise WritePolicyError("Raw-Diff enthält leeren Status")
        if index >= len(tokens) or not tokens[index]:
            raise WritePolicyError("Raw-Diff enthält keinen Pfad")
        source = tokens[index].decode("utf-8", errors="strict")
        index += 1

        if status.startswith(("R", "C")):
            if index >= len(tokens) or not tokens[index]:
                raise WritePolicyError("Unvollständiger Rename-/Copy-Diff")
            destination = tokens[index].decode("utf-8", errors="strict")
            index += 1
            operations.append(
                WriteOperation(
                    source,
                    change="source",
                    git_mode=(previous_mode if status.startswith("C") else None),
                    previous_git_mode=previous_mode,
                )
            )
            operations.append(
                WriteOperation(
                    destination,
                    change="destination",
                    git_mode=current_mode,
                    previous_git_mode=None,
                )
            )
        else:
            operations.append(
                WriteOperation(
                    source,
                    change=status,
                    git_mode=current_mode,
                    previous_git_mode=previous_mode,
                )
            )
    return tuple(operations)


def validate_index(
    root: Path = ROOT,
    policy: WritePolicy | None = None,
) -> tuple[WriteOperation, ...]:
    """Validate the current staged index against the deny-first policy."""

    return validate_operations(
        staged_operations(root),
        policy,
        repository_root=root,
    )
