"""Offline reconciliation CLI contract."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.rki_pipeline.documents import bitstream_identity
from scripts.rki_pipeline import reconciliation, rights


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/reconciliation"


def run_cli(mode: str, fixture: Path = FIXTURE, *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[bytes]:
    values = os.environ.copy()
    values.update(env or {})
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.rki_pipeline.reconciliation",
            "--fixture",
            str(fixture),
            "--mode",
            mode,
        ],
        cwd=ROOT,
        env=values,
        capture_output=True,
        check=False,
    )


def run_command(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.rki_pipeline.cli", "reconcile", *arguments],
        cwd=ROOT,
        capture_output=True,
        check=False,
    )


def snapshot(root: Path) -> dict[str, tuple[bool, int, str]]:
    return {
        path.relative_to(root).as_posix(): (
            path.is_symlink(),
            path.lstat().st_size,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "",
        )
        for path in sorted(root.rglob("*"))
    }


@pytest.mark.parametrize("mode", ("plan", "materialize"))
def test_cli_is_deterministic_across_hostile_environment(mode: str) -> None:
    first = run_cli(mode, env={"TZ": "Pacific/Kiritimati", "SOURCE_DATE_EPOCH": "1"})
    second = run_cli(mode, env={"TZ": "UTC", "SOURCE_DATE_EPOCH": "9999999999"})

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout
    assert first.stderr == second.stderr == b""

    payload = json.loads(first.stdout)
    assert payload["mode"] == mode
    assert payload["conclusion"] == "success"
    assert payload["counts"] == {
        "changed": 0,
        "missing_local": 0,
        "missing_remote": 0,
        "ok": 1,
        "orphan": 0,
        "rights_changed": 0,
        "unresolved": 0,
    }
    assert len(payload["source_manifest_sha256"]) == 64
    assert payload["findings"] == [
        {
            "code": "ok",
            "message": "Quelle stimmt mit allen Prüfungen überein",
            "relative_path": None,
            "subject_id": "rki:176904/900000001#" + bitstream_identity(
                "https://edoc.rki.de/bitstream/handle/176904/900000001/source.pdf?sequence=1"
            ).bitstream_id,
            "subject_kind": "source",
        }
    ]
    assert payload["report_path"] == (
        None
        if mode == "plan"
        else "rki/Bulletins/Manifeste/Reconciliation/reconciliation-20260804T040000Z.json"
    )
    assert payload["changed"] is (False if mode == "plan" else True)


@pytest.mark.parametrize("mode", ("", "PLAN", "unknown", "apply"))
def test_cli_rejects_invalid_mode_without_traceback(mode: str) -> None:
    result = run_cli(mode)

    assert result.returncode == 2
    assert b"Traceback" not in result.stderr


def test_cli_rejects_missing_mode_without_traceback() -> None:
    result = run_command("--fixture", str(FIXTURE))

    assert result.returncode == 2
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    "mutation",
    ("unknown", "oversized", "symlink", "noncanonical", "as_of", "malformed", "duplicate", "float", "bool"),
)
def test_cli_rejects_invalid_fixture_with_fixed_stderr(tmp_path: Path, mutation: str) -> None:
    fixture = tmp_path / "fixture"
    shutil.copytree(FIXTURE, fixture)
    path = fixture / "fixture.json"
    if mutation == "unknown":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["unexpected"] = True
        path.write_text(json.dumps(value), encoding="utf-8")
    elif mutation == "oversized":
        path.write_bytes(b" " * 65_537)
    elif mutation == "symlink":
        target = tmp_path / "fixture.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    elif mutation == "noncanonical":
        value = json.loads(path.read_text(encoding="utf-8"))
        value["source"]["pdf_url"] = value["source"]["pdf_url"].replace("?sequence=1", "?sequence=01")
        path.write_text(json.dumps(value), encoding="utf-8")
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        if mutation == "as_of":
            value["as_of"] = "2026-08-04T04:00:01Z"
            path.write_text(json.dumps(value), encoding="utf-8")
        elif mutation == "float":
            value["scope"]["from_year"] = 2025.0
            path.write_text(json.dumps(value), encoding="utf-8")
        elif mutation == "bool":
            value["scope"]["to_year"] = True
            path.write_text(json.dumps(value), encoding="utf-8")
        elif mutation == "malformed":
            path.write_text("{", encoding="utf-8")
        else:
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    '"schema_version": "1.0.0",',
                    '"schema_version": "1.0.0", "schema_version": "1.0.0",',
                ),
                encoding="utf-8",
            )

    result = run_cli("plan", fixture)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"reconcile: fixture validation failed\n"


def test_fixture_authority_never_mutates_process_default(monkeypatch: pytest.MonkeyPatch) -> None:
    original = rights.DEFAULT_REGISTER_PATH
    loader = reconciliation._load_isolated_rights_authority

    def assert_default_then_load(path: Path):
        assert rights.DEFAULT_REGISTER_PATH == original
        return loader(path)

    monkeypatch.setattr(reconciliation, "_load_isolated_rights_authority", assert_default_then_load)

    assert reconciliation._reconcile_fixture(reconciliation._fixture_payload(FIXTURE), mode="plan")["conclusion"] == "success"
    assert rights.DEFAULT_REGISTER_PATH == original


def test_fixture_loader_reads_to_eof_after_short_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    read = reconciliation.os.read
    calls = 0

    def short_read(descriptor: int, size: int) -> bytes:
        nonlocal calls
        calls += 1
        return read(descriptor, min(size, 7))

    monkeypatch.setattr(reconciliation.os, "read", short_read)

    assert reconciliation._fixture_payload(FIXTURE)["schema_version"] == "1.0.0"
    assert calls > 1


def test_fixture_loader_rejects_root_swap_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = tmp_path / "fixture"
    replacement = tmp_path / "replacement"
    moved = tmp_path / "moved"
    shutil.copytree(FIXTURE, fixture)
    shutil.copytree(FIXTURE, replacement)

    def swap_root(root: Path, _root_fd: int) -> None:
        root.rename(moved)
        replacement.rename(root)

    monkeypatch.setattr(reconciliation, "_fixture_root_read_hook", swap_root, raising=False)

    with pytest.raises(reconciliation.FixtureValidationError):
        reconciliation._fixture_payload(fixture)


def test_cli_does_not_normalize_unrelated_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reconciliation, "_reconcile_fixture", lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bug")))

    with pytest.raises(ValueError, match="bug"):
        reconciliation.main(["--fixture", str(FIXTURE), "--mode", "plan"])


def test_cli_leaves_fixture_and_repository_unchanged() -> None:
    fixture_before = snapshot(FIXTURE)
    repository_before = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=ROOT, capture_output=True, check=True
    ).stdout

    assert run_cli("plan").returncode == 0
    assert run_cli("materialize").returncode == 0

    assert snapshot(FIXTURE) == fixture_before
    assert (
        subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=ROOT, capture_output=True, check=True
        ).stdout
        == repository_before
    )
