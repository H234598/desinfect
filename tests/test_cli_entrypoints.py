"""Regression tests for documented repository-root validator entry points."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

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
