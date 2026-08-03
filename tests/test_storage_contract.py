"""TDD contract for backend-neutral storage types and strict TOML config."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.rki_pipeline import rights
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.storage import base as storage_base
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageBackend,
    StorageConfigurationError,
    StorageError,
    StorageIntent,
    StorageReference,
)
from scripts.rki_pipeline.storage.config import load_storage_config
from scripts.rki_pipeline.storage.factory import build_storage_adapter

SOURCE_ID = "rki:176904/12345.2"
SOURCE_SHA256 = "b" * 64
DECISION_SHA256 = "c" * 64
DOCUMENT_ID = "rki-176904-12345-v2"
CONVERSION_ID = "conv-" + "d" * 64


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
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=DECISION_SHA256,
        document_id=DOCUMENT_ID,
        conversion_id=CONVERSION_ID,
        visibility="repository_authorized",
        rights_state="approved",
    )
    assert intent.sha256 == hashlib.sha256(b"payload").hexdigest()
    assert intent.size == 7
    assert intent.document_id == DOCUMENT_ID
    assert intent.conversion_id == CONVERSION_ID
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_storage_intent_rejects_traversal_and_wrong_types(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    with pytest.raises(ValueError):
        StorageIntent.from_path(
            source,
            artifact_id="artifact-1",
            logical_key="../escape.pdf",
            source_id=SOURCE_ID,
            source_sha256=SOURCE_SHA256,
            decision_sha256=DECISION_SHA256,
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
            source_id=SOURCE_ID,
            source_sha256=SOURCE_SHA256,
            decision_sha256=DECISION_SHA256,
            visibility="repository_authorized",
            rights_state="approved",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_id", None),
        ("source_id", "rki:176904/12345.1"),
        ("source_sha256", "BAD"),
        ("decision_sha256", None),
    ],
)
def test_storage_intent_rejects_missing_or_invalid_authorization_provenance(
    tmp_path: Path, field: str, value: object
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    kwargs: dict[str, object] = {
        "source_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA256,
        "decision_sha256": DECISION_SHA256,
    }
    kwargs[field] = value

    with pytest.raises(ValueError, match=field):
        StorageIntent.from_path(
            source,
            artifact_id="artifact-1",
            logical_key="Jahre/1994/a.pdf",
            visibility="repository_authorized",
            rights_state="approved",
            **kwargs,
        )


def test_storage_intent_rejects_document_from_different_source_handle(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")

    with pytest.raises(ValueError, match="Handle und Version"):
        StorageIntent.from_path(
            source,
            artifact_id="artifact-1",
            logical_key="Jahre/1994/a.pdf",
            source_id=SOURCE_ID,
            source_sha256=SOURCE_SHA256,
            decision_sha256=DECISION_SHA256,
            document_id="rki-176904-99999-v2",
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
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=DECISION_SHA256,
        document_id=None,
        conversion_id=None,
        visibility="repository_authorized",
        rights_state="approved",
    )
    assert prepared.path == prepared_path
    assert prepared.document_id is None
    assert prepared.conversion_id is None
    with pytest.raises(ValueError, match="temp_root"):
        PreparedObject(
            artifact_id="artifact-1",
            logical_key="a.pdf",
            path=tmp_path / "outside.pdf",
            temp_root=temp_root,
            sha256="0" * 64,
            size=0,
            source_id=SOURCE_ID,
            source_sha256=SOURCE_SHA256,
            decision_sha256=DECISION_SHA256,
            visibility="repository_authorized",
            rights_state="approved",
        )


def test_prepared_object_rejects_document_from_different_source_version(
    tmp_path: Path,
) -> None:
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    prepared_path = temp_root / "a.pdf"
    prepared_path.write_bytes(b"x")

    with pytest.raises(ValueError, match="Handle und Version"):
        PreparedObject(
            artifact_id="artifact-1",
            logical_key="a.pdf",
            path=prepared_path,
            temp_root=temp_root,
            sha256=hashlib.sha256(b"x").hexdigest(),
            size=1,
            source_id=SOURCE_ID,
            source_sha256=SOURCE_SHA256,
            decision_sha256=DECISION_SHA256,
            document_id="rki-176904-12345-v3",
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
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        document_id="rki-176904-12345-v2",
        conversion_id="conv-" + "d" * 64,
        decision_sha256=DECISION_SHA256,
        provenance_state="current",
        visibility="repository_authorized",
        rights_state="approved",
        public_reference=None,
    )
    payload = reference.to_dict()
    validate_document("storage-reference", payload)
    assert payload["schema_version"] == "1.1.0"
    assert payload["source_id"] == SOURCE_ID
    assert payload["decision_sha256"] == DECISION_SHA256
    assert payload["storage_backend"] == "lfs"
    assert "repository_root" not in payload


def test_storage_reference_cannot_mark_missing_authorization_as_current() -> None:
    with pytest.raises(ValueError, match="current"):
        StorageReference(
            artifact_id="artifact-1",
            relative_path="rki/Bulletins/Jahre/1994/a.pdf",
            storage_backend=StorageBackend.LFS,
            storage_object_id="sha256:" + "a" * 64,
            sha256="a" * 64,
            size=12,
            source_id=None,
            source_sha256=None,
            document_id=None,
            conversion_id=None,
            decision_sha256=None,
            provenance_state="current",
            visibility="repository_authorized",
            rights_state="approved",
            public_reference=None,
        )


@pytest.mark.parametrize(
    ("source_id", "document_id"),
    (
        ("rki:176904/99999.2", DOCUMENT_ID),
        (SOURCE_ID, "rki-176904-12345-v3"),
        (None, DOCUMENT_ID),
    ),
)
def test_storage_reference_rejects_document_outside_exact_source_version(
    source_id: str | None,
    document_id: str,
) -> None:
    """Document links must belong to the same RKI handle and exact source version."""

    with pytest.raises(ValueError, match="source_id.*document_id"):
        StorageReference(
            artifact_id="artifact-1",
            relative_path="rki/Bulletins/Jahre/1994/a.pdf",
            storage_backend=StorageBackend.LFS,
            storage_object_id="sha256:" + "a" * 64,
            sha256="a" * 64,
            size=12,
            source_id=source_id,
            source_sha256=SOURCE_SHA256 if source_id is not None else None,
            document_id=document_id,
            conversion_id=None,
            decision_sha256=DECISION_SHA256 if source_id is not None else None,
            provenance_state="current" if source_id is not None else "legacy_needs_review",
            visibility="repository_authorized",
            rights_state="approved",
            public_reference=None,
        )


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


def test_factory_requires_clients_for_remote_backends(
    tmp_path: Path,
    storage_rights,
) -> None:
    config = load_storage_config(write_config(tmp_path, valid_config()))
    with pytest.raises(StorageConfigurationError, match="ReleaseClient"):
        build_storage_adapter(
            config,
            backend=StorageBackend.RELEASE,
            repository_root=tmp_path,
            authorizer=storage_rights.authorizer,
        )
    with pytest.raises(StorageConfigurationError, match="ObjectClient"):
        build_storage_adapter(
            config,
            backend=StorageBackend.OBJECT,
            repository_root=tmp_path,
            authorizer=storage_rights.authorizer,
        )


def test_factory_requires_explicit_authorizer_without_allow_all_default(
    tmp_path: Path,
) -> None:
    config = load_storage_config(write_config(tmp_path, valid_config()))

    with pytest.raises((TypeError, StorageConfigurationError), match="authorizer"):
        build_storage_adapter(
            config,
            backend=StorageBackend.LFS,
            repository_root=tmp_path,
        )


def test_factory_rejects_structural_authorizer(
    tmp_path: Path,
    storage_rights,
) -> None:
    class ArbitraryAuthorizer:
        def authorize(self, subject: object, *, operation: str) -> None:
            pass

    class DerivedAuthorizer(storage_base.RightsStorageAuthorizer):
        pass

    config = load_storage_config(write_config(tmp_path, valid_config()))
    invalid_authorizers = (
        ArbitraryAuthorizer(),
        DerivedAuthorizer(
            authority=storage_rights.authorizer.authority,
            policy=storage_rights.authorizer.policy,
        ),
    )
    for authorizer in invalid_authorizers:
        with pytest.raises(StorageConfigurationError, match="RightsStorageAuthorizer"):
            build_storage_adapter(
                config,
                backend=StorageBackend.LFS,
                repository_root=tmp_path,
                authorizer=authorizer,
            )

    adapter = build_storage_adapter(
        config,
        backend=StorageBackend.LFS,
        repository_root=tmp_path,
        authorizer=storage_rights.authorizer,
    )
    assert adapter.authorizer is storage_rights.authorizer


def test_rights_storage_authorizer_reloads_and_compares_exact_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_path = tmp_path / "rights-register.yml"

    def write_decision(state: str) -> str:
        register_path.write_text(
            "schema_version: 1\n"
            "decisions:\n"
            f'  - source_id: "{SOURCE_ID}"\n'
            f'    source_sha256: "{SOURCE_SHA256}"\n'
            f'    state: "{state}"\n'
            '    basis: "Reviewed RKI reuse terms"\n'
            '    reviewed_by: "Legal Reviewer"\n'
            '    reviewed_at: "2026-08-03T08:00:00Z"\n',
            encoding="utf-8",
        )
        decision = rights.load_rights_register(register_path).entries[0]
        assert decision.decision_sha256 is not None
        return decision.decision_sha256

    approved_hash = write_decision("approved")
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", register_path)
    authorizer_type = getattr(storage_base, "RightsStorageAuthorizer")
    authorizer = authorizer_type(
        authority=rights.load_rights_authority(),
        policy=rights.load_rights_policy(),
    )
    source = tmp_path / "source.bin"
    source.write_bytes(b"payload")
    intent = StorageIntent.from_path(
        source,
        artifact_id="artifact-1",
        logical_key="Jahre/1994/a.pdf",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=approved_hash,
        document_id=DOCUMENT_ID,
        visibility="repository_authorized",
        rights_state="approved",
    )
    authorizer.authorize(intent, operation="materialize")

    for drifted in (
        replace(intent, source_id="rki:176904/99999.2", document_id=None),
        replace(intent, source_sha256="e" * 64),
        replace(intent, decision_sha256="f" * 64),
        replace(intent, rights_state="metadata_only"),
    ):
        with pytest.raises(StorageError, match="Rechte|autorisiert|Entscheidung"):
            authorizer.authorize(drifted, operation="materialize")

    write_decision("takedown")
    with pytest.raises(StorageError, match="Rechte|autorisiert|Entscheidung"):
        authorizer.authorize(intent, operation="apply")
