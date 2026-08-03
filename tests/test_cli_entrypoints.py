"""Regression tests for documented repository-root validator entry points."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from scripts.rki_pipeline import conversion_cli
from scripts.rki_pipeline.conversion import runtime as conversion_runtime
from scripts.rki_pipeline.conversion.service import (
    ConversionError,
    ConversionNeedsReview,
)

ROOT = Path(__file__).resolve().parents[1]


def test_validator_scripts_run_by_path() -> None:
    """Keep every documented ``python3 scripts/...`` invocation executable."""

    for relative in (
        "scripts/validate_dependency_locks.py",
        "scripts/validate_fixture_manifest.py",
        "scripts/validate_p01_foundation.py",
        "scripts/validate_p02_contracts.py",
        "scripts/validate_p03_grabber.py",
        "scripts/validate_rights_register.py",
    ):
        completed = subprocess.run(
            [sys.executable, relative],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 0, (
            relative,
            completed.stdout,
            completed.stderr,
        )


@pytest.mark.skipif(
    shutil.which("pdfinfo") is None or shutil.which("pdftotext") is None,
    reason="Poppler tools are not installed",
)
def test_conversion_router_real_poppler_materialize_and_skip(tmp_path: Path) -> None:
    fixture = ROOT / "tests" / "fixtures" / "pdf" / "text.pdf"
    source_before = fixture.read_bytes()
    temp_root = tmp_path / "materialized"
    command = [
        sys.executable,
        "-m",
        "scripts.rki_pipeline.cli",
        "convert",
        "--fixture",
        fixture.as_posix(),
        "--mode",
        "materialize",
        "--temp-root",
        temp_root.as_posix(),
    ]

    first = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    assert first.returncode == 0, (first.stdout, first.stderr)
    first_payload = json.loads(first.stdout)
    output = Path(first_payload["output"])
    manifest = Path(first_payload["manifest"])
    output_mtime = output.stat().st_mtime_ns
    manifest_mtime = manifest.stat().st_mtime_ns
    assert first_payload["status"] == "converted"
    assert first_payload["quality"] == "good"
    assert output.read_text(encoding="utf-8").count("<!-- rki-page:") == 2
    assert {effect["kind"] for effect in first_payload["effects"]} == {"temp_file"}
    assert fixture.read_bytes() == source_before

    second = subprocess.run(command, cwd=ROOT, check=False, capture_output=True, text=True)
    assert second.returncode == 0, (second.stdout, second.stderr)
    second_payload = json.loads(second.stdout)
    assert second_payload["status"] == "skipped_unchanged"
    assert second_payload["effects"] == []
    assert output.stat().st_mtime_ns == output_mtime
    assert manifest.stat().st_mtime_ns == manifest_mtime


def _conversion_args(temp_root: Path) -> list[str]:
    return [
        "--fixture",
        conversion_cli.TEXT_FIXTURE.as_posix(),
        "--mode",
        "materialize",
        "--temp-root",
        temp_root.as_posix(),
    ]


def test_conversion_cli_rejects_repository_temp_root(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(conversion_cli, "_fixture_intent", lambda _fixture: (object(), object()))
    monkeypatch.setattr(conversion_cli, "collect_runtime_evidence", lambda: object())

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("repository temp root reached materialization")

    monkeypatch.setattr(conversion_cli, "materialize_conversion", forbidden)

    assert conversion_cli.main(_conversion_args(ROOT)) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "invalid"
    assert "Repository" in payload["error"]


def test_runtime_tool_lookup_uses_process_runner_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "pdfinfo"
    executable.write_bytes(b"tool")
    observed: list[str | None] = []

    def fake_which(name: str, *, path: str | None = None) -> str:
        assert name == "pdfinfo"
        observed.append(path)
        return executable.as_posix()

    monkeypatch.setattr(conversion_runtime.shutil, "which", fake_which)

    assert conversion_runtime._tool_paths(("pdfinfo",)) == (executable,)
    assert observed == [os.defpath]


@pytest.mark.parametrize(
    ("state", "expected_exit"),
    (("converted", 0), ("skipped_unchanged", 0), ("needs_review", 4)),
)
def test_conversion_cli_result_exit_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    state: str,
    expected_exit: int,
) -> None:
    monkeypatch.setattr(conversion_cli, "_fixture_intent", lambda _fixture: (object(), object()))
    monkeypatch.setattr(conversion_cli, "collect_runtime_evidence", lambda: object())

    def fake_materialize(*args, **kwargs):
        del args
        root = Path(kwargs["temp_root"])
        return SimpleNamespace(
            state=state,
            quality="needs_review" if state == "needs_review" else "good",
            ocr_used=state == "needs_review",
            conversion_id="conv-" + "1" * 64,
            fingerprint_sha256="2" * 64,
            output_path=root / "document.md",
            manifest_path=root / "conversion-manifest.json",
        )

    monkeypatch.setattr(conversion_cli, "materialize_conversion", fake_materialize)

    assert conversion_cli.main(_conversion_args(tmp_path / "out")) == expected_exit
    assert json.loads(capsys.readouterr().out)["status"] == state


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_exit"),
    (
        (ConversionNeedsReview("tesseract fehlt"), "needs_review", 4),
        (ConversionError("parser failed"), "failed", 3),
    ),
)
def test_conversion_cli_visible_error_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error: Exception,
    expected_status: str,
    expected_exit: int,
) -> None:
    monkeypatch.setattr(conversion_cli, "_fixture_intent", lambda _fixture: (object(), object()))
    monkeypatch.setattr(conversion_cli, "collect_runtime_evidence", lambda: object())

    def fail(*args, **kwargs):
        del args, kwargs
        raise error

    monkeypatch.setattr(conversion_cli, "materialize_conversion", fail)

    assert conversion_cli.main(_conversion_args(tmp_path / "out")) == expected_exit
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == expected_status
    assert str(error) in payload["error"]


def test_conversion_cli_reports_missing_runtime_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(conversion_cli, "_fixture_intent", lambda _fixture: (object(), object()))
    pdfinfo = tmp_path / "pdfinfo"
    pdfinfo.write_bytes(b"tool")

    def missing_tool(name: str, *, path: str | None = None) -> str | None:
        assert path == os.defpath
        return pdfinfo.as_posix() if name == "pdfinfo" else None

    monkeypatch.setattr(conversion_runtime.shutil, "which", missing_tool)

    def forbidden(*args, **kwargs):
        del args, kwargs
        raise AssertionError("missing tool reached materialization")

    monkeypatch.setattr(conversion_cli, "materialize_conversion", forbidden)

    assert conversion_cli.main(_conversion_args(tmp_path / "out")) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "pdftotext" in payload["error"]
