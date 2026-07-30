"""TDD contract for injected release/object clients without network access."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import StorageBackend, StorageError, StorageIntent
from scripts.rki_pipeline.storage.config import ObjectConfig, ReleaseConfig
from scripts.rki_pipeline.storage.object import ObjectStorageAdapter
from scripts.rki_pipeline.storage.release import ReleaseStorageAdapter


@dataclass
class MemoryClient:
    objects: dict[str, dict[str, object]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)

    def head(self, key: str):
        self.calls.append(("head", key))
        value = self.objects.get(key)
        return None if value is None else {name: data for name, data in value.items() if name != "payload"}

    def put(self, key: str, source_path: Path, sha256: str, size: int):
        self.calls.append(("put", key))
        self.objects[key] = {
            "sha256": sha256,
            "size": size,
            "public_reference": f"https://example.invalid/{key}",
            "artifact_id": "artifact-1",
            "visibility": "public",
            "rights_state": "approved",
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
        visibility="public",
        rights_state="approved",
    )


@pytest.mark.parametrize("backend", (StorageBackend.RELEASE, StorageBackend.OBJECT))
def test_remote_materialize_has_no_client_calls_and_stays_in_temp(tmp_path: Path, backend: StorageBackend) -> None:
    client = MemoryClient()
    adapter = (
        ReleaseStorageAdapter(ReleaseConfig("desinfect-archive", "rki/Bulletins"), client)
        if backend is StorageBackend.RELEASE
        else ObjectStorageAdapter(ObjectConfig("desinfect", "rki/Bulletins"), client)
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
        ReleaseStorageAdapter(ReleaseConfig("desinfect-archive", "rki/Bulletins"), client)
        if backend is StorageBackend.RELEASE
        else ObjectStorageAdapter(ObjectConfig("desinfect", "rki/Bulletins"), client)
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
                "visibility": "public",
                "rights_state": "approved",
                "payload": b"wrong!",
            }
        }
    )
    adapter = ObjectStorageAdapter(ObjectConfig("desinfect", "rki/Bulletins"), client)
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
