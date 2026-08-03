
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from scripts.rki_pipeline.schema_registry import SchemaContractError, load_registry, load_schema, validate_document

ROOT = Path(__file__).resolve().parents[1]


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
    payload = {
        "schema_version": "1.0.0",
        "artifact_id": "artifact-1",
        "relative_path": "rki/Bulletins/a.pdf",
        "storage_backend": "lfs",
        "storage_object_id": "sha256:bad",
        "sha256": "bad",
        "bytes": 1,
        "visibility": "repository_authorized",
        "rights_state": "approved",
        "public_reference": None,
    }
    with pytest.raises(SchemaContractError, match="does not match"):
        validate_document("storage-reference", payload)
