"""Workflow-facing pipeline CLI regression tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.rki_pipeline.dispatcher import build_backfill_plan
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.pipeline_cli import main
from scripts.rki_pipeline.run_modes import RunMode
from scripts.rki_pipeline.storage.base import StorageBackend
from scripts.rki_pipeline import cli

ROOT = Path(__file__).resolve().parents[1]


def test_domain_router_keeps_existing_commands_and_adds_reconcile(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(cli.conversion_cli, "main", lambda args: calls.append(("convert", args)) or 10)
    monkeypatch.setattr(cli.archive, "main", lambda args: calls.append(("build-archive", args)) or 11)
    monkeypatch.setattr(cli.aggregation, "main", lambda args: calls.append(("aggregate", args)) or 12)
    monkeypatch.setattr(cli.reconciliation, "main", lambda args: calls.append(("reconcile", args)) or 13)

    assert cli.main(["convert", "--fixture", "a"]) == 10
    assert cli.main(["build-archive", "--fixture", "b"]) == 11
    assert cli.main(["aggregate", "--fixture", "c"]) == 12
    assert cli.main(["reconcile", "--fixture", "d"]) == 13
    assert calls == [
        ("convert", ["--fixture", "a"]),
        ("build-archive", ["--fixture", "b"]),
        ("aggregate", ["--fixture", "c"]),
        ("reconcile", ["--fixture", "d"]),
    ]


def test_domain_router_executes_reconcile_materialize_fixture() -> None:
    result = subprocess.run(
        [
            "python3",
            "-m",
            "scripts.rki_pipeline.cli",
            "reconcile",
            "--fixture",
            "tests/fixtures/reconciliation",
            "--mode",
            "materialize",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert json.loads(result.stdout)["mode"] == "materialize"


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", root, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", "-b", "main", root], check=True)
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "status.json").write_text("{}\n", encoding="utf-8")
    git(root, "add", "status.json")
    git(root, "commit", "-qm", "seed")
    return git(root, "rev-parse", "HEAD")


def plan_file(tmp_path: Path, head: str) -> Path:
    plan = build_backfill_plan(
        config_path=ROOT / "config" / "dispatcher.toml",
        now="2026-07-31T12:00:00Z",
        base_sha=head,
        from_year=1994,
        to_year=1995,
        max_tasks=2,
        run_mode=RunMode.APPLY,
        storage_backend=StorageBackend.LFS,
    )
    path = tmp_path / "dispatch.json"
    path.write_text(stable_json_dumps(plan.to_envelope()), encoding="utf-8")
    return path


def test_execute_materializes_tasks_but_leaves_repository_unchanged(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    dispatch = plan_file(tmp_path, head)
    output = tmp_path / "result.json"
    result = main(
        [
            "execute",
            "--plan",
            str(dispatch),
            "--repository-root",
            str(repository),
            "--temp-root",
            str(tmp_path / "temp"),
            "--now",
            "2026-07-31T12:00:00Z",
            "--output",
            str(output),
        ]
    )
    assert result == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["validation_count"] == 1
    assert payload["commit_required"] is False
    assert payload["changed_paths"] == []
    assert git(repository, "rev-parse", "HEAD") == head
    assert git(repository, "status", "--porcelain") == ""


def test_execute_writes_failed_run_manifest_without_changing_exit_code(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    dispatch = plan_file(tmp_path, head)
    temp_root = tmp_path / "temp"
    temp_root.write_text("not a directory\n", encoding="utf-8")
    output = tmp_path / "result.json"

    result = main(
        [
            "execute",
            "--plan",
            str(dispatch),
            "--repository-root",
            str(repository),
            "--temp-root",
            str(temp_root),
            "--now",
            "2026-07-31T12:00:00Z",
            "--output",
            str(output),
        ]
    )

    assert result == 3
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0.0"
    assert payload["run_manifest"]["status"] == "failed"


def test_execute_preserves_transaction_exit_when_failure_output_cannot_write(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    dispatch = plan_file(tmp_path, head)
    temp_root = tmp_path / "temp"
    temp_root.write_text("not a directory\n", encoding="utf-8")
    output_parent = tmp_path / "blocked"
    output_parent.write_text("not a directory\n", encoding="utf-8")

    result = main(
        [
            "execute",
            "--plan",
            str(dispatch),
            "--repository-root",
            str(repository),
            "--temp-root",
            str(temp_root),
            "--now",
            "2026-07-31T12:00:00Z",
            "--output",
            str(output_parent / "result.json"),
        ]
    )

    assert result == 3


def test_validate_plan_rejects_corrupt_hash(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    path = plan_file(tmp_path, head)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["sha256"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert main(["validate-plan", "--plan", str(path)]) == 2
    assert capsys.readouterr().err.startswith("pipeline-cli:")


def test_execute_blocks_base_drift_before_materialization(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    dispatch = plan_file(tmp_path, head)
    (repository / "next.txt").write_text("next\n", encoding="utf-8")
    git(repository, "add", "next.txt")
    git(repository, "commit", "-qm", "move head")
    temp_root = tmp_path / "temp"
    output = tmp_path / "result.json"
    assert main(
        [
            "execute",
            "--plan",
            str(dispatch),
            "--repository-root",
            str(repository),
            "--temp-root",
            str(temp_root),
            "--now",
            "2026-07-31T12:00:00Z",
            "--output",
            str(output),
        ]
    ) == 3
    assert not temp_root.exists()
    assert not output.exists()


def test_commit_push_requires_confirmation_and_app_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    invalid = tmp_path / "commit.json"
    invalid.write_text("{}\n", encoding="utf-8")
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert main(["commit", "--plan", str(invalid), "--push"]) == 2
