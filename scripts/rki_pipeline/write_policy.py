
#!/usr/bin/env python3
"""Deny-first policy for every automatic repository write operation."""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import os
from pathlib import Path
import subprocess
import tomllib
from typing import Iterable

from scripts.rki_pipeline.io_utils import detect_path_collisions, normalize_posix_path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_POLICY_PATH = ROOT / "config" / "automatic-write-paths.toml"


class WritePolicyError(RuntimeError):
    """An automatic write is denied, unlisted, or structurally unsafe."""


@dataclass(frozen=True, slots=True)
class WriteOperation:
    path: str
    change: str = "modify"
    git_mode: str | None = None


@dataclass(frozen=True, slots=True)
class WritePolicy:
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    max_operations: int
    reject_symlinks: bool
    reject_gitlinks: bool


def _validate_pattern(pattern: str) -> str:
    if not isinstance(pattern, str) or not pattern or pattern.startswith("/") or "\\" in pattern:
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
    if not allow or not deny or len(set(allow)) != len(allow) or len(set(deny)) != len(deny):
        raise WritePolicyError("Allow-/Deny-Muster müssen eindeutig und nicht leer sein")
    max_operations = data.get("max_operations")
    if type(max_operations) is not int or not 1 <= max_operations <= 100_000:
        raise WritePolicyError("max_operations liegt außerhalb des sicheren Bereichs")
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


def validate_operations(
    operations: Iterable[WriteOperation], policy: WritePolicy | None = None
) -> tuple[WriteOperation, ...]:
    """Validate planned writes before staging or git index mutation."""

    selected = policy or load_policy()
    materialized = tuple(operations)
    if len(materialized) > selected.max_operations:
        raise WritePolicyError("Zu viele automatische Dateioperationen")
    normalized_paths = [normalize_posix_path(operation.path) for operation in materialized]
    detect_path_collisions(normalized_paths)
    if len(normalized_paths) != len(set(normalized_paths)):
        raise WritePolicyError("Doppelte automatische Dateioperation")

    for operation, normalized in zip(materialized, normalized_paths, strict=True):
        classification = classify_path(normalized, selected)
        if classification != "allowed":
            raise WritePolicyError(f"Automatischer Pfad ist {classification}: {normalized}")
        mode = operation.git_mode
        if selected.reject_symlinks and mode == "120000":
            raise WritePolicyError(f"Symlinkänderung ist unzulässig: {normalized}")
        if selected.reject_gitlinks and mode == "160000":
            raise WritePolicyError(f"Gitlink/Submoduländerung ist unzulässig: {normalized}")
    return materialized


def _run_git(root: Path, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise WritePolicyError(f"Git-Prüfung fehlgeschlagen: {' '.join(args)}") from exc


def staged_operations(root: Path = ROOT) -> tuple[WriteOperation, ...]:
    """Read staged paths and modes without accepting rename-source bypasses."""

    raw = _run_git(root, "diff", "--cached", "--name-status", "-z", "--diff-filter=ACMRDTUXB")
    tokens = raw.decode("utf-8", errors="strict").split("\0")
    operations: list[WriteOperation] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        status = tokens[index]
        index += 1
        if status.startswith(("R", "C")):
            if index + 1 >= len(tokens):
                raise WritePolicyError("Unvollständiger Rename-/Copy-Diff")
            source, destination = tokens[index], tokens[index + 1]
            index += 2
            operations.append(WriteOperation(source, change="source"))
            operations.append(WriteOperation(destination, change="destination"))
        else:
            if index >= len(tokens):
                raise WritePolicyError("Unvollständiger staged diff")
            operations.append(WriteOperation(tokens[index], change=status))
            index += 1

    if not operations:
        return ()
    paths = [operation.path for operation in operations]
    modes_raw = _run_git(root, "ls-files", "-s", "-z", "--", *paths)
    modes: dict[str, str] = {}
    for row in modes_raw.decode("utf-8", errors="strict").split("\0"):
        if not row:
            continue
        prefix, path = row.split("\t", 1)
        mode = prefix.split(" ", 1)[0]
        modes[path] = mode
    return tuple(
        WriteOperation(operation.path, operation.change, modes.get(operation.path))
        for operation in operations
    )


def validate_index(root: Path = ROOT, policy: WritePolicy | None = None) -> tuple[WriteOperation, ...]:
    """Validate the current staged index against the deny-first policy."""

    return validate_operations(staged_operations(root), policy)
