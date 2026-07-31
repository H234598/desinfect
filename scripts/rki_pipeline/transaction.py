#!/usr/bin/env python3
"""All-or-nothing task orchestration with one validation and one write set."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from scripts.rki_pipeline.dispatch_plan import DispatchPlan
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind
from scripts.rki_pipeline.run_modes import EffectLedger, RunMode, SideEffectGuard
from scripts.rki_pipeline.runtime_status import new_run, update_run
from scripts.rki_pipeline.write_policy import WriteOperation, validate_operations


class TransactionError(RuntimeError):
    """A dispatch transaction could not reach one globally valid write set."""

    def __init__(self, message: str, *, run_manifest: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.run_manifest = run_manifest


@dataclass(frozen=True, slots=True)
class TaskPlan:
    """One handler-specific plan value tied to a due task."""

    task_id: str
    payload: object

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id:
            raise ValueError("TaskPlan.task_id darf nicht leer sein")


@dataclass(frozen=True, slots=True)
class TaskResult:
    """One materialized task result that remains outside the repository."""

    task_id: str
    payload: object

    def __post_init__(self) -> None:
        if type(self.task_id) is not str or not self.task_id:
            raise ValueError("TaskResult.task_id darf nicht leer sein")


@dataclass(slots=True)
class TransactionContext:
    """Explicit roots and effect ledger for one transaction phase."""

    repository_root: Path
    temp_root: Path | None
    ledger: EffectLedger


@runtime_checkable
class TaskHandler(Protocol):
    """Plan, materialize, and apply one task kind through strict phase ports."""

    def plan(self, task: DueTask, context: TransactionContext) -> TaskPlan: ...
    def materialize(self, plan: TaskPlan, context: TransactionContext) -> TaskResult: ...
    def apply(
        self,
        task: DueTask,
        result: TaskResult,
        context: TransactionContext,
    ) -> tuple[WriteOperation, ...]: ...


@dataclass(frozen=True, slots=True)
class TransactionResult:
    """Auditable result of one complete dispatch transaction."""

    dispatch_plan_sha256: str
    tasks: tuple[str, ...]
    changed_paths: tuple[str, ...]
    validation_count: int
    commit_required: bool
    run_manifest: dict[str, Any]


Validator = Callable[[tuple[TaskResult, ...]], None]
StatusUpdater = Callable[[dict[str, Any], DispatchPlan], dict[str, Any]]


def _handler(handlers: Mapping[TaskKind, TaskHandler], task: DueTask) -> TaskHandler:
    value = handlers.get(task.kind)
    if value is None or not isinstance(value, TaskHandler):
        raise TransactionError(f"Kein TaskHandler für {task.kind.value}")
    return value


def _failed_run(
    run: dict[str, Any],
    *,
    phase: str,
    error: BaseException,
    now: str,
) -> dict[str, Any]:
    try:
        return update_run(
            run,
            expected_revision=run["revision"],
            status="failed",
            phase=phase,
            now=now,
            error={
                "class": type(error).__name__,
                "code": "transaction_failed",
                "message": str(error),
                "retryable": True,
            },
            recovery={
                "level": "automatic_retry",
                "action": "Dispatchplan mit unverändertem main erneut ausführen",
                "resume_phase": phase,
                "block_next_run": False,
                "acknowledged": False,
            },
        )
    except BaseException:
        return run


def _coalesce_operations(values: list[WriteOperation]) -> tuple[WriteOperation, ...]:
    """Collapse identical repeated path declarations or fail on ambiguity."""

    by_path: dict[str, WriteOperation] = {}
    for value in values:
        previous = by_path.get(value.path)
        if previous is not None and previous != value:
            raise TransactionError(f"Widersprüchliche WriteOperation für {value.path}")
        by_path[value.path] = value
    return tuple(by_path[path] for path in sorted(by_path))


def _finish(
    run: dict[str, Any],
    *,
    plan: DispatchPlan,
    now: str,
    completed_phases: tuple[str, ...],
    changed_paths: tuple[str, ...] = (),
    validation_count: int = 0,
) -> TransactionResult:
    final_status = "success" if changed_paths else "no_op"
    run = update_run(
        run,
        expected_revision=run["revision"],
        status=final_status,
        phase="complete",
        now=now,
        completed_phases=completed_phases,
        metrics={
            "task_count": len(plan.tasks),
            "changed_path_count": len(changed_paths),
            "validation_count": validation_count,
        },
    )
    return TransactionResult(
        dispatch_plan_sha256=plan.sha256,
        tasks=tuple(task.task_id for task in plan.tasks),
        changed_paths=changed_paths,
        validation_count=validation_count,
        commit_required=bool(changed_paths),
        run_manifest=run,
    )


def execute_transaction(
    plan: DispatchPlan,
    *,
    current_head: str,
    repository_root: Path,
    temp_root: Path,
    handlers: Mapping[TaskKind, TaskHandler],
    validator: Validator,
    now: str,
    status: dict[str, Any] | None = None,
    status_updater: StatusUpdater | None = None,
) -> TransactionResult:
    """Execute tasks only through the phases permitted by ``plan.run_mode``."""

    if not isinstance(plan, DispatchPlan):
        raise TypeError("plan muss DispatchPlan sein")
    if type(current_head) is not str or current_head != plan.base_sha:
        raise TransactionError("Dispatchplan-Basis stimmt nicht mit aktuellem HEAD überein")
    repository = Path(repository_root).absolute()
    temporary = Path(temp_root).absolute()
    run = new_run(
        workflow="rki-pipeline",
        trigger_source=plan.trigger,
        run_mode=plan.run_mode.value,
        storage_backend=plan.storage_backend.value,
        tasks=(task.task_id for task in plan.tasks),
        run_id=f"dispatch-{plan.sha256[:24]}",
        now=now,
        branch="main",
        commit_sha=current_head,
    )
    validation_count = 0
    try:
        run = update_run(
            run,
            expected_revision=run["revision"],
            status="running",
            phase="plan",
            now=now,
            completed_phases=("initialize",),
        )
        plan_ledger = EffectLedger(RunMode.PLAN)
        planned: list[TaskPlan] = []
        with SideEffectGuard(
            repository_root=repository,
            mode=RunMode.PLAN,
            temp_root=None,
            ledger=plan_ledger,
        ):
            for task in plan.tasks:
                value = _handler(handlers, task).plan(
                    task,
                    TransactionContext(repository, None, plan_ledger),
                )
                if value.task_id != task.task_id:
                    raise TransactionError(f"TaskPlan-ID driftet: {task.task_id}")
                planned.append(value)
        if plan.run_mode is RunMode.PLAN:
            return _finish(
                run,
                plan=plan,
                now=now,
                completed_phases=("initialize", "plan"),
            )

        temporary.mkdir(parents=True, exist_ok=True)
        run = update_run(
            run,
            expected_revision=run["revision"],
            status="running",
            phase="materialize",
            now=now,
            completed_phases=("initialize", "plan"),
        )
        materialize_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temporary)
        materialized: list[TaskResult] = []
        with SideEffectGuard(
            repository_root=repository,
            mode=RunMode.MATERIALIZE,
            temp_root=temporary,
            ledger=materialize_ledger,
        ):
            for task, task_plan in zip(plan.tasks, planned, strict=True):
                value = _handler(handlers, task).materialize(
                    task_plan,
                    TransactionContext(repository, temporary, materialize_ledger),
                )
                if value.task_id != task.task_id:
                    raise TransactionError(f"TaskResult-ID driftet: {task.task_id}")
                materialized.append(value)

        run = update_run(
            run,
            expected_revision=run["revision"],
            status="running",
            phase="validate",
            now=now,
            completed_phases=("initialize", "plan", "materialize"),
        )
        validator(tuple(materialized))
        validation_count += 1
        if plan.run_mode is RunMode.MATERIALIZE:
            return _finish(
                run,
                plan=plan,
                now=now,
                completed_phases=("initialize", "plan", "materialize", "validate"),
                validation_count=validation_count,
            )

        run = update_run(
            run,
            expected_revision=run["revision"],
            status="running",
            phase="apply",
            now=now,
            completed_phases=("initialize", "plan", "materialize", "validate"),
        )
        apply_ledger = EffectLedger(RunMode.APPLY)
        operations: list[WriteOperation] = []
        with SideEffectGuard(
            repository_root=repository,
            mode=RunMode.APPLY,
            temp_root=None,
            ledger=apply_ledger,
        ):
            for task, result in zip(plan.tasks, materialized, strict=True):
                operations.extend(
                    _handler(handlers, task).apply(
                        task,
                        result,
                        TransactionContext(repository, None, apply_ledger),
                    )
                )
        checked = validate_operations(
            _coalesce_operations(operations),
            repository_root=repository,
        )
        changed_paths = tuple(operation.path for operation in checked)

        run = update_run(
            run,
            expected_revision=run["revision"],
            status="running",
            phase="verify",
            now=now,
            completed_phases=(
                "initialize",
                "plan",
                "materialize",
                "validate",
                "apply",
            ),
        )
        if status is not None and status_updater is not None:
            status_updater(status, plan)
        return _finish(
            run,
            plan=plan,
            now=now,
            completed_phases=(
                "initialize",
                "plan",
                "materialize",
                "validate",
                "apply",
                "verify",
            ),
            changed_paths=changed_paths,
            validation_count=validation_count,
        )
    except BaseException as exc:
        failed = _failed_run(run, phase=run.get("phase", "initialize"), error=exc, now=now)
        raise TransactionError(str(exc), run_manifest=failed) from exc
