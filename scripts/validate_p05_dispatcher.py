#!/usr/bin/env python3
"""Validate P05 scheduling, transaction, writer, and workflow invariants."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.commit_plan import TreeEntry, build_commit_plan  # noqa: E402
from scripts.rki_pipeline.dispatch_plan import DispatchPlan  # noqa: E402
from scripts.rki_pipeline.dispatcher import build_backfill_plan, build_daily_plan  # noqa: E402
from scripts.rki_pipeline.due_tasks import (  # noqa: E402
    TaskKind,
    load_dispatcher_config,
)
from scripts.rki_pipeline.run_modes import RunMode  # noqa: E402
from scripts.rki_pipeline.storage.base import StorageBackend  # noqa: E402
from scripts.validate_ci_mutation_safety import validate_repository  # noqa: E402

WORKFLOW_ROOT = ROOT / ".github" / "workflows"
REQUIRED_RUNTIME = {
    "config/dispatcher.toml",
    "scripts/rki_pipeline/due_tasks.py",
    "scripts/rki_pipeline/dispatch_plan.py",
    "scripts/rki_pipeline/dispatcher.py",
    "scripts/rki_pipeline/transaction.py",
    "scripts/rki_pipeline/commit_plan.py",
    "scripts/rki_pipeline/git_writer.py",
    "scripts/rki_pipeline/pipeline_cli.py",
    "scripts/validate_ci_mutation_safety.py",
    ".github/workflows/rki-dispatcher.yml",
    ".github/workflows/rki-pipeline.yml",
    ".github/workflows/rki-backfill.yml",
}
REQUIRED_TESTS = {
    "tests/test_due_tasks.py",
    "tests/test_dispatch_plan.py",
    "tests/test_dispatcher.py",
    "tests/test_write_transaction.py",
    "tests/test_commit_plan.py",
    "tests/test_git_writer.py",
    "tests/test_pipeline_cli.py",
    "tests/test_p05_workflows.py",
    "tests/test_ci_mutation_safety.py",
}
PINNED_ACTION = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+@[0-9a-f]{40}$")


class P05ValidationError(ValueError):
    """The P05 repository surface violates a blocking invariant."""


def _yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise P05ValidationError(f"Workflow ist nicht lesbar: {path}") from exc
    if not isinstance(value, dict):
        raise P05ValidationError(f"Workflowwurzel ist kein Objekt: {path}")
    return value


def _triggers(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("on", data.get(True))
    if not isinstance(value, dict):
        raise P05ValidationError("Workflow besitzt keinen Triggervertrag")
    return value


def _workflow(name: str) -> tuple[Path, dict[str, Any]]:
    path = WORKFLOW_ROOT / name
    return path, _yaml(path)


def _steps(data: dict[str, Any], job: str) -> list[dict[str, Any]]:
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not isinstance(jobs.get(job), dict):
        raise P05ValidationError(f"Workflowjob fehlt: {job}")
    steps = jobs[job].get("steps")
    if not isinstance(steps, list) or not all(isinstance(step, dict) for step in steps):
        raise P05ValidationError(f"Workflowjob besitzt keine analysierbaren Schritte: {job}")
    return steps


def _named_index(steps: list[dict[str, Any]], name: str) -> int:
    matches = [index for index, step in enumerate(steps) if step.get("name") == name]
    if len(matches) != 1:
        raise P05ValidationError(f"Workflowschritt muss genau einmal existieren: {name}")
    return matches[0]


def _validate_files() -> None:
    missing = sorted(
        relative
        for relative in REQUIRED_RUNTIME | REQUIRED_TESTS
        if not (ROOT / relative).is_file()
    )
    if missing:
        raise P05ValidationError(f"P05-Dateien fehlen: {missing}")


def _validate_requirements() -> None:
    index = json.loads(
        (ROOT / "docs" / "requirements" / "requirement-index.json").read_text(
            encoding="utf-8"
        )
    )
    known = set(index["must_ids"]) | set(index["v2_ids"])
    required = {
        "MUSS-03",
        "MUSS-05",
        "MUSS-06",
        *(f"V2-05-DISPATCH-{number:03d}" for number in range(1, 11)),
        *(f"V2-14-GIT-{number:03d}" for number in range(1, 8)),
    }
    missing = sorted(required - known)
    if missing:
        raise P05ValidationError(f"P05-Anforderungs-IDs fehlen: {missing}")


def _validate_behavior() -> None:
    config = load_dispatcher_config(ROOT / "config" / "dispatcher.toml")
    status = {
        "periods": {
            "last_completed_week": "2026-W29",
            "last_completed_month": "2026-05",
            "last_completed_year": 2024,
            "last_reconciliation_at": "2026-01-01T00:00:00Z",
        }
    }
    daily = build_daily_plan(
        status=status,
        config_path=ROOT / "config" / "dispatcher.toml",
        now="2026-07-31T12:00:00Z",
        base_sha="a" * 40,
        trigger="schedule",
        run_mode=RunMode.APPLY,
        storage_backend=StorageBackend.LFS,
    )
    if not daily.tasks or daily.tasks != tuple(
        sorted(
            daily.tasks,
            key=lambda task: (
                {
                    TaskKind.WEEK: 0,
                    TaskKind.MONTH: 1,
                    TaskKind.YEAR: 2,
                    TaskKind.RECONCILIATION: 3,
                }[task.kind],
                task.period,
                task.task_id,
            ),
        )
    ):
        raise P05ValidationError("Daily Dispatchplan ist leer oder nicht kanonisch")
    if DispatchPlan.from_dict(daily.to_envelope()) != daily:
        raise P05ValidationError("Dispatchplan-Roundtrip ist nicht stabil")

    backfill = build_backfill_plan(
        config_path=ROOT / "config" / "dispatcher.toml",
        now="2026-07-31T12:00:00Z",
        base_sha="a" * 40,
        from_year=config.backfill.minimum_year,
        to_year=config.backfill.minimum_year + 1,
        max_tasks=2,
        run_mode=RunMode.PLAN,
        storage_backend=StorageBackend.LFS,
    )
    if [task.period for task in backfill.tasks] != ["1994", "1995"]:
        raise P05ValidationError("Backfill-Plan ist nicht deterministisch")

    commit = build_commit_plan(
        expected_base_sha="a" * 40,
        entries=(TreeEntry("status.json", "100644", "b" * 64),),
        task_ids=("year:2025",),
        dispatch_plan_sha256="c" * 64,
    )
    if commit.changed_paths != ("status.json",) or "year:2025" not in commit.body:
        raise P05ValidationError("CommitPlan-Vertrag ist nicht stabil")


def _validate_one_schedule() -> None:
    scheduled: list[str] = []
    for path in sorted((*WORKFLOW_ROOT.glob("*.yml"), *WORKFLOW_ROOT.glob("*.yaml"))):
        if "schedule" in _triggers(_yaml(path)):
            scheduled.append(path.name)
    if scheduled != ["rki-dispatcher.yml"]:
        raise P05ValidationError(
            f"Genau rki-dispatcher.yml darf einen Zeitplan besitzen: {scheduled}"
        )


def _validate_pinned_actions(steps: list[dict[str, Any]], workflow: str) -> None:
    for step in steps:
        uses = step.get("uses")
        if type(uses) is not str or uses.startswith("./"):
            continue
        if PINNED_ACTION.fullmatch(uses) is None:
            raise P05ValidationError(f"Action ist nicht auf Voll-SHA gepinnt: {workflow}: {uses}")


def _validate_workflows() -> None:
    dispatcher_path, dispatcher = _workflow("rki-dispatcher.yml")
    dispatcher_triggers = _triggers(dispatcher)
    if set(dispatcher_triggers) != {"schedule", "workflow_dispatch"}:
        raise P05ValidationError("Dispatcher braucht schedule und workflow_dispatch")
    if dispatcher.get("permissions") != {"contents": "read"}:
        raise P05ValidationError("Dispatcher muss contents: read verwenden")
    dispatcher_steps = _steps(dispatcher, "plan")
    _validate_pinned_actions(dispatcher_steps, dispatcher_path.name)
    pipeline_call = dispatcher["jobs"].get("pipeline")
    if not isinstance(pipeline_call, dict) or pipeline_call.get("uses") != "./.github/workflows/rki-pipeline.yml":
        raise P05ValidationError("Dispatcher muss den wiederverwendbaren Pipelineworkflow aufrufen")
    dispatcher_text = dispatcher_path.read_text(encoding="utf-8")
    if re.search(r"\bgit\s+(?:commit|push)\b", dispatcher_text):
        raise P05ValidationError("Dispatcher darf niemals selbst committen oder pushen")

    pipeline_path, pipeline = _workflow("rki-pipeline.yml")
    if set(_triggers(pipeline)) != {"workflow_call", "workflow_dispatch"}:
        raise P05ValidationError("Pipeline braucht workflow_call und workflow_dispatch")
    if pipeline.get("permissions") != {"contents": "read"}:
        raise P05ValidationError("Pipeline muss standardmäßig contents: read verwenden")
    if pipeline.get("concurrency") != {
        "group": "desinfect-repository-writer",
        "cancel-in-progress": False,
    }:
        raise P05ValidationError("Pipeline-Concurrency driftet")
    steps = _steps(pipeline, "pipeline")
    _validate_pinned_actions(steps, pipeline_path.name)
    validation = _named_index(steps, "Run one global blocking validation")
    token = _named_index(steps, "Create repository-scoped Wachhund token")
    writer = _named_index(steps, "Commit and push safely")
    if not validation < token < writer:
        raise P05ValidationError("GitHub-App-Token muss nach Validierung und vor Writer entstehen")
    token_step = steps[token]
    if token_step.get("uses") != "actions/create-github-app-token@bcd2ba49218906704ab6c1aa796996da409d3eb1":
        raise P05ValidationError("Wachhund-Tokenaction driftet vom geprüften Voll-SHA")
    expected_token_scope = {
        "client-id": "${{ vars.WACHHUND_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.WACHHUND_APP_PRIVATE_KEY }}",
        "owner": "H234598",
        "repositories": "desinfect",
        "permission-contents": "write",
    }
    if token_step.get("with") != expected_token_scope:
        raise P05ValidationError("Wachhund-Token ist nicht minimal repository-scoped")
    pipeline_text = pipeline_path.read_text(encoding="utf-8")
    for forbidden in ("--force", "--force-with-lease", "GITHUB_TOKEN"):
        if forbidden in pipeline_text:
            raise P05ValidationError(f"Pipeline enthält verbotenen Writertext: {forbidden}")
    if "persist-credentials: false" not in pipeline_text:
        raise P05ValidationError("Pipelinecheckout muss Credentials verwerfen")

    backfill_path, backfill = _workflow("rki-backfill.yml")
    if set(_triggers(backfill)) != {"workflow_dispatch"}:
        raise P05ValidationError("Backfill muss ausschließlich manuell auslösbar sein")
    if backfill.get("permissions") != {"contents": "read"}:
        raise P05ValidationError("Backfill muss contents: read verwenden")
    backfill_steps = _steps(backfill, "plan")
    _validate_pinned_actions(backfill_steps, backfill_path.name)
    inputs = _triggers(backfill)["workflow_dispatch"].get("inputs")
    if not isinstance(inputs, dict) or not {
        "from_year",
        "to_year",
        "max_tasks",
        "run_mode",
        "confirm_apply",
    } <= set(inputs):
        raise P05ValidationError("Backfill-Eingaben sind unvollständig")
    if '[[ "$CONFIRM_APPLY" != "APPLY" ]]' not in backfill_path.read_text(encoding="utf-8"):
        raise P05ValidationError("Backfill apply benötigt die literale APPLY-Bestätigung")
    backfill_pipeline = backfill["jobs"].get("pipeline")
    if not isinstance(backfill_pipeline, dict) or backfill_pipeline.get("uses") != "./.github/workflows/rki-pipeline.yml":
        raise P05ValidationError("Backfill muss denselben Pipelineworkflow verwenden")


def _validate_mutation_safety() -> None:
    issues = validate_repository(ROOT)
    if issues:
        rendered = "; ".join(issue.render() for issue in issues[:10])
        raise P05ValidationError(f"Variante-B-Verstöße: {rendered}")


def validate() -> None:
    """Run every deterministic P05 gate in a fixed order."""

    _validate_files()
    _validate_requirements()
    _validate_behavior()
    _validate_one_schedule()
    _validate_workflows()
    _validate_mutation_safety()


if __name__ == "__main__":
    validate()
    print(
        "P05 dispatcher: ok; one_schedule=1; one_transaction=1; max_commits=1; "
        "concurrency=desinfect-repository-writer; ADR-003=A; ADR-014=B"
    )
