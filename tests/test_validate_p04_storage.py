"""Regression tests for the blocking P04 validator itself."""
from __future__ import annotations

from pathlib import Path

import pytest

import scripts.validate_p04_storage as validator


def test_offline_boundary_scan_includes_storage_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "scripts" / "rki_pipeline" / "storage"
    storage.mkdir(parents=True)
    (storage / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts" / "rki_pipeline" / "storage_cli.py").write_text(
        "import socket\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "rki_pipeline" / "run_modes.py").write_text(
        "import subprocess\nsubprocess.run(['git', 'status'], check=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="socket|Netzwerk"):
        validator._validate_no_network_sdks()


def test_offline_boundary_rejects_non_git_processes_in_run_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "scripts" / "rki_pipeline" / "storage"
    storage.mkdir(parents=True)
    (storage / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "scripts" / "rki_pipeline" / "storage_cli.py").write_text(
        "from pathlib import Path\n",
        encoding="utf-8",
    )
    (tmp_path / "scripts" / "rki_pipeline" / "run_modes.py").write_text(
        "import subprocess\nsubprocess.run(['curl', 'https://example.invalid'], check=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(validator, "ROOT", tmp_path)

    with pytest.raises(ValueError, match="subprocess|git"):
        validator._validate_no_network_sdks()


def test_validate_invokes_behavioral_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def mark_called() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(validator, "_validate_behavioral_contracts", mark_called, raising=False)
    validator.validate()
    assert called