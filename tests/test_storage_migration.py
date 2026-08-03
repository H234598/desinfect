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

SOURCE_ID = "rki:176904/12345.2"
SOURCE_SHA256 = "b" * 64
DECISION_SHA256 = "86209a043bf3571d183ea7c65e24bcc45f5e0f4db15042773b282273c96c264a"
DOCUMENT_ID = "rki-176904-12345-v2"
CONVERSION_ID = "conv-" + "d" * 64
SECOND_SOURCE_ID = "rki:176904/54321.2"
SECOND_SOURCE_SHA256 = "e" * 64
SECOND_DOCUMENT_ID = "rki-176904-54321-v2"
SECOND_DECISION_SHA256 = "5ed23de2b80f742312bd0271c7ffe0f839e4eec0b0acdeec7be7a0c285553d34"


@dataclass
class MigrationClient:
    objects: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    upload_tokens: dict[str, str] = field(default_factory=dict)

    def head(self, key: str):
        self.calls.append(("head", key))
        value = self.objects.get(key)
        return None if value is None else {name: data for name, data in value.items() if name != "payload"}

    def put(
        self,
        key: str,
        source_path: Path,
        metadata: dict[str, object],
        rollback_token: str,
    ):
        self.calls.append(("put", key))
        payload = source_path.read_bytes()
        self.objects[key] = {
            **metadata,
            "public_reference": f"https://example.invalid/{key}",
            "payload": payload,
        }
        self.upload_tokens[key] = rollback_token
        return f"https://example.invalid/{key}"

    def rollback_put(self, key: str, rollback_token: str) -> bool:
        self.calls.append(("rollback_put", key))
        current_token = self.upload_tokens.get(key)
        if current_token is None:
            return key not in self.objects
        if current_token != rollback_token:
            return False
        del self.upload_tokens[key]
        del self.objects[key]
        return True

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
                "schema_version": "1.1.0",
                "artifact_id": "artifact-1",
                "sha256": sha256,
                "size": len(payload),
                "source_id": SOURCE_ID,
                "source_sha256": SOURCE_SHA256,
                "document_id": DOCUMENT_ID,
                "conversion_id": CONVERSION_ID,
                "decision_sha256": DECISION_SHA256,
                "provenance_state": "current",
                "visibility": "public",
                "rights_state": "approved",
                "public_reference": None,
                "payload": payload,
            }
        }
    )


def adapters(
    source_client: MigrationClient,
    target_client: MigrationClient,
    *,
    authorizer,
):
    source = ObjectStorageAdapter(
        ObjectConfig("source", "rki/Bulletins"),
        source_client,
        authorizer,
    )
    target = ReleaseStorageAdapter(
        ReleaseConfig("target", "rki/Bulletins"),
        target_client,
        authorizer,
    )
    return source, target


def materialized_copy(tmp_path: Path, storage_rights):
    source_client = seeded_client()
    target_client = MigrationClient()
    source, target = adapters(
        source_client,
        target_client,
        authorizer=storage_rights.authorizer,
    )
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


def test_migration_plan_is_deterministic_and_classifies_copy(
    tmp_path: Path,
    storage_rights,
) -> None:
    source, target = adapters(
        seeded_client(),
        MigrationClient(),
        authorizer=storage_rights.authorizer,
    )
    first = plan_migration(source, target)
    second = plan_migration(source, target)
    assert first == second
    assert first.sha256 == second.sha256
    assert [entry.artifact_id for entry in first.entries] == ["artifact-1"]
    assert first.entries[0].state is MigrationState.COPY


def test_materialize_and_apply_are_idempotent_and_never_delete_source(
    tmp_path: Path,
    storage_rights,
) -> None:
    source_client = seeded_client()
    target_client = MigrationClient()
    source, target = adapters(
        source_client,
        target_client,
        authorizer=storage_rights.authorizer,
    )
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


def test_conflicting_target_blocks_before_download_or_publish(
    tmp_path: Path,
    storage_rights,
) -> None:
    source_client = seeded_client(b"source")
    target_client = seeded_client(b"different")
    source, target = adapters(
        source_client,
        target_client,
        authorizer=storage_rights.authorizer,
    )
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


def test_migration_requires_correct_modes(tmp_path: Path, storage_rights) -> None:
    source, target = adapters(
        seeded_client(),
        MigrationClient(),
        authorizer=storage_rights.authorizer,
    )
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
        ("source_id", "rki:176904/99999.2"),
        ("source_sha256", "e" * 64),
        ("decision_sha256", "f" * 64),
        ("document_id", None),
        ("conversion_id", None),
    ),
)
def test_apply_rejects_prepared_metadata_drift_before_any_publish(
    tmp_path: Path,
    field: str,
    value: str,
    storage_rights,
) -> None:
    plan, prepared, target, target_client = materialized_copy(tmp_path, storage_rights)
    changes = {field: value}
    if field == "source_id":
        changes["document_id"] = "rki-176904-99999-v2"
    mismatched = replace(prepared, **changes)

    with pytest.raises(StorageError, match="Migrationsplan"):
        apply_migration(
            plan,
            (mismatched,),
            target,
            ledger=EffectLedger(RunMode.APPLY),
        )

    assert not any(call[0] == "put" for call in target_client.calls)


def test_apply_rejects_prepared_content_identity_drift_before_any_publish(
    tmp_path: Path,
    storage_rights,
) -> None:
    plan, prepared, target, target_client = materialized_copy(tmp_path, storage_rights)
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
        source_id=prepared.source_id,
        source_sha256=prepared.source_sha256,
        decision_sha256=prepared.decision_sha256,
        document_id=prepared.document_id,
        conversion_id=prepared.conversion_id,
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


def test_rights_provenance_drift_is_migration_conflict(storage_rights) -> None:
    source_client = seeded_client()
    target_client = seeded_client()
    key = "rki/Bulletins/Jahre/1994/archive.zip"
    target_client.objects[key]["decision_sha256"] = "e" * 64
    source, target = adapters(
        source_client,
        target_client,
        authorizer=storage_rights.authorizer,
    )

    plan = plan_migration(source, target)

    assert plan.entries[0].state is MigrationState.CONFLICT


def test_legacy_reference_is_plannable_but_export_is_blocked(
    tmp_path: Path,
    storage_rights,
) -> None:
    source_client = seeded_client()
    key = "rki/Bulletins/Jahre/1994/archive.zip"
    source_client.objects[key].update(
        source_id=None,
        source_sha256=None,
        document_id=None,
        conversion_id=None,
        decision_sha256=None,
        provenance_state="legacy_needs_review",
        rights_state="unknown",
    )
    source, target = adapters(
        source_client,
        MigrationClient(),
        authorizer=storage_rights.authorizer,
    )
    plan = plan_migration(source, target)
    assert plan.entries[0].state is MigrationState.COPY
    before_calls = tuple(source_client.calls)

    with pytest.raises(StorageError, match="legacy|Provenienz|autorisiert"):
        materialize_migration(
            plan,
            source,
            temp_root=tmp_path,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        )

    assert not any(call[0] == "get" for call in source_client.calls[len(before_calls):])


def two_seeded_client() -> MigrationClient:
    client = seeded_client(b"first")
    second = b"second"
    client.objects["rki/Bulletins/Jahre/1994/second.zip"] = {
        **{
            name: value
            for name, value in next(iter(client.objects.values())).items()
            if name != "payload"
        },
        "artifact_id": "artifact-2",
        "sha256": hashlib.sha256(second).hexdigest(),
        "size": len(second),
        "source_id": SECOND_SOURCE_ID,
        "source_sha256": SECOND_SOURCE_SHA256,
        "document_id": SECOND_DOCUMENT_ID,
        "decision_sha256": SECOND_DECISION_SHA256,
        "public_reference": None,
        "payload": second,
    }
    return client


def test_migration_preauthorizes_all_exports_before_first_download(
    tmp_path: Path,
    storage_rights,
) -> None:
    source_client = two_seeded_client()
    source, target = adapters(
        source_client,
        MigrationClient(),
        authorizer=storage_rights.authorizer,
    )
    plan = plan_migration(source, target)
    source_client.calls.clear()
    storage_rights.set_decisions(
        (SOURCE_ID, SOURCE_SHA256, "approved"),
        (SECOND_SOURCE_ID, SECOND_SOURCE_SHA256, "takedown"),
    )

    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        materialize_migration(
            plan,
            source,
            temp_root=tmp_path,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        )

    assert not any(call[0] == "get" for call in source_client.calls)


def test_migration_preauthorizes_all_applies_before_first_upload(
    tmp_path: Path,
    storage_rights,
) -> None:
    source_client = two_seeded_client()
    target_client = MigrationClient()
    source, target = adapters(
        source_client,
        target_client,
        authorizer=storage_rights.authorizer,
    )
    storage_rights.set_decisions(
        (SOURCE_ID, SOURCE_SHA256, "approved"),
        (SECOND_SOURCE_ID, SECOND_SOURCE_SHA256, "approved"),
    )
    plan = plan_migration(source, target)
    temp_root = tmp_path / "preflight"
    temp_root.mkdir()
    prepared = materialize_migration(
        plan,
        source,
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    target_client.calls.clear()
    storage_rights.set_decisions(
        (SOURCE_ID, SOURCE_SHA256, "approved"),
        (SECOND_SOURCE_ID, SECOND_SOURCE_SHA256, "takedown"),
    )

    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        apply_migration(
            plan,
            prepared,
            target,
            ledger=EffectLedger(RunMode.APPLY),
        )

    assert not any(call[0] in {"head", "put"} for call in target_client.calls)
    assert target_client.objects == {}
