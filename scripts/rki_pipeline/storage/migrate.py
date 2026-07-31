#!/usr/bin/env python3
"""Deterministic, resumable, non-destructive backend migration."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
from pathlib import Path

from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.run_modes import EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageAdapter,
    StorageBackend,
    StorageError,
    StorageReference,
)


class MigrationState(StrEnum):
    COPY = "copy"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class MigrationEntry:
    artifact_id: str
    state: MigrationState
    source: StorageReference
    target: StorageReference | None

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "state": self.state.value,
            "source": self.source.to_dict(),
            "target": None if self.target is None else self.target.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    source_backend: StorageBackend
    target_backend: StorageBackend
    entries: tuple[MigrationEntry, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "1.0.0",
            "source_backend": self.source_backend.value,
            "target_backend": self.target_backend.value,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @property
    def sha256(self) -> str:
        return hashlib.sha256(stable_json_dumps(self.to_dict()).encode("utf-8")).hexdigest()


def _by_artifact(references: tuple[StorageReference, ...], label: str) -> dict[str, StorageReference]:
    result: dict[str, StorageReference] = {}
    for reference in references:
        if reference.artifact_id in result:
            raise StorageError(f"Doppelte {label}-artifact_id: {reference.artifact_id}")
        result[reference.artifact_id] = reference
    return result


def plan_migration(source: StorageAdapter, target: StorageAdapter) -> MigrationPlan:
    """Compare immutable references without downloading or publishing objects."""

    source_map = _by_artifact(source.list_references(), "Quell")
    target_map = _by_artifact(target.list_references(), "Ziel")
    entries: list[MigrationEntry] = []
    for artifact_id, source_reference in sorted(source_map.items()):
        target_reference = target_map.get(artifact_id)
        if target_reference is None:
            state = MigrationState.COPY
        elif (
            target_reference.sha256,
            target_reference.size,
        ) == (
            source_reference.sha256,
            source_reference.size,
        ):
            state = MigrationState.UNCHANGED
        else:
            state = MigrationState.CONFLICT
        entries.append(
            MigrationEntry(
                artifact_id=artifact_id,
                state=state,
                source=source_reference,
                target=target_reference,
            )
        )
    return MigrationPlan(source.backend, target.backend, tuple(entries))


def _reject_conflicts(plan: MigrationPlan) -> None:
    conflicts = [entry.artifact_id for entry in plan.entries if entry.state is MigrationState.CONFLICT]
    if conflicts:
        raise StorageError("Migrations-Konflikt: " + ", ".join(conflicts))


def materialize_migration(
    plan: MigrationPlan,
    source: StorageAdapter,
    *,
    temp_root: Path,
    ledger: EffectLedger,
) -> tuple[PreparedObject, ...]:
    """Export only copy entries below temp_root; never mutate source or target."""

    if ledger.mode is not RunMode.MATERIALIZE:
        raise StorageError("Migrationsmaterialisierung benötigt RunMode materialize")
    if source.backend is not plan.source_backend:
        raise StorageError("Quelladapter stimmt nicht mit dem Migrationsplan überein")
    _reject_conflicts(plan)
    prepared = [
        source.export(entry.source, temp_root=temp_root, ledger=ledger)
        for entry in plan.entries
        if entry.state is MigrationState.COPY
    ]
    return tuple(sorted(prepared, key=lambda item: item.artifact_id))


def _validate_prepared_objects(
    plan: MigrationPlan,
    prepared_map: dict[str, PreparedObject],
) -> None:
    """Bind every prepared object to its immutable plan source before writes."""

    for entry in plan.entries:
        if entry.state is not MigrationState.COPY:
            continue
        prepared = prepared_map[entry.artifact_id]
        expected = entry.source
        actual_contract = (
            prepared.logical_key,
            prepared.sha256,
            prepared.size,
            prepared.visibility,
            prepared.rights_state,
        )
        expected_contract = (
            expected.relative_path,
            expected.sha256,
            expected.size,
            expected.visibility,
            expected.rights_state,
        )
        if actual_contract != expected_contract:
            raise StorageError(
                "Vorbereitetes Objekt weicht vom Migrationsplan ab: "
                f"{entry.artifact_id}"
            )


def apply_migration(
    plan: MigrationPlan,
    prepared: tuple[PreparedObject, ...],
    target: StorageAdapter,
    *,
    ledger: EffectLedger,
) -> tuple[StorageReference, ...]:
    """Publish copy entries, retain unchanged entries, and verify every target."""

    if ledger.mode is not RunMode.APPLY:
        raise StorageError("Migrationspublikation benötigt RunMode apply")
    if target.backend is not plan.target_backend:
        raise StorageError("Zieladapter stimmt nicht mit dem Migrationsplan überein")
    _reject_conflicts(plan)
    prepared_map = {item.artifact_id: item for item in prepared}
    if len(prepared_map) != len(prepared):
        raise StorageError("Doppelte vorbereitete artifact_id")
    expected_copy = {
        entry.artifact_id
        for entry in plan.entries
        if entry.state is MigrationState.COPY
    }
    if set(prepared_map) != expected_copy:
        raise StorageError("Vorbereitete Objekte stimmen nicht mit Copy-Einträgen überein")
    _validate_prepared_objects(plan, prepared_map)

    references: list[StorageReference] = []
    for entry in plan.entries:
        if entry.state is MigrationState.UNCHANGED:
            if entry.target is None:
                raise StorageError("Unchanged-Eintrag besitzt keine Zielreferenz")
            target.verify(entry.target)
            references.append(entry.target)
        elif entry.state is MigrationState.COPY:
            reference = target.apply(prepared_map[entry.artifact_id], ledger=ledger)
            target.verify(reference)
            references.append(reference)
    return tuple(sorted(references, key=lambda reference: reference.artifact_id))