"""Contracts for the read-only dispatcher API and CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.rki_pipeline.dispatch_plan import DispatchPlan
from scripts.rki_pipeline.dispatcher import (
    build_backfill_plan,
    build_daily_plan,
    build_parser,
    main,
)
from scripts.rki_pipeline.due_tasks import DueTaskError
from scripts.rki_pipeline.run_modes import RunMode
from scripts.rki_pipeline.storage.base import StorageBackend

ROOT = Path(__file__).resolve().parents[1]


def minimal_status() -> dict[str, object]:
    return {
        "periods": {
            "last_completed_week": "2026-W29",
            "last_completed_month": "2026-05",
            "last_completed_year": 2024,
            "last_reconciliation_at": "2026-01-01T00:00:00Z",
        }
    }


def test_daily_plan_contains_due_tasks_and_observed_base() -> None:
    value = build_daily_plan(
        status=minimal_status(),
        config_path=ROOT / "config" / "dispatcher.toml",
        now="2026-07-31T12:00:00Z",
        base_sha="a" * 40,
        trigger="schedule",
        run_mode=RunMode.APPLY,
        storage_backend=StorageBackend.LFS,
    )
    assert value.base_sha == "a" * 40
    assert value.tasks
    assert value.trigger == "schedule"


def test_backfill_enforces_configured_request_limit() -> None:
    with pytest.raises(DueTaskError, match="max_tasks"):
        build_backfill_plan(
            config_path=ROOT / "config" / "dispatcher.toml",
            now="2026-07-31T12:00:00Z",
            base_sha="a" * 40,
            from_year=1994,
            to_year=1995,
            max_tasks=1001,
            run_mode=RunMode.PLAN,
            storage_backend=StorageBackend.LFS,
        )


def test_cli_emits_one_plan_to_stdout_without_output_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = main(
        [
            "--config",
            str(ROOT / "config" / "dispatcher.toml"),
            "--now",
            "2026-07-31T12:00:00Z",
            "--base-sha",
            "a" * 40,
            "--run-mode",
            "plan",
            "backfill",
            "--from-year",
            "1994",
            "--to-year",
            "1995",
            "--max-tasks",
            "2",
        ]
    )
    captured = capsys.readouterr()
    assert result == 0
    assert captured.err == ""
    payload = json.loads(captured.out)
    parsed = DispatchPlan.from_dict(payload)
    assert [task.period for task in parsed.tasks] == ["1994", "1995"]


def test_dispatcher_parser_has_no_output_file_switch() -> None:
    parser = build_parser()
    assert "--output" not in parser.format_help()
    subparsers = next(
        action for action in parser._actions  # noqa: SLF001
        if action.__class__.__name__ == "_SubParsersAction"
    )
    assert "--output" not in subparsers.choices["daily"].format_help()
    assert "--output" not in subparsers.choices["backfill"].format_help()


def test_cli_reports_invalid_status_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    invalid = tmp_path / "status.json"
    invalid.write_text("{}\n", encoding="utf-8")
    result = main(
        [
            "--config",
            str(ROOT / "config" / "dispatcher.toml"),
            "--now",
            "2026-07-31T12:00:00Z",
            "--base-sha",
            "a" * 40,
            "daily",
            "--status",
            str(invalid),
        ]
    )
    assert result == 2
    assert capsys.readouterr().err.startswith("dispatcher:")
