#!/usr/bin/env python3
"""Strict run modes, effect ledger, and repository side-effect snapshots."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Iterable

from scripts.rki_pipeline.io_utils import UnsafePathError, ensure_within, normalize_posix_path

_SHA40 = re.compile(r"^[0-9a-f]{40}$")


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
            self.temp_root = Path(self.temp_root).resolve()

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
            try:
                candidate = ensure_within(Path(target), self.temp_root)
            except (OSError, UnsafePathError) as exc:
                raise ModeViolation(
                    f"Temporärer Effekt liegt außerhalb temp_root: {target}"
                ) from exc
            normalized_target = candidate.as_posix()
        elif kind in {
            EffectKind.REPOSITORY_FILE,
            EffectKind.STATUS,
            EffectKind.GIT_INDEX,
        }:
            normalized_target = normalize_posix_path(target)
        elif kind is EffectKind.GIT_COMMIT:
            if _SHA40.fullmatch(target) is None:
                raise ValueError("GIT_COMMIT-Ziel muss der resultierende 40-stellige HEAD-SHA sein")

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
    dirty_files: tuple[tuple[str, str | None], ...]
    staged_files: tuple[tuple[str, str | None], ...]
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


def _fingerprint(path: Path) -> str | None:
    """Hash regular-file bytes together with permission bits."""

    digest = _sha256(path)
    if digest is None:
        return None
    mode = stat.S_IMODE(path.stat(follow_symlinks=False).st_mode)
    return f"{mode:o}:{digest}"


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
        rows.append((path.relative_to(root).as_posix(), _fingerprint(path) or ""))
    return tuple(rows)


def _status_paths(payload: bytes) -> frozenset[str]:
    """Extract source and destination paths from porcelain-v1 zero output."""

    tokens = payload.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(tokens) and tokens[index]:
        token = tokens[index].decode("utf-8", errors="strict")
        index += 1
        if len(token) < 4:
            raise ModeViolation(f"Ungültige Git-Statuszeile: {token!r}")
        status_code = token[:2]
        path = token[3:]
        if status_code[0] in {"R", "C"} or status_code[1] in {"R", "C"}:
            paths.add(normalize_posix_path(path))
            if index >= len(tokens) or not tokens[index]:
                raise ModeViolation("Unvollständiger Rename-/Copy-Status")
            path = tokens[index].decode("utf-8", errors="strict")
            index += 1
        paths.add(normalize_posix_path(path))
    return frozenset(paths)


def _nul_paths(payload: bytes) -> frozenset[str]:
    return frozenset(
        normalize_posix_path(token.decode("utf-8", errors="strict"))
        for token in payload.split(b"\0")
        if token
    )


def _dirty_snapshot(root: Path, worktree: bytes) -> tuple[tuple[str, str | None], ...]:
    """Fingerprint every currently dirty/untracked path, including prior dirt."""

    return tuple(
        sorted(
            (path, _fingerprint(root / path))
            for path in _status_paths(worktree)
        )
    )


def _staged_snapshot(root: Path) -> tuple[tuple[str, str | None], ...]:
    """Fingerprint each path whose index image differs from HEAD."""

    paths = _nul_paths(
        _git(
            root,
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--diff-filter=ACMRDTUXB",
        )
    )
    rows: list[tuple[str, str | None]] = []
    for path in paths:
        entry = _git(root, "ls-files", "--stage", "--", path, check=False)
        fingerprint = None if not entry else hashlib.sha256(entry).hexdigest()
        rows.append((path, fingerprint))
    return tuple(sorted(rows))


def capture_repository_snapshot(
    repository_root: Path,
    *,
    protected_paths: Iterable[str],
    temp_root: Path | None,
) -> RepositorySnapshot:
    """Capture Git, dirty-file, protected-file, and temp-tree state."""

    root = Path(os.path.abspath(repository_root))
    head = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    worktree = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    index = _git(root, "diff", "--cached", "--binary")
    protected = tuple(
        sorted(
            (
                normalize_posix_path(path),
                _fingerprint(root / normalize_posix_path(path)),
            )
            for path in protected_paths
        )
    )
    return RepositorySnapshot(
        head=head,
        worktree=worktree,
        index=index,
        dirty_files=_dirty_snapshot(root, worktree),
        staged_files=_staged_snapshot(root),
        protected=protected,
        temp_files=_tree_snapshot(temp_root),
    )


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
        self.repository_root = Path(repository_root).resolve()
        self.mode = mode
        self.temp_root = (
            None
            if temp_root is None
            else Path(temp_root).resolve()
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
        if self._before is None:
            raise RuntimeError("SideEffectGuard wurde nicht betreten")
        try:
            after = capture_repository_snapshot(
                self.repository_root,
                protected_paths=self.protected_paths,
                temp_root=self.temp_root,
            )
            self._validate(self._before, after)
        except BaseException as validation_error:
            if exc is None:
                raise
            group_type = (
                ExceptionGroup
                if isinstance(exc, Exception) and isinstance(validation_error, Exception)
                else BaseExceptionGroup
            )
            raise group_type(
                "Operation und SideEffectGuard-Validierung sind fehlgeschlagen",
                [exc, validation_error],
            )
        return False

    @staticmethod
    def _changed_rows(
        before_rows: tuple[tuple[str, str | None], ...],
        after_rows: tuple[tuple[str, str | None], ...],
    ) -> frozenset[str]:
        before = dict(before_rows)
        after = dict(after_rows)
        return frozenset(
            path
            for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        )

    @staticmethod
    def _require_exact(
        *,
        label: str,
        actual: frozenset[str],
        registered: frozenset[str],
    ) -> None:
        missing = sorted(actual - registered)
        extra = sorted(registered - actual)
        if missing or extra:
            details: list[str] = []
            if missing:
                details.append("nicht registriert: " + ", ".join(missing))
            if extra:
                details.append("ohne tatsächlichen Effekt: " + ", ".join(extra))
            raise ModeViolation(f"{label} stimmt nicht exakt mit dem Ledger überein ({'; '.join(details)})")

    def _committed_paths(self, before: RepositorySnapshot, after: RepositorySnapshot) -> frozenset[str]:
        if before.head == after.head:
            return frozenset()
        return _nul_paths(
            _git(
                self.repository_root,
                "diff",
                "--name-only",
                "-z",
                before.head,
                after.head,
            )
        )

    def _validate(
        self,
        before: RepositorySnapshot,
        after: RepositorySnapshot,
    ) -> None:
        dirty_changed = self._changed_rows(before.dirty_files, after.dirty_files)
        staged_changed = self._changed_rows(before.staged_files, after.staged_files)
        protected_changed = self._changed_rows(before.protected, after.protected)
        repository_changed = (
            before.head != after.head
            or before.worktree != after.worktree
            or before.index != after.index
            or bool(dirty_changed)
            or bool(staged_changed)
            or bool(protected_changed)
        )
        if self.mode in {RunMode.PLAN, RunMode.MATERIALIZE} and repository_changed:
            raise ModeViolation(
                f"Repositoryzustand wurde im Modus {self.mode.value} verändert"
            )

        if self.mode is RunMode.APPLY:
            actual_commits = (
                frozenset({after.head})
                if before.head != after.head
                else frozenset()
            )
            self._require_exact(
                label="Git-Commit/HEAD",
                actual=actual_commits,
                registered=self.ledger.targets(EffectKind.GIT_COMMIT),
            )
            if before.index != after.index and not staged_changed:
                raise ModeViolation("Git-Index änderte sich ohne zuordenbare Pfade")
            self._require_exact(
                label="Git-Index",
                actual=staged_changed,
                registered=self.ledger.targets(EffectKind.GIT_INDEX),
            )

            status_before = _status_paths(before.worktree)
            status_after = _status_paths(after.worktree)
            changed_paths = status_before.symmetric_difference(status_after).union(dirty_changed)
            registered_files = self.ledger.targets(
                EffectKind.REPOSITORY_FILE,
                EffectKind.STATUS,
            )
            missing_files = sorted(changed_paths - registered_files)
            if missing_files:
                raise ModeViolation(
                    "Repositoryänderung ist nicht im Ledger registriert: "
                    + ", ".join(missing_files)
                )

            missing_protected = sorted(
                protected_changed - self.ledger.targets(EffectKind.STATUS)
            )
            if missing_protected:
                raise ModeViolation(
                    "Geschützter Statuspfad ist nicht als STATUS im Ledger registriert: "
                    + ", ".join(missing_protected)
                )

            committed_paths = self._committed_paths(before, after)
            missing_committed = sorted(committed_paths - registered_files)
            if missing_committed:
                raise ModeViolation(
                    "Commit enthält nicht registrierte Repositorypfade: "
                    + ", ".join(missing_committed)
                )

        changed_temp = self._changed_rows(before.temp_files, after.temp_files)
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
