"""TDD contract for deterministic, resumable, non-destructive migrations."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
from pathlib import Path

import pytest

from scripts.rki_pipeline.run_modes import EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import PreparedObject, StorageError
from scripts.rki_pipeline.storage.config import ObjectConfig, ReleaseConfig
from scripts.rki_pipeline.storage.migrate import (
    MigrationState,
    apply_migration,
    materialize_migration,
    plan_migration,
)
from scripts.rki_pipeline.storage.object import ObjectStorageAdapter
from scripts.rki_pipeline.storage.release import ReleaseStorageAdapter


@dataclass
class MigrationClient:
    objects: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def head(self, key: str):
        self.calls.append(("head", key))
        value = self.objects.get(key)
        return None if value is None else {name: data for name, data in value.items() if name != "payload"}

    def put(self, key: str, source_path: Path, sha256: str, size: int):
        self.calls.append(("put", key))
        payload = source_path.read_bytes()
        self.objects[key] = {
            "artifact_id": "artifact-1",
            "sha256": sha256,
            "size": size,
            "visibility": "public",
            "rights_state": "approved",
            "public_reference": f"https://example.invalid/{key}",
            "payload": payload,
        }
        return f"https://example.invalid/{key}"

    def get(self, key: str, target_path: Path):
        self.calls.append(("get", key))
        target_path.write_bytes(self.objects[key]["payload"])

    def list(self, prefix: str):
        self.calls.append(("list", prefix))
        return tuple(
            {"key": key, **{name: data for name, data in metadata.items() if name != "payload"}}
            for key, metadata in sorted(self.objects.items())
            if key.startswith(prefix)
        )


def seeded_client(payload: bytes = b"archive") -> MigrationClient:
    sha256 = hashlib.sha256(payload).hexdigest()
    return MigrationClient(
        objects={
            "rki/Bulletins/Jahre/1994/archive.zip": {
                "artifact_id": "artifact-1",
                "sha256": sha256,
                "size": len(payload),
                "visibility": "public",
                "rights_state": "approved",
                "public_reference": None,
                "payload": payload,
            }
        }
    )


def adapters(source_client: MigrationClient, target_client: MigrationClient):
    source = ObjectStorageAdapter(ObjectConfig("source", "rki/Bulletins"), source_client)
    target = ReleaseStorageAdapter(ReleaseConfig("target", "rki/Bulletins"), target_client)
    return source, target


def materialized_copy(tmp_path: Path):
    source_client = seeded_client()
    target_client = MigrationClient()
    source, target = adapters(source_client, target_client)
    plan = plan_migration(source, target)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    prepared = materialize_migration(
        plan,
        source,
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    return plan, prepared[0], target, target_client


def test_migration_plan_is_deterministic_and_classifies_copy(tmp_path: Path) -> None:
    source, target = adapters(seeded_client(), MigrationClient())
    first = plan_migration(source, target)
    second = plan_migration(source, target)
    assert first == second
    assert first.sha256 == second.sha256
    assert [entry.artifact_id for entry in first.entries] == ["artifact-1"]
    assert first.entries[0].state is MigrationState.COPY


def test_materialize_and_apply_are_idempotent_and_never_delete_source(tmp_path: Path) -> None:
    source_client = seeded_client()
    target_client = MigrationClient()
    source, target = adapters(source_client, target_client)
    plan = plan_migration(source, target)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    prepared = materialize_migration(
        plan,
        source,
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    assert prepared[0].path.is_relative_to(temp_root)
    references = apply_migration(
        plan,
        prepared,
        target,
        ledger=EffectLedger(RunMode.APPLY),
    )
    assert references[0].sha256 == plan.entries[0].source.sha256
    assert source_client.objects
    assert [call[0] for call in target_client.calls].count("put") == 1

    after = plan_migration(source, target)
    assert after.entries[0].state is MigrationState.UNCHANGED
    assert materialize_migration(
        after,
        source,
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    ) == ()
    apply_migration(
        after,
        (),
        target,
        ledger=EffectLedger(RunMode.APPLY),
    )
    assert [call[0] for call in target_client.calls].count("put") == 1


def test_conflicting_target_blocks_before_download_or_publish(tmp_path: Path) -> None:
    source_client = seeded_client(b"source")
    target_client = seeded_client(b"different")
    source, target = adapters(source_client, target_client)
    plan = plan_migration(source, target)
    assert plan.entries[0].state is MigrationState.CONFLICT
    with pytest.raises(StorageError, match="Konflikt"):
        materialize_migration(
            plan,
            source,
            temp_root=tmp_path,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        )
    assert not any(call[0] == "get" for call in source_client.calls)
    assert not any(call[0] == "put" for call in target_client.calls)


def test_migration_requires_correct_modes(tmp_path: Path) -> None:
    source, target = adapters(seeded_client(), MigrationClient())
    plan = plan_migration(source, target)
    with pytest.raises(StorageError, match="materialize"):
        materialize_migration(
            plan,
            source,
            temp_root=tmp_path,
            ledger=EffectLedger(RunMode.APPLY),
        )
    with pytest.raises(StorageError, match="apply"):
        apply_migration(
            plan,
            (),
            target,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("logical_key", "rki/Bulletins/Jahre/1995/wrong.zip"),
        ("visibility", "internal"),
        ("rights_state", "unknown"),
    ),
)
def test_apply_rejects_prepared_metadata_drift_before_any_publish(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    plan, prepared, target, target_client = materialized_copy(tmp_path)
    mismatched = replace(prepared, **{field: value})

    with pytest.raises(StorageError, match="Migrationsplan"):
        apply_migration(
            plan,
            (mismatched,),
            target,
            ledger=EffectLedger(RunMode.APPLY),
        )

    assert not any(call[0] == "put" for call in target_client.calls)


def test_apply_rejects_prepared_content_identity_drift_before_any_publish(tmp_path: Path) -> None:
    plan, prepared, target, target_client = materialized_copy(tmp_path)
    rogue_path = prepared.temp_root / "rogue.zip"
    rogue_path.write_bytes(b"rogue archive")
    rogue_hash = hashlib.sha256(rogue_path.read_bytes()).hexdigest()
    mismatched = PreparedObject(
        artifact_id=prepared.artifact_id,
        logical_key=prepared.logical_key,
        path=rogue_path,
        temp_root=prepared.temp_root,
        sha256=rogue_hash,
        size=rogue_path.stat().st_size,
        visibility=prepared.visibility,
        rights_state=prepared.rights_state,
    )

    with pytest.raises(StorageError, match="Migrationsplan"):
        apply_migration(
            plan,
            (mismatched,),
            target,
            ledger=EffectLedger(RunMode.APPLY),
        )

    assert not any(call[0] == "put" for call in target_client.calls)