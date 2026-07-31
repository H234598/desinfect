#!/usr/bin/env python3
"""Read-only CLI and API for daily and bounded backfill dispatch plans."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

from scripts.rki_pipeline.dispatch_plan import DispatchPlan, DispatchPlanError
from scripts.rki_pipeline.due_tasks import (
    DueTaskError,
    calculate_backfill_tasks,
    calculate_due_tasks,
    load_dispatcher_config,
    parse_utc,
)
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.run_modes import RunMode
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.storage.base import StorageBackend


def _load_status(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DueTaskError(f"Statusdatei ist nicht lesbar: {path}") from exc
    if not isinstance(value, dict):
        raise DueTaskError("Statuswurzel muss ein JSON-Objekt sein")
    validate_document("status", value)
    return value


def build_daily_plan(
    *,
    status: dict[str, Any],
    config_path: Path,
    now: str,
    base_sha: str,
    trigger: str,
    run_mode: RunMode,
    storage_backend: StorageBackend,
) -> DispatchPlan:
    """Build one bounded deterministic daily plan without side effects."""

    config = load_dispatcher_config(config_path)
    tasks = calculate_due_tasks(status, now, config.daily)
    return DispatchPlan.create(
        created_at=now,
        trigger=trigger,
        base_sha=base_sha,
        tasks=tasks,
        run_mode=run_mode,
        storage_backend=storage_backend,
    )


def build_backfill_plan(
    *,
    config_path: Path,
    now: str,
    base_sha: str,
    from_year: int,
    to_year: int,
    max_tasks: int,
    run_mode: RunMode,
    storage_backend: StorageBackend,
) -> DispatchPlan:
    """Build one explicit bounded historical plan without reading watermarks."""

    config = load_dispatcher_config(config_path)
    if type(max_tasks) is not int or not 1 <= max_tasks <= config.backfill.max_tasks:
        raise DueTaskError(
            f"max_tasks muss zwischen 1 und {config.backfill.max_tasks} liegen"
        )
    effective_limits = type(config.backfill)(
        max_tasks=max_tasks,
        minimum_year=config.backfill.minimum_year,
    )
    tasks = calculate_backfill_tasks(
        from_year=from_year,
        to_year=to_year,
        due_at=now,
        limits=effective_limits,
    )
    return DispatchPlan.create(
        created_at=now,
        trigger="backfill",
        base_sha=base_sha,
        tasks=tasks,
        run_mode=run_mode,
        storage_backend=storage_backend,
    )


def _run_mode(value: str) -> RunMode:
    try:
        return RunMode(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Unbekannter RunMode: {value}") from exc


def _backend(value: str) -> StorageBackend:
    try:
        return StorageBackend(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Unbekanntes StorageBackend: {value}") from exc


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Positive Ganzzahl erforderlich") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("Positive Ganzzahl erforderlich")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/dispatcher.toml"))
    parser.add_argument("--now", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--run-mode", type=_run_mode, default=RunMode.APPLY)
    parser.add_argument("--storage-backend", type=_backend, default=StorageBackend.LFS)
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser("daily", help="Calculate due tasks from status watermarks")
    daily.add_argument("--status", type=Path, default=Path("status.json"))
    daily.add_argument(
        "--trigger",
        choices=("schedule", "workflow_dispatch"),
        default="workflow_dispatch",
    )

    backfill = sub.add_parser("backfill", help="Create a bounded explicit year plan")
    backfill.add_argument("--from-year", type=_positive_int, required=True)
    backfill.add_argument("--to-year", type=_positive_int, required=True)
    backfill.add_argument("--max-tasks", type=_positive_int, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        parse_utc(args.now)
        if args.command == "daily":
            plan = build_daily_plan(
                status=_load_status(args.status),
                config_path=args.config,
                now=args.now,
                base_sha=args.base_sha,
                trigger=args.trigger,
                run_mode=args.run_mode,
                storage_backend=args.storage_backend,
            )
        else:
            plan = build_backfill_plan(
                config_path=args.config,
                now=args.now,
                base_sha=args.base_sha,
                from_year=args.from_year,
                to_year=args.to_year,
                max_tasks=args.max_tasks,
                run_mode=args.run_mode,
                storage_backend=args.storage_backend,
            )
        print(stable_json_dumps(plan.to_envelope()), end="")
        return 0
    except (OSError, ValueError, DueTaskError, DispatchPlanError) as exc:
        print(f"dispatcher: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
