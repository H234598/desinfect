
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


def _conversion_manifest_payload() -> dict[str, object]:
    return {
        "schema_version": "1.1.0",
        "conversion_id": "conv-a7632e1e638770f8420f94dca8d7e842bd4ab0845a78a5ad9e05d73090f0980b",
        "document_id": "rki-176904-12345-v2",
        "bitstream_id": "rki-bitstream-" + "1" * 64,
        "source_sha256": "a" * 64,
        "converter": "pdftotext-layout",
        "converter_version": "25.05.0",
        "options_sha256": "b" * 64,
        "page_count": 2,
        "toolchain": [
            {
                "name": "pdftotext",
                "version_output": "pdftotext version 25.05.0",
                "executable_sha256": "d" * 64,
                "argv": ["pdftotext", "-layout", "$INPUT", "$OUTPUT"],
                "environment": [
                    {"name": "LANG", "value": "C.UTF-8"},
                    {"name": "LC_ALL", "value": "C.UTF-8"},
                ],
                "ocr_settings": None,
            }
        ],
        "runtime": {
            "platform": "linux-x86_64",
            "libc": "glibc-2.39",
            "shared_libraries": [{"name": "libpoppler.so.140", "sha256": "e" * 64}],
            "fonts": [{"name": "DejaVuSans.ttf", "sha256": "f" * 64}],
        },
        "fingerprint_sha256": "f294e43694711cb2db6dbc71bdf26805ca504131fe6db2ae4b30e894ce3eedab",
        "output_sha256": "c" * 64,
        "storage_reference": "markdown-rki-176904-12345-v2",
        "state": "converted",
        "quality": "good",
        "ocr_used": False,
        "provenance_state": "current",
    }


def test_all_registered_schemas_are_strict_draft_2020_12() -> None:
    registry = load_registry()
    assert len(registry["contracts"]) == 13
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


def test_period_archive_manifest_validates_and_unknown_field_fails_closed() -> None:
    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "schemas" / "period-archive-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    validate_document("period-archive-manifest", fixture)
    changed = deepcopy(fixture)
    changed["unexpected"] = True
    with pytest.raises(SchemaContractError, match="Additional properties"):
        validate_document("period-archive-manifest", changed)


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


@pytest.mark.parametrize(
    "corrupt_fixture",
    ["storage-reference-v1.0.json", "conversion-manifest-v1.0.json"],
)
def test_schema_validator_checks_every_p06_predecessor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt_fixture: str,
) -> None:
    """Every registered P06 predecessor must be loaded and migrated."""

    fixture_root = tmp_path / "tests" / "fixtures" / "schemas"
    fixture_root.mkdir(parents=True)
    (tmp_path / "status.json").write_bytes((ROOT / "status.json").read_bytes())
    for name in (
        "status-v2.json",
        "source-manifest-v1.0.json",
        "document-manifest-v1.0.json",
        "storage-reference-v1.0.json",
        "conversion-manifest-v1.0.json",
        "period-archive-manifest.json",
    ):
        target = fixture_root / name
        if name == corrupt_fixture:
            continue
        target.write_bytes((ROOT / "tests" / "fixtures" / "schemas" / name).read_bytes())
    (fixture_root / corrupt_fixture).write_text(
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


def test_current_conversion_manifest_validates_complete_evidence() -> None:
    validate_document("conversion-manifest", _conversion_manifest_payload())


@pytest.mark.parametrize(
    "field",
    [
        "conversion_id",
        "bitstream_id",
        "page_count",
        "toolchain",
        "runtime",
        "fingerprint_sha256",
        "storage_reference",
        "provenance_state",
    ],
)
def test_conversion_manifest_requires_every_v1_1_field(field: str) -> None:
    payload = _conversion_manifest_payload()
    del payload[field]

    with pytest.raises(SchemaContractError, match=rf"'{field}' is a required property"):
        validate_document("conversion-manifest", payload)


@pytest.mark.parametrize(
    "field",
    ["conversion_id", "bitstream_id", "page_count", "toolchain", "runtime", "fingerprint_sha256"],
)
def test_current_conversion_manifest_rejects_null_evidence(field: str) -> None:
    payload = _conversion_manifest_payload()
    payload[field] = None

    with pytest.raises(SchemaContractError):
        validate_document("conversion-manifest", payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("conversion_id", "conversion-bad"),
        ("bitstream_id", "bitstream-bad"),
        ("fingerprint_sha256", "bad"),
        ("page_count", 0),
    ],
)
def test_conversion_manifest_rejects_invalid_identity_evidence(
    field: str, invalid: str | int
) -> None:
    payload = _conversion_manifest_payload()
    payload[field] = invalid

    with pytest.raises(SchemaContractError):
        validate_document("conversion-manifest", payload)


@pytest.mark.parametrize("field", ["fingerprint_sha256", "conversion_id"])
def test_conversion_manifest_rejects_tampered_derived_identity(field: str) -> None:
    payload = _conversion_manifest_payload()
    payload[field] = ("conv-" if field == "conversion_id" else "") + "0" * 64

    with pytest.raises(SchemaContractError, match=r"Fingerprint|conversion_id"):
        validate_document("conversion-manifest", payload)


@pytest.mark.parametrize(
    ("state", "quality"),
    [
        ("converted", "good"),
        ("skipped_unchanged", "good"),
        ("skipped_unchanged", "needs_review"),
        ("needs_review", "needs_review"),
    ],
)
def test_materialized_conversion_requires_output_but_accepts_no_storage_reference(
    state: str,
    quality: str,
) -> None:
    payload = _conversion_manifest_payload()
    payload["state"] = state
    payload["quality"] = quality
    payload["storage_reference"] = None

    validate_document("conversion-manifest", payload)

    payload["output_sha256"] = None
    with pytest.raises(SchemaContractError):
        validate_document("conversion-manifest", payload)


@pytest.mark.parametrize("state", ["failed", "not_materialized"])
def test_unmaterialized_conversion_rejects_output_and_storage_reference(state: str) -> None:
    payload = _conversion_manifest_payload()
    payload["state"] = state
    payload["quality"] = "failed" if state == "failed" else "not_assessed"

    with pytest.raises(SchemaContractError):
        validate_document("conversion-manifest", payload)


@pytest.mark.parametrize(
    ("state", "quality", "output_sha256", "storage_reference"),
    [
        ("converted", "needs_review", "c" * 64, None),
        ("needs_review", "good", "c" * 64, None),
        ("failed", "good", None, None),
        ("not_materialized", "good", None, None),
        ("skipped_unchanged", "failed", "c" * 64, None),
    ],
)
def test_conversion_manifest_rejects_state_quality_drift(
    state: str,
    quality: str,
    output_sha256: str | None,
    storage_reference: str | None,
) -> None:
    payload = _conversion_manifest_payload()
    payload.update(
        state=state,
        quality=quality,
        output_sha256=output_sha256,
        storage_reference=storage_reference,
    )

    with pytest.raises(SchemaContractError):
        validate_document("conversion-manifest", payload)


@pytest.mark.parametrize("field", ["converter", "converter_version"])
def test_conversion_manifest_binds_converter_identity_into_fingerprint(field: str) -> None:
    payload = _conversion_manifest_payload()
    payload[field] = "tampered"

    with pytest.raises(SchemaContractError, match="Fingerprint"):
        validate_document("conversion-manifest", payload)


def test_conversion_manifest_rejects_nonfixed_environment_evidence() -> None:
    payload = _conversion_manifest_payload()
    payload["toolchain"][0]["environment"] = [  # type: ignore[index]
        {"name": "GH_TOKEN", "value": "secret"}
    ]

    with pytest.raises(SchemaContractError):
        validate_document("conversion-manifest", payload)


def test_conversion_manifest_rejects_nested_unknown_fields() -> None:
    payload = _conversion_manifest_payload()
    payload["runtime"]["machine_path"] = "/usr/lib"  # type: ignore[index]

    with pytest.raises(SchemaContractError, match="Additional properties"):
        validate_document("conversion-manifest", payload)
