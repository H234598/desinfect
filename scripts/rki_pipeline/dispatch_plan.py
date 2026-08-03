#!/usr/bin/env python3
"""Versioned immutable dispatch plans with canonical JSON and SHA-256."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Iterable

from scripts.rki_pipeline.due_tasks import DueTask, TaskKind, parse_utc, task_ids
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.run_modes import RunMode
from scripts.rki_pipeline.storage.base import StorageBackend

_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_TRIGGERS = frozenset({"schedule", "workflow_dispatch", "backfill"})
_KIND_ORDER = {
    TaskKind.WEEK: 0,
    TaskKind.MONTH: 1,
    TaskKind.YEAR: 2,
    TaskKind.RECONCILIATION: 3,
}


class DispatchPlanError(ValueError):
    """A dispatch plan is malformed, non-canonical, or self-inconsistent."""


def _task_key(task: DueTask) -> tuple[int, str, str]:
    return (_KIND_ORDER[task.kind], task.period, task.task_id)


@dataclass(frozen=True, slots=True)
class DispatchPlan:
    """One complete deterministic transaction request."""

    schema_version: str
    created_at: str
    trigger: str
    base_sha: str
    tasks: tuple[DueTask, ...]
    run_mode: RunMode
    storage_backend: StorageBackend

    def __post_init__(self) -> None:
        if self.schema_version != "1.0.0":
            raise DispatchPlanError("Unbekannte Dispatchplan-Version")
        parse_utc(self.created_at)
        if self.trigger not in _TRIGGERS:
            raise DispatchPlanError(f"Unbekannter Trigger: {self.trigger}")
        if type(self.base_sha) is not str or _SHA40.fullmatch(self.base_sha) is None:
            raise DispatchPlanError("base_sha muss ein kleingeschriebener 40-hex SHA sein")
        if not isinstance(self.run_mode, RunMode):
            raise DispatchPlanError("run_mode muss ein RunMode sein")
        if not isinstance(self.storage_backend, StorageBackend):
            raise DispatchPlanError("storage_backend muss ein StorageBackend sein")
        if type(self.tasks) is not tuple or not all(isinstance(task, DueTask) for task in self.tasks):
            raise DispatchPlanError("tasks muss ein DueTask-Tupel sein")
        task_ids(self.tasks)
        if self.tasks != tuple(sorted(self.tasks, key=_task_key)):
            raise DispatchPlanError("tasks sind nicht kanonisch sortiert")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "trigger": self.trigger,
            "base_sha": self.base_sha,
            "tasks": [task.to_dict() for task in self.tasks],
            "run_mode": self.run_mode.value,
            "storage_backend": self.storage_backend.value,
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(stable_json_dumps(self.to_dict()).encode("utf-8")).hexdigest()

    def to_envelope(self) -> dict[str, object]:
        return {**self.to_dict(), "sha256": self.sha256}

    @classmethod
    def create(
        cls,
        *,
        created_at: str,
        trigger: str,
        base_sha: str,
        tasks: Iterable[DueTask],
        run_mode: RunMode,
        storage_backend: StorageBackend,
    ) -> DispatchPlan:
        materialized = tuple(sorted(tuple(tasks), key=_task_key))
        return cls(
            schema_version="1.0.0",
            created_at=created_at,
            trigger=trigger,
            base_sha=base_sha,
            tasks=materialized,
            run_mode=run_mode,
            storage_backend=storage_backend,
        )

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DispatchPlan:
        expected = {
            "schema_version",
            "created_at",
            "trigger",
            "base_sha",
            "tasks",
            "run_mode",
            "storage_backend",
            "sha256",
        }
        if not isinstance(payload, dict) or set(payload) != expected:
            raise DispatchPlanError("Dispatchplan besitzt nicht exakt die erwarteten Felder")
        for name in (
            "schema_version",
            "created_at",
            "trigger",
            "base_sha",
            "run_mode",
            "storage_backend",
            "sha256",
        ):
            if type(payload[name]) is not str:
                raise DispatchPlanError(f"{name} muss eine Zeichenkette sein")
        raw_tasks = payload["tasks"]
        if not isinstance(raw_tasks, list) or not all(isinstance(item, dict) for item in raw_tasks):
            raise DispatchPlanError("tasks muss eine Liste aus Objekten sein")
        try:
            mode = RunMode(payload["run_mode"])
            backend = StorageBackend(payload["storage_backend"])
        except ValueError as exc:
            raise DispatchPlanError("Unbekannter RunMode oder StorageBackend") from exc
        result = cls(
            schema_version=payload["schema_version"],
            created_at=payload["created_at"],
            trigger=payload["trigger"],
            base_sha=payload["base_sha"],
            tasks=tuple(DueTask.from_dict(item) for item in raw_tasks),
            run_mode=mode,
            storage_backend=backend,
        )
        if payload["sha256"] != result.sha256:
            raise DispatchPlanError("Dispatchplan-SHA-256 stimmt nicht")
        return result
