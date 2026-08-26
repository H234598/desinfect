"""TDD contract for backend-neutral storage types and strict TOML config."""
from __future__ import annotations

import hashlib
import ast
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from scripts.rki_pipeline import rights
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.storage import base as storage_base
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageBackend,
    StorageAuthorizationError,
    StorageConfigurationError,
    StorageError,
    StorageIntent,
    StorageReference,
    rights_actions_for_storage_operation,
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
    ("source_id", "source_sha256", "decision_sha256"),
    (
        (SOURCE_ID, None, None),
        (None, SOURCE_SHA256, None),
        (None, None, DECISION_SHA256),
        (SOURCE_ID, SOURCE_SHA256, None),
        (SOURCE_ID, None, DECISION_SHA256),
        (None, SOURCE_SHA256, DECISION_SHA256),
    ),
)
def test_legacy_storage_reference_rejects_partial_authorization_provenance(
    source_id: str | None,
    source_sha256: str | None,
    decision_sha256: str | None,
) -> None:
    with pytest.raises(ValueError, match=r"Provenienz|gemeinsam|vollständig"):
        StorageReference(
            artifact_id="artifact-1",
            relative_path="rki/Bulletins/Jahre/1994/a.pdf",
            storage_backend=StorageBackend.LFS,
            storage_object_id="sha256:" + "a" * 64,
            sha256="a" * 64,
            size=12,
            source_id=source_id,
            source_sha256=source_sha256,
            document_id=None,
            conversion_id=None,
            decision_sha256=decision_sha256,
            provenance_state="legacy_needs_review",
            visibility="repository_authorized",
            rights_state="unknown",
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

    with pytest.raises(ValueError, match=r"source_id.*document_id"):
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
        canonical_url = (
            "https://edoc.rki.de/bitstream/handle/176904/12345.2/"
            "source.pdf?sequence=1"
        )
        approved = state == "approved"
        register_path.write_text(
            yaml.safe_dump(
                {
                    "schema_version": 2,
                    "decisions": [{
                        "source_id": SOURCE_ID,
                        "canonical_url": canonical_url,
                        "version_or_bitstream": rights.bitstream_identity(
                            canonical_url
                        ).bitstream_id,
                        "source_sha256": SOURCE_SHA256,
                        "state": state,
                        "mode": "materialized" if approved else "remove_all",
                        "allowed_actions": (
                            sorted(action.value for action in rights.RightsAction)
                            if approved else []
                        ),
                        "components_state": "cleared" if approved else "blocked",
                        "attribution": {
                            "creators": ["Synthetic Creator"],
                            "attribution_parties": ["Synthetic Rights Holder"],
                            "copyright_notice": "Synthetic copyright notice",
                            "license_notice": "CC BY 4.0",
                            "license_url": (
                                "https://creativecommons.org/licenses/by/4.0/"
                            ),
                            "disclaimer_notice": "Synthetic fixture only",
                            "origin_url": (
                                "https://edoc.rki.de/handle/176904/12345.2"
                            ),
                            "prior_change_history": [],
                            "current_change_notice": "Unchanged synthetic fixture",
                        } if approved else None,
                        "basis": (
                            "Synthetic fixture; no external publication rights claim"
                        ),
                        "reviewed_by": "Test Fixture",
                        "reviewed_at": "2026-08-03T08:00:00Z",
                    }],
                },
                sort_keys=False,
            ),
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
        with pytest.raises(StorageError, match=r"Rechte|autorisiert|Entscheidung"):
            authorizer.authorize(drifted, operation="materialize")

    write_decision("takedown")
    with pytest.raises(StorageError, match=r"Rechte|autorisiert|Entscheidung"):
        authorizer.authorize(intent, operation="apply")


def test_rights_storage_authorizer_rejects_forged_authority_seal(
    tmp_path: Path,
    storage_rights,
) -> None:
    """Direct field forgery must not turn an arbitrary register into authority."""

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "approved"))
    decision = rights.load_rights_register(storage_rights.register_path).entries[0]
    forged = object.__new__(rights.RightsAuthority)
    object.__setattr__(forged, "_register_source", storage_rights.register_path)
    object.__setattr__(forged, "_token", object())
    object.__setattr__(forged, "_isolated", True)
    authorizer = storage_base.RightsStorageAuthorizer(
        forged,
        storage_rights.authorizer.policy,
    )
    source = tmp_path / "forged-source.bin"
    source.write_bytes(b"payload")
    intent = StorageIntent.from_path(
        source,
        artifact_id="forged-artifact",
        logical_key="Jahre/1994/forged.pdf",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=decision.decision_sha256 or "0" * 64,
        document_id=DOCUMENT_ID,
        visibility="repository_authorized",
        rights_state="approved",
    )

    with pytest.raises(StorageAuthorizationError, match="Rechteentscheidung"):
        authorizer.authorize(intent, operation="materialize")


def test_rights_storage_authorizer_rejects_canonical_source_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    storage_rights,
) -> None:
    """A minted authority must stop working when canonical source binding changes."""

    storage_rights.set_decisions((SOURCE_ID, SOURCE_SHA256, "approved"))
    decision = rights.load_rights_register(storage_rights.register_path).entries[0]
    source = tmp_path / "source-drift.bin"
    source.write_bytes(b"payload")
    intent = StorageIntent.from_path(
        source,
        artifact_id="source-drift-artifact",
        logical_key="Jahre/1994/source-drift.pdf",
        source_id=SOURCE_ID,
        source_sha256=SOURCE_SHA256,
        decision_sha256=decision.decision_sha256 or "0" * 64,
        document_id=DOCUMENT_ID,
        visibility="repository_authorized",
        rights_state="approved",
    )
    replacement = tmp_path / "replacement-rights.yml"
    replacement.write_text("schema_version: 2\ndecisions: []\n", encoding="utf-8")
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", replacement)

    with pytest.raises(StorageAuthorizationError, match="Rechteentscheidung"):
        storage_rights.authorizer.authorize(intent, operation="materialize")


@pytest.mark.parametrize(
    ("operation", "backend", "visibility", "expected"),
    (
        ("materialize", None, "internal", (rights.RightsAction.CACHE,)),
        ("export", None, "internal", (rights.RightsAction.FETCH,)),
        ("verify", None, "internal", (rights.RightsAction.HASH,)),
        ("apply", StorageBackend.LFS, "public", (rights.RightsAction.CACHE,)),
        ("apply", StorageBackend.OBJECT, "internal", (rights.RightsAction.CACHE,)),
        (
            "apply",
            StorageBackend.OBJECT,
            "public",
            (rights.RightsAction.CACHE, rights.RightsAction.PUBLISH),
        ),
        ("convert", None, "internal", (rights.RightsAction.EXTRACT_TEXT,)),
        ("convert_ocr", None, "internal", (rights.RightsAction.OCR,)),
        ("convert_output", None, "internal", (rights.RightsAction.CACHE,)),
        ("convert_manifest", None, "internal", (rights.RightsAction.CACHE,)),
        ("convert_publish", None, "internal", (rights.RightsAction.CACHE,)),
        ("archive", None, "internal", (rights.RightsAction.CACHE,)),
    ),
)
def test_storage_operation_mapping_is_total_and_effect_specific(
    operation: str,
    backend: StorageBackend | None,
    visibility: str,
    expected: tuple[object, ...],
) -> None:
    """Wrong operation mapping must block before payload effects."""

    assert rights_actions_for_storage_operation(
        operation,
        backend=backend,
        visibility=visibility,
    ) == expected


@pytest.mark.parametrize(
    ("operation", "backend"),
    (("exists", None), ("period-archive-materialize", None), ("apply", None)),
)
def test_unknown_or_underspecified_storage_operation_is_rejected(
    operation: str,
    backend: StorageBackend | None,
) -> None:
    """Lookup aliases and backend-ambiguous apply must never become effects."""

    with pytest.raises(storage_base.StorageAuthorizationError):
        rights_actions_for_storage_operation(
            operation,
            backend=backend,
            visibility="public",
        )


def test_effect_operation_source_inventory_is_closed() -> None:
    """New effect literals must add an explicit action mapping and contract test."""

    relatives = (
        "storage/lfs.py",
        "storage/remote.py",
        "storage/migrate.py",
        "conversion/service.py",
        "archive.py",
        "manifests.py",
        "aggregation.py",
    )
    observed: set[str] = set()
    for relative in relatives:
        source = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "rki_pipeline"
            / relative
        ).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if (
                    keyword.arg == "operation"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    observed.add(keyword.value.value)

    assert observed == {
        "apply",
        "archive",
        "convert",
        "convert_manifest",
        "convert_ocr",
        "convert_output",
        "convert_publish",
        "export",
        "materialize",
        "verify",
    }


def test_authority_register_source_access_is_sealed_in_production() -> None:
    """Only rights.py may read the opaque authority register source field."""

    pipeline_root = Path(__file__).resolve().parents[1] / "scripts" / "rki_pipeline"
    offenders: list[str] = []
    for path in sorted(pipeline_root.rglob("*.py")):
        if path.name == "rights.py":
            continue
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Attribute) and node.attr == "_register_source":
                offenders.append(f"{path.relative_to(pipeline_root)}:{node.lineno}")

    assert offenders == []
