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


@pytest.mark.parametrize("mutation", ("unknown", "oversized", "symlink", "noncanonical", "as_of"))
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
        value["as_of"] = "2026-08-04T04:00:01Z"
        path.write_text(json.dumps(value), encoding="utf-8")

    result = run_cli("plan", fixture)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == b"reconcile: fixture validation failed\n"


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
