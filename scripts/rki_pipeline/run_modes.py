#!/usr/bin/env python3
"""Strict run modes, effect ledger, and repository side-effect snapshots."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import os
from pathlib import Path
import subprocess
from typing import Iterable

from scripts.rki_pipeline.io_utils import normalize_posix_path


class ModeViolation(RuntimeError):
    """A component performed or declared an effect forbidden by its run mode."""


class RunMode(StrEnum):
    """The only supported pipeline execution modes."""

    PLAN = "plan"
    MATERIALIZE = "materialize"
    APPLY = "apply"


class EffectKind(StrEnum):
    """Auditable kinds of local and remote side effects."""

    TEMP_FILE = "temp_file"
    REPOSITORY_FILE = "repository_file"
    GIT_INDEX = "git_index"
    GIT_COMMIT = "git_commit"
    LFS = "lfs"
    RELEASE = "release"
    OBJECT = "object"
    STATUS = "status"


_ALLOWED_EFFECTS: dict[RunMode, frozenset[EffectKind]] = {
    RunMode.PLAN: frozenset(),
    RunMode.MATERIALIZE: frozenset({EffectKind.TEMP_FILE}),
    RunMode.APPLY: frozenset(EffectKind),
}


@dataclass(frozen=True, slots=True)
class EffectEvent:
    """One explicitly declared effect with a stable target and optional digest."""

    kind: EffectKind
    target: str
    sha256: str | None = None
    size: int | None = None


@dataclass(slots=True)
class EffectLedger:
    """Collect effects only after validating the active mode contract."""

    mode: RunMode
    temp_root: Path | None = None
    events: list[EffectEvent] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, RunMode):
            raise ValueError("mode muss ein RunMode sein")
        if self.mode is RunMode.MATERIALIZE and self.temp_root is None:
            raise ValueError("materialize benötigt einen expliziten temp_root")
        if self.temp_root is not None:
            self.temp_root = Path(os.path.abspath(self.temp_root))

    def record(
        self,
        kind: EffectKind,
        target: str,
        *,
        sha256: str | None = None,
        size: int | None = None,
    ) -> EffectEvent:
        """Validate and append one intended or completed effect."""

        if not isinstance(kind, EffectKind):
            raise ValueError("kind muss ein EffectKind sein")
        if kind not in _ALLOWED_EFFECTS[self.mode]:
            raise ModeViolation(
                f"Effekt {kind.value} ist im Modus {self.mode.value} verboten"
            )
        if type(target) is not str or not target or "\x00" in target:
            raise ValueError("Effektziel muss eine nichtleere Zeichenkette sein")
        if sha256 is not None and (
            len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ValueError("sha256 muss ein kleingeschriebener SHA-256 sein")
        if size is not None and (type(size) is not int or size < 0):
            raise ValueError("size muss eine nichtnegative Ganzzahl sein")

        normalized_target = target
        if kind is EffectKind.TEMP_FILE:
            if self.temp_root is None:
                raise ModeViolation("TEMP_FILE benötigt einen temp_root")
            candidate = Path(os.path.abspath(target))
            try:
                candidate.relative_to(self.temp_root)
            except ValueError as exc:
                raise ModeViolation(
                    f"Temporärer Effekt liegt außerhalb temp_root: {target}"
                ) from exc
            normalized_target = candidate.as_posix()
        elif kind in {
            EffectKind.REPOSITORY_FILE,
            EffectKind.STATUS,
        }:
            normalized_target = normalize_posix_path(target)

        event = EffectEvent(kind, normalized_target, sha256, size)
        self.events.append(event)
        return event

    def targets(self, *kinds: EffectKind) -> frozenset[str]:
        """Return all targets recorded for the selected effect kinds."""

        selected = set(kinds)
        return frozenset(
            event.target
            for event in self.events
            if not selected or event.kind in selected
        )


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    """Read-only repository and protected-file state used by the guard."""

    head: str
    worktree: bytes
    index: bytes
    protected: tuple[tuple[str, str | None], ...]
    temp_files: tuple[tuple[str, str], ...]


def _git(root: Path, *arguments: str, check: bool = True) -> bytes:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=check,
            capture_output=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ModeViolation(
            f"Git-Snapshot fehlgeschlagen: {' '.join(arguments)}"
        ) from exc


def _sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    if not path.is_file() or path.is_symlink():
        raise ModeViolation(f"Geschützter Pfad ist keine reguläre Datei: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_snapshot(root: Path | None) -> tuple[tuple[str, str], ...]:
    if root is None or not root.exists():
        return ()
    root = Path(os.path.abspath(root))
    rows: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ModeViolation(f"Symlink im temp_root ist unzulässig: {path}")
        if not path.is_file():
            continue
        rows.append((path.relative_to(root).as_posix(), _sha256(path) or ""))
    return tuple(rows)


def capture_repository_snapshot(
    repository_root: Path,
    *,
    protected_paths: Iterable[str],
    temp_root: Path | None,
) -> RepositorySnapshot:
    """Capture Git, protected-file, and temp-tree state without mutations."""

    root = Path(os.path.abspath(repository_root))
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    worktree = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    index = _git(root, "diff", "--cached", "--binary")
    protected = tuple(
        sorted(
            (
                normalize_posix_path(path),
                _sha256(root / normalize_posix_path(path)),
            )
            for path in protected_paths
        )
    )
    return RepositorySnapshot(
        head=head,
        worktree=worktree,
        index=index,
        protected=protected,
        temp_files=_tree_snapshot(temp_root),
    )


def _status_paths(payload: bytes) -> frozenset[str]:
    """Extract stable destination paths from porcelain-v1 zero output."""

    tokens = payload.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(tokens) and tokens[index]:
        token = tokens[index].decode("utf-8", errors="strict")
        index += 1
        if len(token) < 4:
            raise ModeViolation(f"Ungültige Git-Statuszeile: {token!r}")
        status = token[:2]
        path = token[3:]
        if status[0] in {"R", "C"} or status[1] in {"R", "C"}:
            if index >= len(tokens) or not tokens[index]:
                raise ModeViolation("Unvollständiger Rename-/Copy-Status")
            path = tokens[index].decode("utf-8", errors="strict")
            index += 1
        paths.add(normalize_posix_path(path))
    return frozenset(paths)


class SideEffectGuard:
    """Detect repository/temp mutations that do not match the mode ledger."""

    def __init__(
        self,
        *,
        repository_root: Path,
        mode: RunMode,
        temp_root: Path | None,
        ledger: EffectLedger,
        protected_paths: tuple[str, ...] = ("status.json",),
    ) -> None:
        if ledger.mode is not mode:
            raise ValueError("Ledger- und Guard-Modus müssen übereinstimmen")
        self.repository_root = Path(os.path.abspath(repository_root))
        self.mode = mode
        self.temp_root = (
            None
            if temp_root is None
            else Path(os.path.abspath(temp_root))
        )
        self.ledger = ledger
        self.protected_paths = protected_paths
        self._before: RepositorySnapshot | None = None

    def __enter__(self) -> SideEffectGuard:
        self._before = capture_repository_snapshot(
            self.repository_root,
            protected_paths=self.protected_paths,
            temp_root=self.temp_root,
        )
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is not None:
            return False
        if self._before is None:
            raise RuntimeError("SideEffectGuard wurde nicht betreten")
        after = capture_repository_snapshot(
            self.repository_root,
            protected_paths=self.protected_paths,
            temp_root=self.temp_root,
        )
        self._validate(self._before, after)
        return False

    def _validate(
        self,
        before: RepositorySnapshot,
        after: RepositorySnapshot,
    ) -> None:
        repository_changed = (
            before.head != after.head
            or before.worktree != after.worktree
            or before.index != after.index
            or before.protected != after.protected
        )
        if self.mode in {RunMode.PLAN, RunMode.MATERIALIZE} and repository_changed:
            raise ModeViolation(
                f"Repositoryzustand wurde im Modus {self.mode.value} verändert"
            )

        if self.mode is RunMode.APPLY:
            if before.head != after.head and not self.ledger.targets(
                EffectKind.GIT_COMMIT
            ):
                raise ModeViolation("Git-Commit wurde nicht im Ledger registriert")
            if before.index != after.index and not self.ledger.targets(
                EffectKind.GIT_INDEX
            ):
                raise ModeViolation("Git-Index wurde nicht im Ledger registriert")
            before_paths = _status_paths(before.worktree)
            after_paths = _status_paths(after.worktree)
            changed_paths = before_paths.symmetric_difference(after_paths)
            registered = self.ledger.targets(
                EffectKind.REPOSITORY_FILE,
                EffectKind.STATUS,
            )
            missing = sorted(changed_paths - registered)
            if missing:
                raise ModeViolation(
                    "Repositoryänderung ist nicht im Ledger registriert: "
                    + ", ".join(missing)
                )

        before_temp = dict(before.temp_files)
        after_temp = dict(after.temp_files)
        changed_temp = {
            path
            for path in set(before_temp) | set(after_temp)
            if before_temp.get(path) != after_temp.get(path)
        }
        if changed_temp:
            if self.mode is RunMode.PLAN:
                raise ModeViolation("plan darf temp_root nicht verändern")
            if self.temp_root is None:
                raise ModeViolation("Temporäre Änderungen ohne temp_root")
            registered_temp = {
                Path(target).relative_to(self.temp_root).as_posix()
                for target in self.ledger.targets(EffectKind.TEMP_FILE)
            }
            missing_temp = sorted(changed_temp - registered_temp)
            if missing_temp:
                raise ModeViolation(
                    "Temporäre Änderung ist nicht im Ledger registriert: "
                    + ", ".join(missing_temp)
                )
