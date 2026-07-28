from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "scripts.rki_pipeline.runtime_status_cli", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def start_and_advance(run_path: Path) -> None:
    result = run_cli(
        "start",
        "--output",
        str(run_path),
        "--run-id",
        "cli-1",
        "--workflow",
        "manual",
        "--trigger-source",
        "test",
        "--run-mode",
        "apply",
        "--tasks",
        "weekly_ingest,weekly_archives",
    )
    assert result.returncode == 0, result.stderr
    result = run_cli(
        "update",
        "--input",
        str(run_path),
        "--expected-revision",
        "1",
        "--status",
        "running",
        "--phase",
        "validate",
        "--completed-phases",
        "initialize,plan,materialize",
    )
    assert result.returncode == 0, result.stderr


def test_cli_start_update_finish_roundtrip(tmp_path: Path) -> None:
    run_path = tmp_path / "run.json"
    finished = tmp_path / "finished.json"
    public = tmp_path / "status.json"
    public.write_text(
        (ROOT / "status.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    start_and_advance(run_path)
    result = run_cli(
        "finish",
        "--input",
        str(run_path),
        "--output",
        str(finished),
        "--public-status",
        str(public),
        "--expected-revision",
        "2",
        "--status",
        "success",
        "--content-changed",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(finished.read_text(encoding="utf-8"))["status"] == "success"
    assert json.loads(public.read_text(encoding="utf-8"))["status"] == "operational"


def test_update_without_completed_phases_preserves_existing_values(
    tmp_path: Path,
) -> None:
    run_path = tmp_path / "run.json"
    start_and_advance(run_path)
    result = run_cli(
        "update",
        "--input",
        str(run_path),
        "--expected-revision",
        "2",
        "--status",
        "running",
        "--phase",
        "verify",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(run_path.read_text(encoding="utf-8"))
    assert payload["completed_phases"] == [
        "initialize",
        "plan",
        "materialize",
    ]


def test_finish_projection_failure_writes_neither_output(
    tmp_path: Path,
) -> None:
    run_path = tmp_path / "run.json"
    finished = tmp_path / "finished.json"
    public = tmp_path / "status.json"
    original_public = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    original_public["unexpected"] = True
    public.write_text(json.dumps(original_public), encoding="utf-8")
    start_and_advance(run_path)

    result = run_cli(
        "finish",
        "--input",
        str(run_path),
        "--output",
        str(finished),
        "--public-status",
        str(public),
        "--expected-revision",
        "2",
        "--status",
        "success",
    )
    assert result.returncode == 2
    assert not finished.exists()
    assert json.loads(run_path.read_text(encoding="utf-8"))["status"] == "running"
    assert json.loads(public.read_text(encoding="utf-8"))["unexpected"] is True
