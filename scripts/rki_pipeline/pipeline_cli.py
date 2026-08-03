#!/usr/bin/env python3
"""Workflow-facing P05 transaction, commit-plan, and exact Git writer CLI."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from scripts.rki_pipeline.commit_plan import CommitPlan, build_commit_plan
from scripts.rki_pipeline.dispatch_plan import DispatchPlan, DispatchPlanError
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind
from scripts.rki_pipeline.git_writer import (
    GitWriterError,
    apply_commit_plan,
    working_tree_entries,
)
from scripts.rki_pipeline.io_utils import atomic_write_text, stable_json_dumps
from scripts.rki_pipeline.run_modes import EffectKind
from scripts.rki_pipeline.transaction import (
    TaskPlan,
    TaskResult,
    TransactionContext,
    TransactionError,
    execute_transaction,
)


class InfrastructureHandler:
    """P05-safe handler that proves orchestration without claiming domain work."""

    def plan(self, task: DueTask, context: TransactionContext) -> TaskPlan:
        return TaskPlan(task.task_id, task.to_dict())

    def materialize(self, plan: TaskPlan, context: TransactionContext) -> TaskResult:
        if context.temp_root is None:
            raise ValueError("InfrastructureHandler benötigt temp_root")
        target = context.temp_root / "tasks" / f"{plan.task_id.replace(':', '_')}.json"
        rendered = stable_json_dumps(plan.payload)
        atomic_write_text(target, rendered, allowed_root=context.temp_root)
        context.ledger.record(
            EffectKind.TEMP_FILE,
            target.absolute().as_posix(),
            size=len(rendered.encode("utf-8")),
        )
        return TaskResult(plan.task_id, target)

    def apply(self, task: DueTask, result: TaskResult, context: TransactionContext):
        # P06/P07 own domain writes and watermark completion. P05 infrastructure
        # therefore returns a verified no-op instead of claiming work it did not do.
        return ()


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"JSON ist nicht lesbar: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON-Wurzel muss Objekt sein: {path}")
    return value


def _head(repository_root: Path) -> str:
    try:
        return subprocess.run(
            ["git", "-C", repository_root, "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError("Aktueller Repository-HEAD ist nicht lesbar") from exc


def _handlers() -> dict[TaskKind, InfrastructureHandler]:
    handler = InfrastructureHandler()
    return {kind: handler for kind in TaskKind}


def _validate_materialized(values: tuple[TaskResult, ...]) -> None:
    for value in values:
        path = Path(value.payload)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Materialisiertes Task-Artefakt fehlt: {value.task_id}")
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if parsed.get("task_id") != value.task_id:
            raise ValueError(f"Materialisiertes Task-Artefakt driftet: {value.task_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-plan", help="Validate and echo one dispatch plan")
    validate.add_argument("--plan", type=Path, required=True)

    execute = sub.add_parser("execute", help="Run the P05 infrastructure transaction")
    execute.add_argument("--plan", type=Path, required=True)
    execute.add_argument("--repository-root", type=Path, default=Path("."))
    execute.add_argument("--temp-root", type=Path, required=True)
    execute.add_argument("--now", required=True)
    execute.add_argument("--output", type=Path)

    create = sub.add_parser("build-commit-plan", help="Build a commit contract from changed files")
    create.add_argument("--dispatch-plan", type=Path, required=True)
    create.add_argument("--repository-root", type=Path, default=Path("."))
    create.add_argument("--path", action="append", required=True)

    commit = sub.add_parser("commit", help="Apply an exact commit plan")
    commit.add_argument("--plan", type=Path, required=True)
    commit.add_argument("--repository-root", type=Path, default=Path("."))
    commit.add_argument("--push", action="store_true")
    commit.add_argument("--confirm-apply")
    return parser


def _write_or_print(payload: dict[str, object], output: Path | None) -> None:
    rendered = stable_json_dumps(payload)
    if output is None:
        print(rendered, end="")
    else:
        atomic_write_text(output, rendered, allowed_root=output.parent)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "validate-plan":
            plan = DispatchPlan.from_dict(_json(args.plan))
            print(stable_json_dumps(plan.to_envelope()), end="")
            return 0

        if args.command == "execute":
            plan = DispatchPlan.from_dict(_json(args.plan))
            current_head = _head(args.repository_root)
            result = execute_transaction(
                plan,
                current_head=current_head,
                repository_root=args.repository_root,
                temp_root=args.temp_root,
                handlers=_handlers(),
                validator=_validate_materialized,
                now=args.now,
            )
            payload: dict[str, object] = {
                "schema_version": "1.0.0",
                "dispatch_plan_sha256": result.dispatch_plan_sha256,
                "tasks": list(result.tasks),
                "changed_paths": list(result.changed_paths),
                "validation_count": result.validation_count,
                "commit_required": result.commit_required,
                "run_manifest": result.run_manifest,
            }
            _write_or_print(payload, args.output)
            return 0

        if args.command == "build-commit-plan":
            dispatch = DispatchPlan.from_dict(_json(args.dispatch_plan))
            value = build_commit_plan(
                expected_base_sha=dispatch.base_sha,
                entries=working_tree_entries(args.repository_root, tuple(args.path)),
                task_ids=(task.task_id for task in dispatch.tasks),
                dispatch_plan_sha256=dispatch.sha256,
            )
            print(stable_json_dumps(value.to_dict()), end="")
            return 0

        plan = CommitPlan.from_dict(_json(args.plan))
        if args.push:
            if args.confirm_apply != "APPLY":
                raise GitWriterError("Push benötigt --confirm-apply APPLY")
            if not os.environ.get("GH_TOKEN"):
                raise GitWriterError("Push benötigt einen kurzlebigen GitHub-App-Token in GH_TOKEN")
        result = apply_commit_plan(
            plan,
            repository_root=args.repository_root,
            push=args.push,
        )
        print(
            stable_json_dumps(
                {
                    "changed": result.changed,
                    "commit_sha": result.commit_sha,
                    "pushed": result.pushed,
                    "paths": list(result.paths),
                }
            ),
            end="",
        )
        return 0
    except (OSError, ValueError, DispatchPlanError, TransactionError, GitWriterError) as exc:
        print(f"pipeline-cli: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, (ValueError, DispatchPlanError, GitWriterError)) else 3


if __name__ == "__main__":
    raise SystemExit(main())
