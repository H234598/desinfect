"""TDD contract for strict plan/materialize/apply isolation."""
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


def init_repository(root: Path) -> None:
    """Create one deterministic local Git repository for snapshot tests."""

    subprocess.run(["git", "init", "-q", root], check=True)
    subprocess.run(
        ["git", "-C", root, "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", root, "config", "user.name", "Test"],
        check=True,
    )
    (root / "status.json").write_text('{"status":"clean"}\n', encoding="utf-8")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", root, "add", "."], check=True)
    subprocess.run(["git", "-C", root, "commit", "-qm", "seed"], check=True)


@pytest.mark.parametrize(
    ("mode", "effect", "allowed"),
    (
        (RunMode.PLAN, EffectKind.REPOSITORY_FILE, False),
        (RunMode.PLAN, EffectKind.TEMP_FILE, False),
        (RunMode.MATERIALIZE, EffectKind.TEMP_FILE, True),
        (RunMode.MATERIALIZE, EffectKind.GIT_INDEX, False),
        (RunMode.MATERIALIZE, EffectKind.STATUS, False),
        (RunMode.APPLY, EffectKind.REPOSITORY_FILE, True),
        (RunMode.APPLY, EffectKind.LFS, True),
        (RunMode.APPLY, EffectKind.RELEASE, True),
        (RunMode.APPLY, EffectKind.OBJECT, True),
    ),
)
def test_mode_matrix(tmp_path: Path, mode: RunMode, effect: EffectKind, allowed: bool) -> None:
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    ledger = EffectLedger(
        mode,
        temp_root=temp_root if mode is RunMode.MATERIALIZE else None,
    )
    target = (
        (temp_root / "artifact").as_posix()
        if effect is EffectKind.TEMP_FILE
        else "artifact"
    )
    if allowed:
        ledger.record(effect, target)
        assert ledger.events[-1].kind is effect
    else:
        with pytest.raises(ModeViolation):
            ledger.record(effect, target)


def test_unknown_run_mode_fails_closed() -> None:
    with pytest.raises(ValueError):
        RunMode("preview")


def test_plan_detects_repository_file_mutation(tmp_path: Path) -> None:
    init_repository(tmp_path)
    with pytest.raises(ModeViolation, match="Repositoryzustand"):
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.PLAN,
            temp_root=None,
            ledger=EffectLedger(RunMode.PLAN),
        ):
            (tmp_path / "seed.txt").write_text("changed\n", encoding="utf-8")


def test_materialize_detects_status_and_index_mutation(tmp_path: Path) -> None:
    init_repository(tmp_path)
    temp_root = tmp_path.parent / f"{tmp_path.name}-materialized"
    temp_root.mkdir()
    with pytest.raises(ModeViolation, match="Repositoryzustand"):
        with SideEffectGuard(
            repository_root=tmp_path,
            mode=RunMode.MATERIALIZE,
            temp_root=temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
        ):
            (tmp_path / "status.json").write_text('{"status":"changed"}\n', encoding="utf-8")
            subprocess.run(["git", "-C", tmp_path, "add", "status.json"], check=True)


def test_materialize_allows_only_declared_temp_file(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    temp_root = tmp_path / "temp"
    repository.mkdir()
    temp_root.mkdir()
    init_repository(repository)
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    with SideEffectGuard(
        repository_root=repository,
        mode=RunMode.MATERIALIZE,
        temp_root=temp_root,
        ledger=ledger,
    ):
        artifact = temp_root / "artifact.bin"
        artifact.write_bytes(b"payload")
        ledger.record(EffectKind.TEMP_FILE, artifact.as_posix())
    assert len(ledger.events) == 1


def test_materialize_rejects_declared_temp_file_outside_root(tmp_path: Path) -> None:
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    with pytest.raises(ModeViolation, match="temp_root"):
        ledger.record(EffectKind.TEMP_FILE, (tmp_path / "outside.bin").as_posix())


def test_materialize_rejects_declared_temp_file_through_symlink(tmp_path: Path) -> None:
    temp_root = tmp_path / "temp"
    outside = tmp_path / "outside"
    temp_root.mkdir()
    outside.mkdir()
    (temp_root / "escape").symlink_to(outside, target_is_directory=True)
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root.resolve())

    with pytest.raises(ModeViolation, match="temp_root"):
        ledger.record(
            EffectKind.TEMP_FILE,
            (temp_root.resolve() / "escape" / "artifact.bin").as_posix(),
        )


def test_apply_requires_explicit_effect_registration(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    init_repository(repository)
    ledger = EffectLedger(RunMode.APPLY)
    with pytest.raises(ModeViolation, match="nicht im Ledger"):
        with SideEffectGuard(
            repository_root=repository,
            mode=RunMode.APPLY,
            temp_root=None,
            ledger=ledger,
        ):
            (repository / "new.txt").write_text("new\n", encoding="utf-8")


def test_apply_accepts_registered_repository_write(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    init_repository(repository)
    ledger = EffectLedger(RunMode.APPLY)
    with SideEffectGuard(
        repository_root=repository,
        mode=RunMode.APPLY,
        temp_root=None,
        ledger=ledger,
    ):
        (repository / "new.txt").write_text("new\n", encoding="utf-8")
        ledger.record(EffectKind.REPOSITORY_FILE, "new.txt")
