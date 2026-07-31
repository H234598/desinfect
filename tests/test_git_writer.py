"""Local temporary-repository tests for the single-commit Git writer."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.rki_pipeline.commit_plan import build_commit_plan
from scripts.rki_pipeline.git_writer import (
    GitWriterError,
    apply_commit_plan,
    working_tree_entries,
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", root, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "status.json").write_text("before\n", encoding="utf-8")
    git(root, "add", "status.json")
    git(root, "commit", "-qm", "seed")
    return git(root, "rev-parse", "HEAD")


def plan(root: Path, base: str, paths: tuple[str, ...] = ("status.json",)):
    return build_commit_plan(
        expected_base_sha=base,
        entries=working_tree_entries(root, paths),
        task_ids=("year:2025",),
        dispatch_plan_sha256="d" * 64,
    )


def test_writer_creates_exactly_one_commit(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    base = init_repo(repository)
    (repository / "status.json").write_text("after\n", encoding="utf-8")
    result = apply_commit_plan(plan(repository, base), repository_root=repository, push=False)
    assert result.changed is True
    assert result.pushed is False
    assert result.paths == ("status.json",)
    assert git(repository, "rev-list", "--count", f"{base}..HEAD") == "1"
    assert "status.json" in result.diff_diagnostic
    assert git(repository, "status", "--porcelain") == ""


def test_writer_rejects_unplanned_dirty_path_before_staging(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    base = init_repo(repository)
    (repository / "status.json").write_text("after\n", encoding="utf-8")
    (repository / "rogue.txt").write_text("rogue\n", encoding="utf-8")
    with pytest.raises(GitWriterError, match="nicht geplante"):
        apply_commit_plan(plan(repository, base), repository_root=repository, push=False)
    assert git(repository, "rev-parse", "HEAD") == base


def test_writer_rejects_wrong_base_before_staging(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    base = init_repo(repository)
    (repository / "status.json").write_text("after\n", encoding="utf-8")
    value = build_commit_plan(
        expected_base_sha="b" * 40,
        entries=working_tree_entries(repository, ("status.json",)),
        task_ids=("year:2025",),
        dispatch_plan_sha256="d" * 64,
    )
    with pytest.raises(GitWriterError, match="HEAD"):
        apply_commit_plan(value, repository_root=repository, push=False)
    assert git(repository, "rev-parse", "HEAD") == base


def test_writer_blocks_remote_base_drift_without_force_push(tmp_path: Path) -> None:
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
    repository = tmp_path / "repo"
    repository.mkdir()
    base = init_repo(repository)
    git(repository, "remote", "add", "origin", str(bare))
    git(repository, "push", "-u", "origin", "main")

    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", str(bare), other], check=True)
    git(other, "config", "user.email", "other@example.invalid")
    git(other, "config", "user.name", "Other")
    git(other, "checkout", "-q", "main")
    (other / "remote.txt").write_text("remote\n", encoding="utf-8")
    git(other, "add", "remote.txt")
    git(other, "commit", "-qm", "advance remote")
    git(other, "push", "origin", "main")

    (repository / "status.json").write_text("after\n", encoding="utf-8")
    with pytest.raises(GitWriterError, match="origin/main"):
        apply_commit_plan(plan(repository, base), repository_root=repository, push=True)

    remote_head = subprocess.run(
        ["git", "--git-dir", bare, "rev-parse", "refs/heads/main"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert remote_head == git(other, "rev-parse", "HEAD")
