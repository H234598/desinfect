
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.rki_pipeline.schema_registry import SchemaContractError, load_registry, load_schema, validate_document
from scripts import validate_schemas

ROOT = Path(__file__).resolve().parents[1]


def _storage_reference_payload() -> dict[str, object]:
    return {
        "schema_version": "1.1.0",
        "artifact_id": "artifact-1",
        "relative_path": "rki/Bulletins/a.pdf",
        "storage_backend": "lfs",
        "storage_object_id": "sha256:" + "a" * 64,
        "sha256": "a" * 64,
        "bytes": 1,
        "source_id": "rki:176904/12345.2",
        "source_sha256": "b" * 64,
        "document_id": "rki-176904-12345-v2",
        "conversion_id": None,
        "decision_sha256": "c" * 64,
        "provenance_state": "current",
        "visibility": "repository_authorized",
        "rights_state": "approved",
        "public_reference": None,
    }


def test_all_registered_schemas_are_strict_draft_2020_12() -> None:
    registry = load_registry()
    assert len(registry["contracts"]) == 12
    for entry in registry["contracts"]:
        schema = load_schema(entry["name"], registry)
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
        assert schema["$schema"].endswith("2020-12/schema")


def test_invalid_registered_schema_raises_contract_error(tmp_path: Path) -> None:
    schema_path = tmp_path / "invalid.schema.json"
    schema_path.write_text(
        json.dumps(
            {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "not-a-json-schema-type",
            }
        ),
        encoding="utf-8",
    )
    registry = {
        "contracts": [
            {
                "name": "invalid",
                "path": str(schema_path),
                "current_version": "1.0.0",
            }
        ]
    }

    with pytest.raises(SchemaContractError, match="Schema ist ungültig"):
        load_schema("invalid", registry)


def test_public_status_validates_and_unknown_field_fails_closed() -> None:
    status = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    validate_document("status", status)
    changed = deepcopy(status)
    changed["unexpected"] = True
    with pytest.raises(SchemaContractError, match="Additional properties"):
        validate_document("status", changed)


def test_storage_reference_rejects_non_sha256() -> None:
    payload = _storage_reference_payload()
    payload["sha256"] = "bad"
    with pytest.raises(SchemaContractError, match="does not match"):
        validate_document("storage-reference", payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("source_id", "rki:176904/12345.1"),
        ("source_sha256", "bad"),
        ("document_id", "legacy-document"),
        ("conversion_id", "conversion-bad"),
        ("decision_sha256", "bad"),
    ],
)
def test_storage_reference_rejects_invalid_provenance_fields(
    field: str, invalid: str
) -> None:
    payload = _storage_reference_payload()
    payload[field] = invalid

    with pytest.raises(SchemaContractError, match="does not match"):
        validate_document("storage-reference", payload)


@pytest.mark.parametrize("field", ["source_id", "source_sha256", "decision_sha256"])
def test_current_storage_reference_rejects_null_authorization(field: str) -> None:
    payload = _storage_reference_payload()
    payload[field] = None

    with pytest.raises(SchemaContractError):
        validate_document("storage-reference", payload)


def test_current_storage_reference_accepts_nullable_document_and_conversion_links() -> None:
    payload = _storage_reference_payload()
    payload["document_id"] = None
    payload["conversion_id"] = None

    validate_document("storage-reference", payload)


@pytest.mark.parametrize(
    ("source_id", "document_id"),
    (
        ("rki:176904/99999.2", "rki-176904-12345-v2"),
        ("rki:176904/12345.2", "rki-176904-12345-v3"),
        (None, "rki-176904-12345-v2"),
    ),
)
def test_storage_reference_schema_rejects_document_outside_source_version(
    source_id: str | None,
    document_id: str,
) -> None:
    """Schema validation must enforce the semantic source/document relationship."""

    payload = _storage_reference_payload()
    payload["source_id"] = source_id
    payload["document_id"] = document_id
    if source_id is None:
        payload["source_sha256"] = None
        payload["decision_sha256"] = None
        payload["provenance_state"] = "legacy_needs_review"

    with pytest.raises(SchemaContractError, match=r"source_id.*document_id"):
        validate_document("storage-reference", payload)


def test_schema_validator_checks_storage_reference_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the storage 1.0 migration check must expose a corrupt predecessor."""

    fixture_root = tmp_path / "tests" / "fixtures" / "schemas"
    fixture_root.mkdir(parents=True)
    (tmp_path / "status.json").write_bytes((ROOT / "status.json").read_bytes())
    for name in (
        "status-v2.json",
        "source-manifest-v1.0.json",
        "document-manifest-v1.0.json",
    ):
        (fixture_root / name).write_bytes(
            (ROOT / "tests" / "fixtures" / "schemas" / name).read_bytes()
        )
    (fixture_root / "storage-reference-v1.0.json").write_text(
        '{"schema_version":"0.9.0"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(validate_schemas, "ROOT", tmp_path)

    with pytest.raises(SchemaContractError, match="weder aktuell noch"):
        validate_schemas.validate()


@pytest.mark.parametrize(
    "field",
    [
        "source_id",
        "source_sha256",
        "document_id",
        "conversion_id",
        "decision_sha256",
        "provenance_state",
    ],
)
def test_storage_reference_requires_every_provenance_field(field: str) -> None:
    payload = _storage_reference_payload()
    del payload[field]

    with pytest.raises(SchemaContractError, match=rf"'{field}' is a required property"):
        validate_document("storage-reference", payload)
