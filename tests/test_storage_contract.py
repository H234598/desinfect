"""TDD contract for backend-neutral storage types and strict TOML config."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageBackend,
    StorageConfigurationError,
    StorageIntent,
    StorageReference,
)
from scripts.rki_pipeline.storage.config import load_storage_config
from scripts.rki_pipeline.storage.factory import build_storage_adapter


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "storage.toml"
    path.write_text(text, encoding="utf-8")
    return path


def valid_config() -> str:
    return """schema_version = 1
backend = "lfs"

[lfs]
artifact_root = "rki/Bulletins"
max_run_objects = 100
max_run_bytes = 1048576
warn_total_bytes = 2097152
block_total_bytes = 4194304

[release]
tag_prefix = "desinfect-archive"
asset_prefix = "rki/Bulletins"

[object]
bucket = "desinfect"
namespace = "rki/Bulletins"
"""


def test_unknown_storage_backend_fails_closed() -> None:
    with pytest.raises(ValueError):
        StorageBackend("filesystem")


def test_storage_intent_hashes_without_writing(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    before = sorted(path.name for path in tmp_path.iterdir())
    intent = StorageIntent.from_path(
        source,
        artifact_id="artifact-1",
        logical_key="Jahre/1994/a.pdf",
        visibility="repository_authorized",
        rights_state="approved",
    )
    assert intent.sha256 == hashlib.sha256(b"payload").hexdigest()
    assert intent.size == 7
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_storage_intent_rejects_traversal_and_wrong_types(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    with pytest.raises(ValueError):
        StorageIntent.from_path(
            source,
            artifact_id="artifact-1",
            logical_key="../escape.pdf",
            visibility="repository_authorized",
            rights_state="approved",
        )
    with pytest.raises(ValueError):
        StorageIntent(
            artifact_id="artifact-1",
            logical_key="a.pdf",
            source_path=source,
            sha256="0" * 64,
            size=True,
            visibility="repository_authorized",
            rights_state="approved",
        )


def test_prepared_object_must_be_beneath_temp_root(tmp_path: Path) -> None:
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    prepared_path = temp_root / "a.pdf"
    prepared_path.write_bytes(b"x")
    prepared = PreparedObject(
        artifact_id="artifact-1",
        logical_key="a.pdf",
        path=prepared_path,
        temp_root=temp_root,
        sha256=hashlib.sha256(b"x").hexdigest(),
        size=1,
        visibility="repository_authorized",
        rights_state="approved",
    )
    assert prepared.path == prepared_path
    with pytest.raises(ValueError, match="temp_root"):
        PreparedObject(
            artifact_id="artifact-1",
            logical_key="a.pdf",
            path=tmp_path / "outside.pdf",
            temp_root=temp_root,
            sha256="0" * 64,
            size=0,
            visibility="repository_authorized",
            rights_state="approved",
        )


def test_storage_reference_is_schema_valid_and_backend_neutral() -> None:
    reference = StorageReference(
        artifact_id="artifact-1",
        relative_path="rki/Bulletins/Jahre/1994/a.pdf",
        storage_backend=StorageBackend.LFS,
        storage_object_id="sha256:" + "a" * 64,
        sha256="a" * 64,
        size=12,
        visibility="repository_authorized",
        rights_state="approved",
        public_reference=None,
    )
    payload = reference.to_dict()
    validate_document("storage-reference", payload)
    assert payload["storage_backend"] == "lfs"
    assert "repository_root" not in payload


def test_storage_config_rejects_unknown_keys_and_wrong_types(tmp_path: Path) -> None:
    path = write_config(tmp_path, valid_config() + "\nunknown = true\n")
    with pytest.raises(StorageConfigurationError, match="Unbekannte"):
        load_storage_config(path)

    wrong = valid_config().replace("max_run_objects = 100", 'max_run_objects = "100"')
    path = write_config(tmp_path, wrong)
    with pytest.raises(StorageConfigurationError, match="max_run_objects"):
        load_storage_config(path)


def test_storage_config_loads_exact_default_backend(tmp_path: Path) -> None:
    config = load_storage_config(write_config(tmp_path, valid_config()))
    assert config.backend is StorageBackend.LFS
    assert config.lfs.artifact_root == "rki/Bulletins"
    assert config.object.namespace == "rki/Bulletins"


def test_factory_requires_clients_for_remote_backends(tmp_path: Path) -> None:
    config = load_storage_config(write_config(tmp_path, valid_config()))
    with pytest.raises(StorageConfigurationError, match="ReleaseClient"):
        build_storage_adapter(
            config,
            backend=StorageBackend.RELEASE,
            repository_root=tmp_path,
        )
    with pytest.raises(StorageConfigurationError, match="ObjectClient"):
        build_storage_adapter(
            config,
            backend=StorageBackend.OBJECT,
            repository_root=tmp_path,
        )
