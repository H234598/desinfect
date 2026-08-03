"""Local temporary-repository tests for the single-commit Git writer."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import scripts.rki_pipeline.git_writer as git_writer
from scripts.rki_pipeline.commit_plan import build_commit_plan
from scripts.rki_pipeline.git_writer import (
    GitRunner,
    GitWriterError,
    _staged_entry,
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


def test_working_tree_entries_rejects_symlinked_parent(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    (repository / "bridge").symlink_to(outside, target_is_directory=True)

    with pytest.raises(GitWriterError):
        working_tree_entries(repository, ("bridge/secret.txt",))


def test_writer_handles_colliding_git_pathspec_names_independently(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    base = init_repo(repository)
    generated = repository / "content" / "generated-data"
    generated.mkdir(parents=True)
    plain = generated / "a1.txt"
    bracket = generated / "a[1].txt"
    plain.write_text("plain\n", encoding="utf-8")
    bracket.write_text("bracket\n", encoding="utf-8")
    bracket.chmod(0o755)
    paths = (
        "content/generated-data/a1.txt",
        "content/generated-data/a[1].txt",
    )

    result = apply_commit_plan(
        plan(repository, base, paths),
        repository_root=repository,
        push=False,
    )

    assert result.paths == paths
    assert git(
        repository,
        "ls-tree",
        "HEAD",
        "--",
        ":(literal)content/generated-data/a[1].txt",
    ).split()[0] == "100755"


def test_writer_adds_only_literal_plan_path_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    init_repo(repository)
    generated = repository / "content" / "generated-data"
    generated.mkdir(parents=True)
    competitor = generated / "a1.txt"
    literal = generated / "a?.txt"
    competitor.write_text("before\n", encoding="utf-8")
    literal.write_text("before\n", encoding="utf-8")
    git(repository, "add", "--", "content/generated-data")
    git(repository, "commit", "-qm", "seed pathspec files")
    base = git(repository, "rev-parse", "HEAD")
    literal.write_text("after\n", encoding="utf-8")
    original_status_paths = git_writer._status_paths

    def mutate_competitor_after_status(runner: GitRunner) -> tuple[str, ...]:
        paths = original_status_paths(runner)
        competitor.write_text("raced\n", encoding="utf-8")
        return paths

    monkeypatch.setattr(git_writer, "_status_paths", mutate_competitor_after_status)

    result = apply_commit_plan(
        plan(repository, base, ("content/generated-data/a?.txt",)),
        repository_root=repository,
        push=False,
    )

    assert result.paths == ("content/generated-data/a?.txt",)
    assert git(repository, "diff", "--name-only") == "content/generated-data/a1.txt"
    assert git(repository, "show", "HEAD:content/generated-data/a1.txt") == "before"


def test_staged_entry_rejects_multiple_stage_records(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    git(repository, "init", "-q")
    ours = repository / "ours"
    theirs = repository / "theirs"
    ours.write_text("ours\n", encoding="utf-8")
    theirs.write_text("theirs\n", encoding="utf-8")
    ours_sha = git(repository, "hash-object", "-w", "ours")
    theirs_sha = git(repository, "hash-object", "-w", "theirs")
    subprocess.run(
        ["git", "-C", repository, "update-index", "--index-info"],
        check=True,
        input=(
            f"100644 {ours_sha} 1\tcontent/generated-data/one.txt\n"
            f"100644 {theirs_sha} 2\tcontent/generated-data/one.txt\n"
        ),
        text=True,
    )

    with pytest.raises(GitWriterError, match="genau einen"):
        _staged_entry(GitRunner(repository), "content/generated-data/one.txt")


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
