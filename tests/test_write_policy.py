
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


def test_deny_first_and_unlisted_paths_fail_closed() -> None:
    policy = load_policy()
    assert classify_path("status.json", policy) == "allowed"
    assert classify_path("rki/Bulletins/Jahre/1996/a.json", policy) == "allowed"
    assert classify_path("scripts/evil.py", policy) == "denied"
    assert classify_path("README.md", policy) == "unlisted"
    with pytest.raises(WritePolicyError, match="denied"):
        validate_operations([WriteOperation("scripts/evil.py")], policy)
    with pytest.raises(WritePolicyError, match="unlisted"):
        validate_operations([WriteOperation("README.md")], policy)


def test_symlink_gitlink_duplicates_and_portable_collisions_are_blocked() -> None:
    policy = load_policy()
    with pytest.raises(WritePolicyError, match="Symlink"):
        validate_operations([WriteOperation("status.json", git_mode="120000")], policy)
    with pytest.raises(WritePolicyError, match="Gitlink"):
        validate_operations([WriteOperation("status.json", git_mode="160000")], policy)
    with pytest.raises(WritePolicyError, match="Doppelte"):
        validate_operations([WriteOperation("status.json"), WriteOperation("status.json")], policy)
    with pytest.raises(ValueError, match="Pfadkollision"):
        validate_operations([
            WriteOperation("rki/Bulletins/A.json"),
            WriteOperation("rki/Bulletins/a.json"),
        ], policy)


def test_staged_index_rejects_protected_path_before_commit(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q", tmp_path], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", tmp_path, "config", "user.name", "Test"], check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "evil.py").write_text("print('x')\n", encoding="utf-8")
    subprocess.run(["git", "-C", tmp_path, "add", "scripts/evil.py"], check=True)
    with pytest.raises(WritePolicyError, match="denied"):
        validate_index(tmp_path, load_policy())
