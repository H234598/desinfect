"""Regression tests for exact repository side-effect validation."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.rki_pipeline.run_modes import (
    EffectKind,
    EffectLedger,
    ModeViolation,
    RunMode,
    SideEffectGuard,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", root, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def init_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q", root], check=True)
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "status.json").write_text("{}\n", encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "seed")


def test_plan_detects_content_change_to_preexisting_untracked_path(tmp_path: Path) -> None:
    """Identical porcelain status must not hide changed untracked bytes."""

    init_repository(tmp_path)
    dirty = tmp_path / "already-untracked.txt"
    dirty.write_text("before\n", encoding="utf-8")
    with pytest.raises(ModeViolation, match="Repositoryzustand"):
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.PLAN,
            temp_root=None,
            ledger=EffectLedger(RunMode.PLAN),
        ):
            dirty.write_text("after\n", encoding="utf-8")


def test_apply_requires_registration_for_change_to_preexisting_dirty_path(
    tmp_path: Path,
) -> None:
    """A stable status path still requires a ledger entry when its bytes change."""

    init_repository(tmp_path)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("seed\n", encoding="utf-8")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-qm", "tracked")
    tracked.write_text("dirty-before\n", encoding="utf-8")

    with pytest.raises(ModeViolation, match="nicht im Ledger"):
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.APPLY,
            temp_root=None,
            ledger=EffectLedger(RunMode.APPLY),
        ):
            tracked.write_text("dirty-after\n", encoding="utf-8")


def test_guard_validates_mutations_even_when_body_raises(tmp_path: Path) -> None:
    """An operation exception cannot bypass the final side-effect snapshot."""

    init_repository(tmp_path)
    with pytest.raises(BaseExceptionGroup) as caught:
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.PLAN,
            temp_root=None,
            ledger=EffectLedger(RunMode.PLAN),
        ):
            (tmp_path / "seed.txt").write_text("changed\n", encoding="utf-8")
            raise RuntimeError("operation failed")

    rendered = " ".join(str(error) for error in caught.value.exceptions)
    assert "operation failed" in rendered
    assert "Repositoryzustand" in rendered


def test_apply_rejects_unregistered_ignored_protected_file_change(tmp_path: Path) -> None:
    init_repository(tmp_path)
    (tmp_path / ".gitignore").write_text("ignored-status.json\n", encoding="utf-8")
    git(tmp_path, "add", ".gitignore")
    git(tmp_path, "commit", "-qm", "ignore protected runtime file")
    protected = tmp_path / "ignored-status.json"
    protected.write_text("before\n", encoding="utf-8")

    with pytest.raises(ModeViolation, match="geschützt|Ledger|status"):
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.APPLY,
            temp_root=None,
            ledger=EffectLedger(RunMode.APPLY),
            protected_paths=("ignored-status.json",),
        ):
            protected.write_text("after\n", encoding="utf-8")


def test_apply_matches_each_changed_index_path_to_ledger(tmp_path: Path) -> None:
    init_repository(tmp_path)
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("before\n", encoding="utf-8")
    git(tmp_path, "add", "a.txt", "b.txt")
    git(tmp_path, "commit", "-qm", "add index fixtures")
    for name in ("a.txt", "b.txt"):
        (tmp_path / name).write_text("after\n", encoding="utf-8")
    ledger = EffectLedger(RunMode.APPLY)

    with pytest.raises(ModeViolation, match="Git-Index|b.txt"):
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.APPLY,
            temp_root=None,
            ledger=ledger,
        ):
            git(tmp_path, "add", "a.txt", "b.txt")
            ledger.record(EffectKind.GIT_INDEX, "a.txt")
            ledger.record(EffectKind.REPOSITORY_FILE, "a.txt")
            ledger.record(EffectKind.REPOSITORY_FILE, "b.txt")


def test_apply_requires_exact_resulting_commit_sha(tmp_path: Path) -> None:
    init_repository(tmp_path)
    (tmp_path / "seed.txt").write_text("changed\n", encoding="utf-8")
    git(tmp_path, "add", "seed.txt")
    ledger = EffectLedger(RunMode.APPLY)

    with pytest.raises(ModeViolation, match="Commit|HEAD"):
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.APPLY,
            temp_root=None,
            ledger=ledger,
        ):
            git(tmp_path, "commit", "-qm", "change seed")
            ledger.record(EffectKind.GIT_COMMIT, "0" * 40)
            ledger.record(EffectKind.GIT_INDEX, "seed.txt")
            ledger.record(EffectKind.REPOSITORY_FILE, "seed.txt")


def test_apply_accepts_exact_commit_index_and_repository_registration(tmp_path: Path) -> None:
    init_repository(tmp_path)
    (tmp_path / "seed.txt").write_text("changed\n", encoding="utf-8")
    git(tmp_path, "add", "seed.txt")
    ledger = EffectLedger(RunMode.APPLY)

    with SideEffectGuard(
        repository_root=tmp_path,
        mode=RunMode.APPLY,
        temp_root=None,
        ledger=ledger,
    ):
        git(tmp_path, "commit", "-qm", "change seed")
        ledger.record(EffectKind.GIT_COMMIT, git(tmp_path, "rev-parse", "HEAD"))
        ledger.record(EffectKind.GIT_INDEX, "seed.txt")
        ledger.record(EffectKind.REPOSITORY_FILE, "seed.txt")