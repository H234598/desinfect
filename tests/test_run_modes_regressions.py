"""Regression tests for repository snapshots with already-dirty paths."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts.rki_pipeline.run_modes import (
    EffectLedger,
    ModeViolation,
    RunMode,
    SideEffectGuard,
)


def init_repository(root: Path) -> None:
    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(
        ["git", "-C", root, "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", root, "config", "user.name", "Test"],
        check=True,
    )
    (root / "status.json").write_text("{}\n", encoding="utf-8")
    subprocess.run(["git", "-C", root, "add", "status.json"], check=True)
    subprocess.run(["git", "-C", root, "commit", "-qm", "seed"], check=True)


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
    subprocess.run(["git", "-C", tmp_path, "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", tmp_path, "commit", "-qm", "tracked"], check=True)
    tracked.write_text("dirty-before\n", encoding="utf-8")

    with pytest.raises(ModeViolation, match="nicht im Ledger"):
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.APPLY,
            temp_root=None,
            ledger=EffectLedger(RunMode.APPLY),
        ):
            tracked.write_text("dirty-after\n", encoding="utf-8")
