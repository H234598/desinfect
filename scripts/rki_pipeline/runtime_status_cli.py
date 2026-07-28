
#!/usr/bin/env python3
"""Command-line interface for strict run status and public projection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from scripts.rki_pipeline.runtime_status import (
    RuntimeStatusError,
    load_json,
    new_run,
    project_public_status,
    update_run,
    write_validated_json,
)
from scripts.rki_pipeline.schema_registry import SchemaContractError, migrate_document


def csv_values(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Create a revision-one run manifest")
    start.add_argument("--output", type=Path, required=True)
    start.add_argument("--run-id", required=True)
    start.add_argument("--workflow", default="manual")
    start.add_argument("--trigger-source", default="manual")
    start.add_argument("--run-mode", choices=("plan", "materialize", "apply"), default="plan")
    start.add_argument("--storage-backend", choices=("lfs", "release", "object"), default="lfs")
    start.add_argument("--tasks", default="")

    update = sub.add_parser("update", help="Advance an existing run")
    update.add_argument("--input", type=Path, required=True)
    update.add_argument("--output", type=Path)
    update.add_argument("--expected-revision", type=int, required=True)
    update.add_argument("--status", required=True)
    update.add_argument("--phase", required=True)
    update.add_argument("--completed-phases", default="")
    update.add_argument("--error-class")
    update.add_argument("--error-code")
    update.add_argument("--error-message")
    update.add_argument("--retryable", action="store_true")
    update.add_argument("--recovery-level")
    update.add_argument("--recovery-action")
    update.add_argument("--resume-phase")
    update.add_argument("--block-next-run", action="store_true")

    finish = sub.add_parser("finish", help="Finalize and project a run")
    finish.add_argument("--input", type=Path, required=True)
    finish.add_argument("--output", type=Path, required=True)
    finish.add_argument("--public-status", type=Path, required=True)
    finish.add_argument("--expected-revision", type=int, required=True)
    finish.add_argument("--status", choices=("success", "no_op", "blocked", "failed", "recovered"), required=True)
    finish.add_argument("--content-changed", action="store_true")
    finish.add_argument("--last-main-commit-at")

    restore = sub.add_parser("restore", help="Migrate/validate one snapshot and write it atomically")
    restore.add_argument("--name", default="status")
    restore.add_argument("--snapshot", type=Path, required=True)
    restore.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            payload = new_run(
                workflow=args.workflow,
                trigger_source=args.trigger_source,
                run_mode=args.run_mode,
                storage_backend=args.storage_backend,
                tasks=csv_values(args.tasks),
                run_id=args.run_id,
            )
            write_validated_json(args.output, "run-manifest", payload)
        elif args.command == "update":
            source = load_json(args.input)
            error = None
            if args.error_code or args.error_message or args.error_class:
                error = {
                    "class": args.error_class or "unknown",
                    "code": args.error_code or "unknown",
                    "message": args.error_message or "Unbekannter Fehler",
                    "retryable": args.retryable,
                }
            recovery = None
            if args.recovery_level or args.recovery_action:
                recovery = {
                    "level": args.recovery_level or "manual_intervention",
                    "action": args.recovery_action or "Manuelle Prüfung erforderlich",
                    "resume_phase": args.resume_phase,
                    "block_next_run": args.block_next_run,
                    "acknowledged": False,
                }
            payload = update_run(
                source,
                expected_revision=args.expected_revision,
                status=args.status,
                phase=args.phase,
                completed_phases=csv_values(args.completed_phases),
                error=error,
                recovery=recovery,
            )
            write_validated_json(args.output or args.input, "run-manifest", payload)
        elif args.command == "finish":
            source = load_json(args.input)
            finalized = update_run(
                source,
                expected_revision=args.expected_revision,
                status=args.status,
                phase="complete",
                completed_phases=[*source.get("completed_phases", []), "complete"],
                error=source.get("error"),
                recovery=source.get("recovery"),
            )
            write_validated_json(args.output, "run-manifest", finalized)
            public = project_public_status(
                load_json(args.public_status),
                finalized,
                content_changed=args.content_changed,
                last_main_commit_at=args.last_main_commit_at,
            )
            write_validated_json(args.public_status, "status", public)
        elif args.command == "restore":
            payload = migrate_document(args.name, load_json(args.snapshot))
            write_validated_json(args.output, args.name, payload)
        return 0
    except (OSError, ValueError, RuntimeStatusError, SchemaContractError, json.JSONDecodeError) as exc:
        print(f"runtime-status: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
