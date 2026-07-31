#!/usr/bin/env python3
"""Exact stage, policy, commit, base-drift, and non-force push writer."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import subprocess
from typing import Sequence

from scripts.rki_pipeline.commit_plan import CommitPlan, TreeEntry, compute_tree_sha256
from scripts.rki_pipeline.io_utils import normalize_posix_path
from scripts.rki_pipeline.write_policy import validate_index


class GitWriterError(RuntimeError):
    """The repository cannot be written exactly as described by CommitPlan."""


@dataclass(frozen=True, slots=True)
class GitWriteResult:
    """One no-op or successful single-commit write result."""

    changed: bool
    commit_sha: str | None
    pushed: bool
    paths: tuple[str, ...]
    status_diagnostic: str
    diff_diagnostic: str


class GitRunner:
    """Argument-array Git subprocess port with redacted failures."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve(strict=True)
        git_dir = self.root / ".git"
        if git_dir.is_symlink() or not git_dir.exists():
            raise GitWriterError("Repository besitzt kein sicheres .git")

    def run(
        self,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> bytes:
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.root,
                check=check,
                capture_output=True,
                env=None if env is None else {**os.environ, **env},
            ).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GitWriterError(f"Git-Befehl fehlgeschlagen: {' '.join(args)}") from exc


def _nul_paths(payload: bytes) -> tuple[str, ...]:
    return tuple(
        normalize_posix_path(token.decode("utf-8", errors="strict"))
        for token in payload.split(b"\0")
        if token
    )


def _status_paths(runner: GitRunner) -> tuple[str, ...]:
    payload = runner.run("status", "--porcelain=v1", "-z", "--untracked-files=all")
    tokens = payload.split(b"\0")
    paths: list[str] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        row = tokens[index].decode("utf-8", errors="strict")
        index += 1
        if len(row) < 4:
            raise GitWriterError(f"Ungültige Git-Statuszeile: {row!r}")
        code = row[:2]
        path = normalize_posix_path(row[3:])
        if code[0] in {"R", "C"} or code[1] in {"R", "C"}:
            paths.append(path)
            if index >= len(tokens) or not tokens[index]:
                raise GitWriterError("Unvollständiger Rename-/Copy-Status")
            path = normalize_posix_path(tokens[index].decode("utf-8", errors="strict"))
            index += 1
        paths.append(path)
    return tuple(sorted(set(paths)))


def _staged_paths(runner: GitRunner) -> tuple[str, ...]:
    return tuple(
        sorted(
            _nul_paths(
                runner.run(
                    "diff",
                    "--cached",
                    "--name-only",
                    "-z",
                    "--diff-filter=ACMRDTUXB",
                )
            )
        )
    )


def _staged_entry(runner: GitRunner, path: str) -> TreeEntry:
    stage = runner.run("ls-files", "--stage", "--", path).decode("utf-8").strip()
    if not stage:
        raise GitWriterError(f"Staged Pfad fehlt im Index: {path}")
    fields = stage.split(maxsplit=3)
    if len(fields) != 4:
        raise GitWriterError(f"Ungültiger Indexeintrag: {path}")
    mode = fields[0]
    if mode not in {"100644", "100755"}:
        raise GitWriterError(f"Unzulässiger Indexmodus {mode}: {path}")
    payload = runner.run("show", f":{path}")
    return TreeEntry(
        path=path,
        mode=mode,
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def working_tree_entries(root: Path, paths: Sequence[str]) -> tuple[TreeEntry, ...]:
    """Create commit-plan entries from exact regular working-tree files."""

    repository = Path(root).resolve(strict=True)
    entries: list[TreeEntry] = []
    for raw in sorted(set(paths)):
        path = normalize_posix_path(raw)
        candidate = repository / path
        if candidate.is_symlink() or not candidate.is_file():
            raise GitWriterError(f"Commitquelle ist keine reguläre Datei: {path}")
        mode = "100755" if stat.S_IMODE(candidate.stat().st_mode) & 0o111 else "100644"
        entries.append(
            TreeEntry(
                path=path,
                mode=mode,
                sha256=hashlib.sha256(candidate.read_bytes()).hexdigest(),
            )
        )
    return tuple(entries)


def apply_commit_plan(
    plan: CommitPlan,
    *,
    repository_root: Path,
    push: bool,
    push_env: dict[str, str] | None = None,
) -> GitWriteResult:
    """Stage exactly the plan paths, commit once, and optionally push safely."""

    if not isinstance(plan, CommitPlan):
        raise TypeError("plan muss CommitPlan sein")
    runner = GitRunner(repository_root)
    head = runner.run("rev-parse", "HEAD").decode("ascii").strip()
    if head != plan.expected_base_sha:
        raise GitWriterError("Lokaler HEAD weicht von expected_base_sha ab")

    actual_dirty = set(_status_paths(runner))
    expected = set(plan.changed_paths)
    unexpected_dirty = sorted(actual_dirty - expected)
    if unexpected_dirty:
        raise GitWriterError(
            "Arbeitsbaum enthält nicht geplante Änderungen: " + ", ".join(unexpected_dirty)
        )
    missing_dirty = sorted(expected - actual_dirty)
    if missing_dirty:
        raise GitWriterError(
            "CommitPlan-Pfade besitzen keine Arbeitsbaumänderung: " + ", ".join(missing_dirty)
        )

    runner.run("add", "--", *plan.changed_paths)
    validate_index(Path(repository_root))
    staged = _staged_paths(runner)
    if staged != plan.changed_paths:
        raise GitWriterError(
            f"Staged Pfade driften: erwartet {plan.changed_paths}, gefunden {staged}"
        )
    status_diagnostic = runner.run("status", "--short").decode("utf-8", errors="replace")
    diff_diagnostic = runner.run(
        "diff", "--cached", "--name-status", "--no-renames"
    ).decode("utf-8", errors="replace")
    quiet = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=runner.root,
        check=False,
        capture_output=True,
    ).returncode
    if quiet == 0:
        return GitWriteResult(False, None, False, (), status_diagnostic, diff_diagnostic)
    if quiet != 1:
        raise GitWriterError("Staged No-op-Prüfung ist fehlgeschlagen")

    staged_entries = tuple(_staged_entry(runner, path) for path in staged)
    staged_tree_sha = compute_tree_sha256(staged_entries)
    if staged_tree_sha != plan.tree_sha256 or staged_entries != plan.entries:
        raise GitWriterError("Staged Baum stimmt nicht mit CommitPlan überein")

    runner.run("commit", "-m", plan.subject, "-m", plan.body)
    commit_sha = runner.run("rev-parse", "HEAD").decode("ascii").strip()
    count = int(
        runner.run(
            "rev-list", "--count", f"{plan.expected_base_sha}..{commit_sha}"
        ).decode("ascii")
    )
    if count != 1:
        raise GitWriterError(f"Writer erzeugte {count} statt genau eines Commits")

    pushed = False
    if push:
        runner.run("fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main", env=push_env)
        remote = runner.run("rev-parse", "refs/remotes/origin/main").decode("ascii").strip()
        if remote != plan.expected_base_sha:
            raise GitWriterError("origin/main änderte sich seit der Dispatchplanung")
        runner.run("push", "origin", "HEAD:main", env=push_env)
        pushed = True

    return GitWriteResult(
        changed=True,
        commit_sha=commit_sha,
        pushed=pushed,
        paths=staged,
        status_diagnostic=status_diagnostic,
        diff_diagnostic=diff_diagnostic,
    )
