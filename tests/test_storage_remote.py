"""TDD contract for injected release/object clients without network access."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from typing import Any

import pytest

from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import StorageBackend, StorageError, StorageIntent, StorageReference
from scripts.rki_pipeline.storage.config import ObjectConfig, ReleaseConfig
from scripts.rki_pipeline.storage.object import ObjectStorageAdapter
from scripts.rki_pipeline.storage.release import ReleaseStorageAdapter

SOURCE_ID = "rki:176904/12345.2"
SOURCE_SHA256 = "b" * 64
DECISION_SHA256 = "c" * 64
DOCUMENT_ID = "rki-176904-12345-v2"
CONVERSION_ID = "conv-" + "d" * 64


class AllowAuthorizer:
    def authorize(self, subject: Any, *, operation: str) -> None:
        pass


class DenyAuthorizer:
    def authorize(self, subject: Any, *, operation: str) -> None:
        raise StorageError(f"nicht autorisiert: {operation}")


@dataclass
class MemoryClient:
    objects: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def head(self, key: str):
        self.calls.append(("head", key))
        value = self.objects.get(key)
        return None if value is None else {name: data for name, data in value.items() if name != "payload"}

    def put(self, key: str, source_path: Path, metadata: dict[str, object]):
        self.calls.append(("put", key))
        self.objects[key] = {
            **metadata,
            "public_reference": f"https://example.invalid/{key}",
            "payload": source_path.read_bytes(),
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


def object_adapter(client: MemoryClient) -> ObjectStorageAdapter:
    return ObjectStorageAdapter(
        ObjectConfig("desinfect", "rki/Bulletins"),
        client,
        AllowAuthorizer(),
    )


@pytest.mark.parametrize("backend", (StorageBackend.RELEASE, StorageBackend.OBJECT))
def test_remote_materialize_has_no_client_calls_and_stays_in_temp(tmp_path: Path, backend: StorageBackend) -> None:
    client = MemoryClient()
    adapter = (
        ReleaseStorageAdapter(
            ReleaseConfig("desinfect-archive", "rki/Bulletins"),
            client,
            AllowAuthorizer(),
        )
        if backend is StorageBackend.RELEASE
        else object_adapter(client)
    )
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    prepared = adapter.materialize(intent(tmp_path), temp_root=temp_root, ledger=ledger)
    assert prepared.path.is_relative_to(temp_root)
    assert client.calls == []
    assert ledger.events[-1].kind is EffectKind.TEMP_FILE


@pytest.mark.parametrize("backend,effect", ((StorageBackend.RELEASE, EffectKind.RELEASE), (StorageBackend.OBJECT, EffectKind.OBJECT)))
def test_remote_apply_is_idempotent_and_returns_backend_neutral_reference(tmp_path: Path, backend: StorageBackend, effect: EffectKind) -> None:
    client = MemoryClient()
    adapter = (
        ReleaseStorageAdapter(
            ReleaseConfig("desinfect-archive", "rki/Bulletins"),
            client,
            AllowAuthorizer(),
        )
        if backend is StorageBackend.RELEASE
        else object_adapter(client)
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


def test_remote_checksum_conflict_fails_closed(tmp_path: Path) -> None:
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
    adapter = object_adapter(client)
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


def test_invalid_remote_client_raises_storage_error() -> None:
    class InvalidClient:
        pass

    with pytest.raises(StorageError, match="RemoteClient"):
        ObjectStorageAdapter(
            ObjectConfig("desinfect", "rki/Bulletins"),
            InvalidClient(),
            AllowAuthorizer(),
        )


def test_remote_apply_rehashes_prepared_path_before_upload(tmp_path: Path) -> None:
    client = MemoryClient()
    adapter = object_adapter(client)
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


def test_remote_verify_reads_actual_payload_instead_of_trusting_metadata(tmp_path: Path) -> None:
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
    adapter = object_adapter(client)
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


def test_remote_authorization_precedes_temp_get_put_and_verify_reads(tmp_path: Path) -> None:
    client = MemoryClient()
    denied = ObjectStorageAdapter(
        ObjectConfig("desinfect", "rki/Bulletins"),
        client,
        DenyAuthorizer(),
    )
    source_intent = intent(tmp_path)
    temp_root = tmp_path / "denied-temp"
    temp_root.mkdir()
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)

    with pytest.raises(StorageError, match="nicht autorisiert"):
        denied.materialize(source_intent, temp_root=temp_root, ledger=ledger)

    assert tuple(temp_root.rglob("*")) == ()
    assert ledger.events == []
    assert client.calls == []

    allowed = object_adapter(client)
    prepared = allowed.materialize(
        source_intent,
        temp_root=temp_root,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
    )
    apply_ledger = EffectLedger(RunMode.APPLY)

    with pytest.raises(StorageError, match="nicht autorisiert"):
        denied.apply(prepared, ledger=apply_ledger)

    assert not client.objects
    assert not any(call[0] == "put" for call in client.calls)
    assert apply_ledger.events == []

    reference = allowed.apply(prepared, ledger=EffectLedger(RunMode.APPLY))
    client.calls.clear()
    export_root = tmp_path / "denied-export"
    export_root.mkdir()

    with pytest.raises(StorageError, match="nicht autorisiert"):
        denied.export(
            reference,
            temp_root=export_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=export_root),
        )
    with pytest.raises(StorageError, match="nicht autorisiert"):
        denied.verify(reference)
    with pytest.raises(StorageError, match="nicht autorisiert"):
        denied.apply(prepared, ledger=EffectLedger(RunMode.APPLY))

    assert client.calls == []
    assert tuple(export_root.rglob("*")) == ()


def test_remote_metadata_requires_full_versioned_provenance_without_fallback(
    tmp_path: Path,
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
            object_adapter(client).exists(source_intent)


def test_remote_verify_rejects_metadata_provenance_drift_before_get(tmp_path: Path) -> None:
    client = MemoryClient()
    adapter = object_adapter(client)
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
        object_adapter(MemoryClient(objects={key: metadata})).exists(source_intent)


def test_remote_put_persists_complete_current_metadata(tmp_path: Path) -> None:
    client = MemoryClient()
    adapter = object_adapter(client)
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
