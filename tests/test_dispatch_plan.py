"""Contracts for immutable canonical dispatcher plans."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.rki_pipeline.dispatch_plan import DispatchPlan, DispatchPlanError
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind
from scripts.rki_pipeline.run_modes import RunMode
from scripts.rki_pipeline.storage.base import StorageBackend


def task(kind: TaskKind, period: str) -> DueTask:
    return DueTask(
        task_id=f"{kind.value}:{period}",
        kind=kind,
        period=period,
        reason="test",
        due_at="2026-07-31T12:00:00Z",
    )


def plan() -> DispatchPlan:
    return DispatchPlan.create(
        created_at="2026-07-31T12:00:00Z",
        trigger="schedule",
        base_sha="a" * 40,
        tasks=(task(TaskKind.MONTH, "2026-06"), task(TaskKind.WEEK, "2026-W30")),
        run_mode=RunMode.APPLY,
        storage_backend=StorageBackend.LFS,
    )


def test_plan_sorts_tasks_and_hashes_canonically() -> None:
    value = plan()
    assert [item.task_id for item in value.tasks] == ["week:2026-W30", "month:2026-06"]
    assert value.sha256 == DispatchPlan.from_dict(value.to_envelope()).sha256


def test_plan_rejects_duplicate_tasks() -> None:
    duplicate = task(TaskKind.YEAR, "2025")
    with pytest.raises(ValueError, match="Doppelte"):
        DispatchPlan.create(
            created_at="2026-07-31T12:00:00Z",
            trigger="schedule",
            base_sha="a" * 40,
            tasks=(duplicate, duplicate),
            run_mode=RunMode.APPLY,
            storage_backend=StorageBackend.LFS,
        )


def test_plan_parser_rejects_unknown_fields_and_hash_drift() -> None:
    payload = plan().to_envelope()
    unknown = deepcopy(payload)
    unknown["extra"] = True
    with pytest.raises(DispatchPlanError, match="exakt"):
        DispatchPlan.from_dict(unknown)
    tampered = deepcopy(payload)
    tampered["base_sha"] = "b" * 40
    with pytest.raises(DispatchPlanError, match="SHA-256"):
        DispatchPlan.from_dict(tampered)


@pytest.mark.parametrize(
    "field,value",
    (
        ("base_sha", "A" * 40),
        ("created_at", "2026-07-31T12:00:00+00:00"),
        ("trigger", "push"),
        ("run_mode", "preview"),
        ("storage_backend", "filesystem"),
    ),
)
def test_plan_parser_fails_closed_on_invalid_contract(field: str, value: object) -> None:
    payload = plan().to_envelope()
    payload[field] = value
    with pytest.raises(ValueError):
        DispatchPlan.from_dict(payload)
