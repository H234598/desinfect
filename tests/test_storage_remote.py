"""TDD contract for injected release/object clients without network access."""
from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path

import pytest

from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import StorageBackend, StorageError, StorageIntent, StorageReference
from scripts.rki_pipeline.storage.config import ObjectConfig, ReleaseConfig
from scripts.rki_pipeline.storage.object import ObjectStorageAdapter
from scripts.rki_pipeline.storage.release import ReleaseStorageAdapter
from scripts.rki_pipeline.storage import remote as remote_storage
from scripts.rki_pipeline.storage.remote import RemotePutReceipt, RemoteStorageAdapter

SOURCE_ID = "rki:176904/12345.2"
SOURCE_SHA256 = "b" * 64
DECISION_SHA256 = "86209a043bf3571d183ea7c65e24bcc45f5e0f4db15042773b282273c96c264a"
DOCUMENT_ID = "rki-176904-12345-v2"
CONVERSION_ID = "conv-" + "d" * 64


@dataclass
class MemoryClient:
    objects: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    get_failure: str | None = None
    head_hook: Callable[[], None] | None = None
    get_hook: Callable[[], None] | None = None
    put_hook: Callable[[], None] | None = None
    list_result: tuple[dict[str, object], ...] | None = None
    upload_tokens: dict[str, str] = field(default_factory=dict)
    upload_sequence: int = 0

    def head(self, key: str):
        self.calls.append(("head", key))
        if self.head_hook is not None:
            self.head_hook()
        value = self.objects.get(key)
        return None if value is None else {name: data for name, data in value.items() if name != "payload"}

    def put(self, key: str, source_path: Path, metadata: dict[str, object]):
        self.calls.append(("put", key))
        self.upload_sequence += 1
        rollback_token = f"upload-{self.upload_sequence}"
        self.objects[key] = {
            **metadata,
            "public_reference": f"https://example.invalid/{key}",
            "payload": source_path.read_bytes(),
        }
        self.upload_tokens[key] = rollback_token
        if self.put_hook is not None:
            self.put_hook()
        return RemotePutReceipt(f"https://example.invalid/{key}", rollback_token)

    def rollback_put(self, key: str, rollback_token: str) -> bool:
        self.calls.append(("rollback_put", key))
        if self.upload_tokens.get(key) != rollback_token:
            return False
        del self.upload_tokens[key]
        del self.objects[key]
        return True

    def get(self, key: str, target_path: Path):
        self.calls.append(("get", key))
        if self.get_hook is not None:
            self.get_hook()
        if self.get_failure == "raise":
            target_path.write_bytes(b"partial")
            raise RuntimeError("injected get failure")
        if self.get_failure == "corrupt":
            target_path.write_bytes(b"corrupt")
            return
        target_path.write_bytes(self.objects[key]["payload"])

    def list(self, prefix: str):
        self.calls.append(("list", prefix))
        if self.list_result is not None:
            return self.list_result
        return tuple(
            {"key": key, **{name: data for name, data in metadata.items() if name != "payload"}}
            for key, metadata in sorted(self.objects.items())
            if key.startswith(prefix)
        )


def intent(tmp_path: Path) -> StorageIntent:
    source = tmp_path / "source.zip"
    source.write_bytes(b"archive")
    return StorageIntent.from_path(
        source,
        artifact_id="artifact-1",
        logical_key="Jahre/1994/archive.zip",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=DECISION_SHA256,
        document_id=DOCUMENT_ID,
        conversion_id=CONVERSION_ID,
        visibility="public",
        rights_state="approved",
    )


def object_adapter(client: MemoryClient, storage_rights) -> ObjectStorageAdapter:
    return ObjectStorageAdapter(
        ObjectConfig("desinfect", "rki/Bulletins"),
        client,
        storage_rights.authorizer,
    )


def tree_snapshot(root: Path) -> tuple[tuple[str, bool, bytes], ...]:
    return tuple(
        (
            path.relative_to(root).as_posix(),
            path.is_dir(),
            b"" if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(root.rglob("*"))
    )


@pytest.mark.parametrize("backend", (StorageBackend.RELEASE, StorageBackend.OBJECT))
def test_remote_materialize_has_no_client_calls_and_stays_in_temp(
    tmp_path: Path,
    backend: StorageBackend,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = (
        ReleaseStorageAdapter(
            ReleaseConfig("desinfect-archive", "rki/Bulletins"),
            client,
            storage_rights.authorizer,
        )
        if backend is StorageBackend.RELEASE
        else object_adapter(client, storage_rights)
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    prepared = adapter.materialize(intent(tmp_path), temp_root=temp_root, ledger=ledger)
    assert prepared.path.is_relative_to(temp_root)
    assert client.calls == []
    assert ledger.events[-1].kind is EffectKind.TEMP_FILE


@pytest.mark.parametrize("backend,effect", ((StorageBackend.RELEASE, EffectKind.RELEASE), (StorageBackend.OBJECT, EffectKind.OBJECT)))
def test_remote_apply_is_idempotent_and_returns_backend_neutral_reference(
    tmp_path: Path,
    backend: StorageBackend,
    effect: EffectKind,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = (
        ReleaseStorageAdapter(
            ReleaseConfig("desinfect-archive", "rki/Bulletins"),
            client,
            storage_rights.authorizer,
        )
        if backend is StorageBackend.RELEASE
        else object_adapter(client, storage_rights)
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    ledger = EffectLedger(RunMode.APPLY)
    first = adapter.apply(prepared, ledger=ledger)
    second = adapter.apply(prepared, ledger=ledger)
    assert first == second
    assert [call[0] for call in client.calls].count("put") == 1
    assert first.storage_backend is backend
    assert first.relative_path == "rki/Bulletins/Jahre/1994/archive.zip"
    assert effect in {event.kind for event in ledger.events}
    adapter.verify(first)


def test_remote_checksum_conflict_fails_closed(tmp_path: Path, storage_rights) -> None:
    client = MemoryClient(
        objects={
            "rki/Bulletins/Jahre/1994/archive.zip": {
                "sha256": "f" * 64,
                "size": 7,
                "public_reference": None,
                "artifact_id": "artifact-1",
                "schema_version": "1.1.0",
                "source_id": SOURCE_ID,
                "source_sha256": SOURCE_SHA256,
                "document_id": DOCUMENT_ID,
                "conversion_id": CONVERSION_ID,
                "decision_sha256": DECISION_SHA256,
                "provenance_state": "current",
                "visibility": "public",
                "rights_state": "approved",
                "payload": b"wrong!",
            }
        }
    )
    adapter = object_adapter(client, storage_rights)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    with pytest.raises(StorageError, match="Konflikt"):
        adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    assert not any(call[0] == "put" for call in client.calls)


def test_invalid_remote_client_raises_storage_error(storage_rights) -> None:
    class InvalidClient:
        pass

    with pytest.raises(StorageError, match="RemoteClient"):
        ObjectStorageAdapter(
            ObjectConfig("desinfect", "rki/Bulletins"),
            InvalidClient(),
            storage_rights.authorizer,
        )


def test_remote_apply_rehashes_prepared_path_before_upload(
    tmp_path: Path,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    prepared.path.write_bytes(b"tampered after preparation")

    with pytest.raises(StorageError, match="Größe|SHA-256|vorbereitet"):
        adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))

    assert not any(call[0] == "put" for call in client.calls)


def test_remote_verify_reads_actual_payload_instead_of_trusting_metadata(
    tmp_path: Path,
    storage_rights,
) -> None:
    key = "rki/Bulletins/Jahre/1994/archive.zip"
    expected = b"archive"
    client = MemoryClient(
        objects={
            key: {
                "sha256": hashlib.sha256(expected).hexdigest(),
                "size": len(expected),
                "public_reference": None,
                "artifact_id": "artifact-1",
                "schema_version": "1.1.0",
                "source_id": SOURCE_ID,
                "source_sha256": SOURCE_SHA256,
                "document_id": DOCUMENT_ID,
                "conversion_id": CONVERSION_ID,
                "decision_sha256": DECISION_SHA256,
                "provenance_state": "current",
                "visibility": "public",
                "rights_state": "approved",
                "payload": b"corrupt",
            }
        }
    )
    adapter = object_adapter(client, storage_rights)
    reference = StorageReference(
        artifact_id="artifact-1",
        relative_path=key,
        storage_backend=StorageBackend.OBJECT,
        storage_object_id=f"object:desinfect:{key}",
        sha256=hashlib.sha256(expected).hexdigest(),
        size=len(expected),
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        document_id=DOCUMENT_ID,
        conversion_id=CONVERSION_ID,
        decision_sha256=DECISION_SHA256,
        provenance_state="current",
        visibility="public",
        rights_state="approved",
        public_reference=None,
    )

    with pytest.raises(StorageError, match="[Ii]ntegrität|SHA-256|Größe"):
        adapter.verify(reference)

    assert ("get", key) in client.calls


def test_remote_authorization_precedes_temp_get_put_and_verify_reads(
    tmp_path: Path,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = ObjectStorageAdapter(
        ObjectConfig("desinfect", "rki/Bulletins"),
        client,
        storage_rights.authorizer,
    )
    source_intent = intent(tmp_path)
    temp_root = tmp_path / "denied-temp"
    temp_root.mkdir()
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))
    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.materialize(source_intent, temp_root=temp_root, ledger=ledger)

    assert tuple(temp_root.rglob("*")) == ()
    assert ledger.events == []
    assert client.calls == []

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "approved"))
    prepared = adapter.materialize(
        source_intent,
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    apply_ledger = EffectLedger(RunMode.APPLY)

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))
    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.apply(prepared, ledger=apply_ledger)

    assert not client.objects
    assert not any(call[0] == "put" for call in client.calls)
    assert apply_ledger.events == []

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "approved"))
    reference = adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    client.calls.clear()
    export_root = tmp_path / "denied-export"
    export_root.mkdir()

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))
    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.export(
            reference,
            temp_root=export_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=export_root),
        )
    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.verify(reference)
    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))

    assert client.calls == []
    assert tuple(export_root.rglob("*")) == ()


def test_remote_metadata_requires_full_versioned_provenance_without_fallback(
    tmp_path: Path,
    storage_rights,
) -> None:
    source_intent = intent(tmp_path)
    key = "rki/Bulletins/Jahre/1994/archive.zip"
    metadata = {
        "schema_version": "1.1.0",
        "artifact_id": source_intent.artifact_id,
        "sha256": source_intent.sha256,
        "size": source_intent.size,
        "source_id": source_intent.source_id,
        "source_sha256": source_intent.source_sha256,
        "document_id": source_intent.document_id,
        "conversion_id": source_intent.conversion_id,
        "decision_sha256": source_intent.decision_sha256,
        "provenance_state": "current",
        "visibility": source_intent.visibility,
        "rights_state": source_intent.rights_state,
        "public_reference": None,
        "payload": b"archive",
    }
    for missing in (
        "schema_version",
        "source_id",
        "source_sha256",
        "document_id",
        "conversion_id",
        "decision_sha256",
        "provenance_state",
        "visibility",
        "rights_state",
    ):
        incomplete = dict(metadata)
        del incomplete[missing]
        client = MemoryClient(objects={key: incomplete})

        with pytest.raises(StorageError, match="Metadaten"):
            object_adapter(client, storage_rights).exists(source_intent)


def test_remote_verify_rejects_metadata_provenance_drift_before_get(
    tmp_path: Path,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    reference = adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    client.calls.clear()
    client.objects[reference.relative_path]["decision_sha256"] = "e" * 64

    with pytest.raises(StorageError, match="Metadaten|drift|Integrität"):
        adapter.verify(reference)

    assert not any(call[0] == "get" for call in client.calls)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_id", "rki:176904/99999.2"),
        ("source_sha256", "e" * 64),
        ("document_id", None),
        ("conversion_id", None),
        ("decision_sha256", "e" * 64),
        ("provenance_state", "legacy_needs_review"),
        ("visibility", "internal"),
        ("rights_state", "internal_only"),
    ),
)
def test_remote_head_provenance_drift_never_uses_intent_fallback(
    tmp_path: Path,
    field: str,
    value: object,
    storage_rights,
) -> None:
    source_intent = intent(tmp_path)
    key = "rki/Bulletins/Jahre/1994/archive.zip"
    metadata = {
        "schema_version": "1.1.0",
        "artifact_id": source_intent.artifact_id,
        "sha256": source_intent.sha256,
        "size": source_intent.size,
        "source_id": source_intent.source_id,
        "source_sha256": source_intent.source_sha256,
        "document_id": source_intent.document_id,
        "conversion_id": source_intent.conversion_id,
        "decision_sha256": source_intent.decision_sha256,
        "provenance_state": "current",
        "visibility": source_intent.visibility,
        "rights_state": source_intent.rights_state,
        "public_reference": None,
        "payload": b"archive",
    }
    metadata[field] = value

    with pytest.raises(StorageError, match="Metadaten|Konflikt"):
        object_adapter(
            MemoryClient(objects={key: metadata}),
            storage_rights,
        ).exists(source_intent)


def test_remote_put_persists_complete_current_metadata(
    tmp_path: Path,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    temp_root = tmp_path / "temp-metadata"
    temp_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )

    reference = adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    stored = {
        name: value
        for name, value in client.objects[reference.relative_path].items()
        if name != "payload"
    }

    assert set(stored) == {
        "schema_version",
        "artifact_id",
        "sha256",
        "size",
        "source_id",
        "source_sha256",
        "document_id",
        "conversion_id",
        "decision_sha256",
        "provenance_state",
        "visibility",
        "rights_state",
        "public_reference",
    }
    assert stored["schema_version"] == "1.1.0"
    assert stored["decision_sha256"] == DECISION_SHA256


def test_remote_constructors_reject_structural_authorizer(
    storage_rights,
) -> None:
    class ArbitraryAuthorizer:
        def authorize(self, subject: object, *, operation: str) -> None:
            pass

    class DerivedAuthorizer(type(storage_rights.authorizer)):
        pass

    client = MemoryClient()
    invalid_authorizers = (
        ArbitraryAuthorizer(),
        DerivedAuthorizer(
            authority=storage_rights.authorizer.authority,
            policy=storage_rights.authorizer.policy,
        ),
    )
    for authorizer in invalid_authorizers:
        constructors = (
            lambda: RemoteStorageAdapter(
                client=client,
                prefix="rki/Bulletins",
                object_prefix="remote:test",
                authorizer=authorizer,
            ),
            lambda: ReleaseStorageAdapter(
                ReleaseConfig("desinfect-archive", "rki/Bulletins"),
                client,
                authorizer,
            ),
            lambda: ObjectStorageAdapter(
                ObjectConfig("desinfect", "rki/Bulletins"),
                client,
                authorizer,
            ),
        )
        for construct in constructors:
            with pytest.raises(StorageError, match="RightsStorageAuthorizer"):
                construct()

    assert object_adapter(client, storage_rights).authorizer is storage_rights.authorizer


def test_remote_exists_authorizes_before_head(tmp_path: Path, storage_rights) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    source_intent = intent(tmp_path)
    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))

    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.exists(source_intent)

    assert client.calls == []


@pytest.mark.parametrize("replacement", (b"changed", None))
def test_remote_materialize_validates_one_source_snapshot_before_temp_write(
    tmp_path: Path,
    storage_rights,
    replacement: bytes | None,
) -> None:
    adapter = object_adapter(MemoryClient(), storage_rights)
    source_intent = intent(tmp_path)
    if replacement is None:
        source_intent.source_path.unlink()
        replacement_path = tmp_path / "replacement.zip"
        replacement_path.write_bytes(b"archive")
        source_intent.source_path.symlink_to(replacement_path)
    else:
        source_intent.source_path.write_bytes(replacement)
    temp_root = tmp_path / "snapshot-temp"
    temp_root.mkdir()
    (temp_root / "sentinel").write_bytes(b"keep")
    before = tree_snapshot(temp_root)
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)

    with pytest.raises(StorageError, match="Größe|SHA-256|reguläre Datei"):
        adapter.materialize(source_intent, temp_root=temp_root, ledger=ledger)

    assert tree_snapshot(temp_root) == before
    assert ledger.events == []


@pytest.mark.parametrize("failure", ("raise", "corrupt"))
def test_remote_export_cleans_only_its_unique_partial_file(
    tmp_path: Path,
    storage_rights,
    failure: str,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    materialize_root = tmp_path / "materialize"
    materialize_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=materialize_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=materialize_root),
    )
    reference = adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    client.calls.clear()
    client.get_failure = failure
    export_root = tmp_path / "export"
    target = export_root / reference.relative_path
    target.parent.mkdir(parents=True)
    preexisting_part = target.with_name(f".{target.name}.export.part")
    preexisting_part.write_bytes(b"caller-owned")
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=export_root)

    with pytest.raises((RuntimeError, StorageError)):
        adapter.export(reference, temp_root=export_root, ledger=ledger)

    assert preexisting_part.read_bytes() == b"caller-owned"
    assert not target.exists()
    assert [
        path
        for path in target.parent.iterdir()
        if path != preexisting_part and path.name.endswith(".part")
    ] == []
    assert ledger.events == []


@pytest.mark.parametrize(
    "operation",
    (
        "materialize",
        "export-before-get",
        "export-before-write",
        "apply",
        "verify",
        "apply-idempotent",
    ),
)
def test_remote_intra_call_revocation_blocks_next_payload_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_rights,
    operation: str,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    source_intent = intent(tmp_path)
    prepared = None
    reference = None
    if operation != "materialize":
        prepared_root = tmp_path / "prepared"
        prepared_root.mkdir()
        prepared = adapter.materialize(
            source_intent,
            temp_root=prepared_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=prepared_root),
        )
    if operation in {
        "export-before-get",
        "export-before-write",
        "verify",
        "apply-idempotent",
    }:
        reference = adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    client.calls.clear()

    destination = tmp_path / f"revoked-{operation}"
    destination.mkdir()
    (destination / "sentinel").write_bytes(b"keep")
    mode = RunMode.APPLY if operation.startswith("apply") else RunMode.MATERIALIZE
    ledger = EffectLedger(mode, temp_root=destination if mode is RunMode.MATERIALIZE else None)
    objects_before = deepcopy(client.objects)
    destination_before = tree_snapshot(destination)

    def revoke() -> None:
        storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))

    if operation == "materialize":
        original = remote_storage.read_verified_payload

        def read_then_revoke(*args, **kwargs):
            payload = original(*args, **kwargs)
            revoke()
            return payload

        monkeypatch.setattr(remote_storage, "read_verified_payload", read_then_revoke)
        action = lambda: adapter.materialize(
            source_intent,
            temp_root=destination,
            ledger=ledger,
        )
    elif operation == "export-before-get":
        original = remote_storage.NamedTemporaryFile

        def create_part_then_revoke(*args, **kwargs):
            temporary = original(*args, **kwargs)
            revoke()
            return temporary

        monkeypatch.setattr(
            remote_storage,
            "NamedTemporaryFile",
            create_part_then_revoke,
        )
        action = lambda: adapter.export(
            reference,
            temp_root=destination,
            ledger=ledger,
        )
    elif operation == "export-before-write":
        client.get_hook = revoke
        action = lambda: adapter.export(
            reference,
            temp_root=destination,
            ledger=ledger,
        )
    elif operation == "apply":
        client.head_hook = revoke
        action = lambda: adapter.apply(prepared, ledger=ledger)
    elif operation == "verify":
        client.head_hook = revoke
        action = lambda: adapter.verify(reference)
    else:
        head_calls = 0

        def revoke_on_verify_head() -> None:
            nonlocal head_calls
            head_calls += 1
            if head_calls == 2:
                revoke()

        client.head_hook = revoke_on_verify_head
        action = lambda: adapter.apply(prepared, ledger=ledger)

    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        action()

    assert client.objects == objects_before
    assert tree_snapshot(destination) == destination_before
    assert ledger.events == []


def test_remote_apply_rolls_back_its_upload_when_rights_are_revoked_during_put(
    tmp_path: Path,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    prepared_root = tmp_path / "prepared-put-revocation"
    prepared_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=prepared_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=prepared_root),
    )
    ledger = EffectLedger(RunMode.APPLY)
    client.put_hook = lambda: storage_rights.set_decisions(
        (SOURCE_ID, SOURCE_SHA256, "takedown")
    )

    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.apply(prepared, ledger=ledger)

    assert client.objects == {}
    assert ledger.events == []


def test_remote_apply_never_removes_a_concurrent_replacement_during_rollback(
    tmp_path: Path,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    prepared_root = tmp_path / "prepared-put-replacement"
    prepared_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=prepared_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=prepared_root),
    )
    ledger = EffectLedger(RunMode.APPLY)
    key = "rki/Bulletins/Jahre/1994/archive.zip"
    replacement = {"owner": "concurrent-writer", "payload": b"foreign"}

    def replace_and_revoke() -> None:
        client.objects[key] = replacement
        client.upload_tokens[key] = "concurrent-upload"
        storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "takedown"))

    client.put_hook = replace_and_revoke

    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.apply(prepared, ledger=ledger)

    assert client.objects[key] is replacement
    assert ledger.events == []


def test_remote_apply_reauthorizes_before_prepared_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_rights,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    prepared_root = tmp_path / "prepared-snapshot"
    prepared_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=prepared_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=prepared_root),
    )
    ledger = EffectLedger(RunMode.APPLY)
    before = tree_snapshot(prepared_root)
    original = RemoteStorageAdapter._snapshot_prepared

    def mark_snapshot_entry(subject, directory):
        (prepared_root / "snapshot-entered").write_bytes(b"unexpected read")
        return original(subject, directory)

    monkeypatch.setattr(
        RemoteStorageAdapter,
        "_snapshot_prepared",
        staticmethod(mark_snapshot_entry),
    )
    client.head_hook = lambda: storage_rights.set_decisions(
        (SOURCE_ID, SOURCE_SHA256, "takedown")
    )

    with pytest.raises(StorageError, match="Rechte|autorisiert"):
        adapter.apply(prepared, ledger=ledger)

    assert tree_snapshot(prepared_root) == before
    assert client.objects == {}
    assert ledger.events == []


@pytest.mark.parametrize(
    ("listed_key", "expected_key"),
    (
        ("rki/Bulletins", None),
        ("rki/Bulletins-escape/archive.zip", None),
        ("other/archive.zip", None),
        ("rki/Bulletins/../archive.zip", None),
        (
            "rki/Bulletins/Jahre/1994/Cafe\u0301.zip",
            None,
        ),
        (
            "rki/Bulletins/Jahre/1994/Caf\u00e9.zip",
            "rki/Bulletins/Jahre/1994/Caf\u00e9.zip",
        ),
    ),
)
def test_remote_list_normalizes_strictly_confined_keys(
    tmp_path: Path,
    storage_rights,
    listed_key: str,
    expected_key: str | None,
) -> None:
    client = MemoryClient()
    adapter = object_adapter(client, storage_rights)
    prepared_root = tmp_path / "prepared-list"
    prepared_root.mkdir()
    prepared = adapter.materialize(
        intent(tmp_path),
        temp_root=prepared_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=prepared_root),
    )
    reference = adapter.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    stored = client.objects[reference.relative_path]
    client.list_result = (
        {
            "key": listed_key,
            **{name: value for name, value in stored.items() if name != "payload"},
        },
    )
    client.calls.clear()

    if expected_key is None:
        with pytest.raises(StorageError, match="Pfad|Prefix|Schl.ssel|kanonisch"):
            adapter.list_references()
    else:
        listed = adapter.list_references()
        assert listed[0].relative_path == expected_key
        assert listed[0].storage_object_id.endswith(f":{expected_key}")

    assert client.calls == [("list", "rki/Bulletins")]
