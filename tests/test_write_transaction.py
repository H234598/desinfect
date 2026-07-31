"""Contracts for one validation and one atomic due-task write set."""
from __future__ import annotations

from dataclasses import dataclass, field
import subprocess
from pathlib import Path

import pytest

from scripts.rki_pipeline.dispatch_plan import DispatchPlan
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind
from scripts.rki_pipeline.run_modes import EffectKind, RunMode
from scripts.rki_pipeline.storage.base import StorageBackend
from scripts.rki_pipeline.transaction import (
    TaskPlan,
    TaskResult,
    TransactionContext,
    TransactionError,
    execute_transaction,
)
from scripts.rki_pipeline.write_policy import WriteOperation


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", root, *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def init_repo(root: Path) -> str:
    subprocess.run(["git", "init", "-q", root], check=True)
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / "status.json").write_text('{"value":0}\n', encoding="utf-8")
    git(root, "add", "status.json")
    git(root, "commit", "-qm", "seed")
    return git(root, "rev-parse", "HEAD")


def task(period: str) -> DueTask:
    return DueTask(
        task_id=f"year:{period}",
        kind=TaskKind.YEAR,
        period=period,
        reason="test",
        due_at="2026-07-31T12:00:00Z",
    )


def plan(head: str, tasks: tuple[DueTask, ...]) -> DispatchPlan:
    return DispatchPlan.create(
        created_at="2026-07-31T12:00:00Z",
        trigger="workflow_dispatch",
        base_sha=head,
        tasks=tasks,
        run_mode=RunMode.APPLY,
        storage_backend=StorageBackend.LFS,
    )


@dataclass
class Handler:
    repository: Path
    calls: list[str] = field(default_factory=list)
    fail_plan_for: str | None = None
    write: bool = True

    def plan(self, due: DueTask, context: TransactionContext) -> TaskPlan:
        self.calls.append(f"plan:{due.task_id}")
        if due.task_id == self.fail_plan_for:
            raise RuntimeError("planned failure")
        return TaskPlan(due.task_id, {"period": due.period})

    def materialize(self, value: TaskPlan, context: TransactionContext) -> TaskResult:
        self.calls.append(f"materialize:{value.task_id}")
        artifact = context.temp_root / f"{value.task_id.replace(':', '-')}.txt"
        artifact.write_text(value.task_id + "\n", encoding="utf-8")
        context.ledger.record(EffectKind.TEMP_FILE, artifact.as_posix())
        return TaskResult(value.task_id, artifact)

    def apply(self, due: DueTask, value: TaskResult, context: TransactionContext):
        self.calls.append(f"apply:{due.task_id}")
        if not self.write:
            return ()
        target = self.repository / "status.json"
        previous = target.read_text(encoding="utf-8")
        target.write_text(previous.rstrip() + f" {due.period}\n", encoding="utf-8")
        context.ledger.record(EffectKind.STATUS, "status.json")
        return (WriteOperation("status.json", change="modify", git_mode="100644", previous_git_mode="100644"),)


def test_multiple_tasks_share_one_validation_and_one_write_set(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    handler = Handler(repository)
    validations: list[tuple[str, ...]] = []
    result = execute_transaction(
        plan(head, (task("2024"), task("2025"))),
        current_head=head,
        repository_root=repository,
        temp_root=tmp_path / "temp",
        handlers={TaskKind.YEAR: handler},
        validator=lambda values: validations.append(tuple(item.task_id for item in values)),
        now="2026-07-31T12:00:00Z",
    )
    assert result.validation_count == 1
    assert validations == [("year:2024", "year:2025")]
    assert result.changed_paths == ("status.json", "status.json")
    assert result.commit_required is True
    assert handler.calls.count("apply:year:2024") == 1
    assert handler.calls.count("apply:year:2025") == 1


def test_second_plan_failure_prevents_all_materialize_and_apply(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    handler = Handler(repository, fail_plan_for="year:2025")
    with pytest.raises(TransactionError, match="planned failure") as caught:
        execute_transaction(
            plan(head, (task("2024"), task("2025"))),
            current_head=head,
            repository_root=repository,
            temp_root=tmp_path / "temp",
            handlers={TaskKind.YEAR: handler},
            validator=lambda _values: None,
            now="2026-07-31T12:00:00Z",
        )
    assert not any(call.startswith("materialize:") for call in handler.calls)
    assert not any(call.startswith("apply:") for call in handler.calls)
    assert caught.value.run_manifest["status"] == "failed"


def test_validation_failure_prevents_apply(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    handler = Handler(repository)

    def reject(_values) -> None:
        raise ValueError("global validation failed")

    with pytest.raises(TransactionError, match="global validation failed"):
        execute_transaction(
            plan(head, (task("2025"),)),
            current_head=head,
            repository_root=repository,
            temp_root=tmp_path / "temp",
            handlers={TaskKind.YEAR: handler},
            validator=reject,
            now="2026-07-31T12:00:00Z",
        )
    assert not any(call.startswith("apply:") for call in handler.calls)
    assert (repository / "status.json").read_text(encoding="utf-8") == '{"value":0}\n'


def test_all_noop_tasks_need_no_commit(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    handler = Handler(repository, write=False)
    result = execute_transaction(
        plan(head, (task("2025"),)),
        current_head=head,
        repository_root=repository,
        temp_root=tmp_path / "temp",
        handlers={TaskKind.YEAR: handler},
        validator=lambda _values: None,
        now="2026-07-31T12:00:00Z",
    )
    assert result.commit_required is False
    assert result.changed_paths == ()
    assert result.run_manifest["status"] == "no_op"


def test_status_updater_runs_only_after_apply_and_verify(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    state = {"updated": False}
    handler = Handler(repository, write=False)
    execute_transaction(
        plan(head, (task("2025"),)),
        current_head=head,
        repository_root=repository,
        temp_root=tmp_path / "temp",
        handlers={TaskKind.YEAR: handler},
        validator=lambda _values: None,
        now="2026-07-31T12:00:00Z",
        status=state,
        status_updater=lambda value, _plan: value.update(updated=True) or value,
    )
    assert state["updated"] is True


def test_base_sha_mismatch_blocks_before_handler_calls(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    head = init_repo(repository)
    handler = Handler(repository)
    with pytest.raises(TransactionError, match="Basis"):
        execute_transaction(
            plan(head, (task("2025"),)),
            current_head="b" * 40,
            repository_root=repository,
            temp_root=tmp_path / "temp",
            handlers={TaskKind.YEAR: handler},
            validator=lambda _values: None,
            now="2026-07-31T12:00:00Z",
        )
    assert handler.calls == []
