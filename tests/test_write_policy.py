from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from scripts.rki_pipeline.write_policy import (
    WriteOperation,
    WritePolicyError,
    classify_path,
    load_policy,
    validate_index,
    validate_operations,
)


def configure_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(
        ["git", "-C", root, "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", root, "config", "user.name", "Test"],
        check=True,
    )


def test_deny_first_and_unlisted_paths_fail_closed() -> None:
    policy = load_policy()
    assert classify_path("status.json", policy) == "allowed"
    assert classify_path(
        "rki/Bulletins/Jahre/1996/a.json",
        policy,
    ) == "allowed"
    assert classify_path("scripts/evil.py", policy) == "denied"
    assert classify_path("research/rights-register.yml", policy) == "denied"
    assert classify_path("README.md", policy) == "unlisted"
    with pytest.raises(WritePolicyError, match="denied"):
        validate_operations([WriteOperation("scripts/evil.py")], policy)
    with pytest.raises(WritePolicyError, match="unlisted"):
        validate_operations([WriteOperation("README.md")], policy)


def test_symlink_gitlink_duplicates_and_portable_collisions_are_blocked() -> None:
    policy = load_policy()
    with pytest.raises(WritePolicyError, match="Symlink"):
        validate_operations(
            [WriteOperation("status.json", git_mode="120000")],
            policy,
        )
    with pytest.raises(WritePolicyError, match="Symlink"):
        validate_operations(
            [WriteOperation("status.json", previous_git_mode="120000")],
            policy,
        )
    with pytest.raises(WritePolicyError, match="Gitlink"):
        validate_operations(
            [WriteOperation("status.json", git_mode="160000")],
            policy,
        )
    with pytest.raises(WritePolicyError, match="Gitlink"):
        validate_operations(
            [WriteOperation("status.json", previous_git_mode="160000")],
            policy,
        )
    with pytest.raises(WritePolicyError, match="Doppelte"):
        validate_operations(
            [WriteOperation("status.json"), WriteOperation("status.json")],
            policy,
        )
    with pytest.raises(ValueError, match="Pfadkollision"):
        validate_operations(
            [
                WriteOperation("rki/Bulletins/A.json"),
                WriteOperation("rki/Bulletins/a.json"),
            ],
            policy,
        )


def test_existing_filesystem_symlink_component_is_rejected(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "content" / "generated-data"
    generated.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (generated / "pivot").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WritePolicyError, match="Filesystem-Symlink"):
        validate_operations(
            [WriteOperation("content/generated-data/pivot/hook.json")],
            load_policy(),
            repository_root=tmp_path,
        )


def test_staged_index_rejects_protected_path_before_commit(
    tmp_path: Path,
) -> None:
    configure_repo(tmp_path)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "evil.py").write_text(
        "print('x')\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "-C", tmp_path, "add", "scripts/evil.py"],
        check=True,
    )
    with pytest.raises(WritePolicyError, match="denied"):
        validate_index(tmp_path, load_policy())


def test_staged_deletion_of_symlink_is_rejected(tmp_path: Path) -> None:
    configure_repo(tmp_path)
    generated = tmp_path / "content" / "generated-data"
    generated.mkdir(parents=True)
    (generated / "link").symlink_to("target")
    subprocess.run(["git", "-C", tmp_path, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", tmp_path, "commit", "-qm", "add symlink"],
        check=True,
    )
    (generated / "link").unlink()
    subprocess.run(["git", "-C", tmp_path, "add", "-u"], check=True)
    with pytest.raises(WritePolicyError, match="Symlink"):
        validate_index(tmp_path, load_policy())


def test_staged_deletion_of_gitlink_is_rejected(tmp_path: Path) -> None:
    configure_repo(tmp_path)
    seed = tmp_path / "seed.txt"
    seed.write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", tmp_path, "add", "seed.txt"], check=True)
    subprocess.run(
        ["git", "-C", tmp_path, "commit", "-qm", "seed"],
        check=True,
    )
    head = subprocess.run(
        ["git", "-C", tmp_path, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            tmp_path,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{head},content/generated-data/module",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", tmp_path, "commit", "-qm", "add gitlink"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            tmp_path,
            "rm",
            "--cached",
            "-q",
            "content/generated-data/module",
        ],
        check=True,
    )
    with pytest.raises(WritePolicyError, match="Gitlink"):
        validate_index(tmp_path, load_policy())
