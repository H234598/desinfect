
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


def test_cli_start_update_finish_roundtrip(tmp_path: Path) -> None:
    run_path = tmp_path / "run.json"
    finished = tmp_path / "finished.json"
    public = tmp_path / "status.json"
    public.write_text((ROOT / "status.json").read_text(encoding="utf-8"), encoding="utf-8")

    result = run_cli(
        "start", "--output", str(run_path), "--run-id", "cli-1",
        "--workflow", "manual", "--trigger-source", "test", "--run-mode", "apply",
        "--tasks", "weekly_ingest,weekly_archives",
    )
    assert result.returncode == 0, result.stderr
    result = run_cli(
        "update", "--input", str(run_path), "--expected-revision", "1",
        "--status", "running", "--phase", "validate",
        "--completed-phases", "initialize,plan,materialize",
    )
    assert result.returncode == 0, result.stderr
    result = run_cli(
        "finish", "--input", str(run_path), "--output", str(finished),
        "--public-status", str(public), "--expected-revision", "2",
        "--status", "success", "--content-changed",
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(finished.read_text(encoding="utf-8"))["status"] == "success"
    assert json.loads(public.read_text(encoding="utf-8"))["status"] == "operational"
