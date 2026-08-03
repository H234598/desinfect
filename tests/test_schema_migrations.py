
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.rki_pipeline.documents import bitstream_identity
from scripts.rki_pipeline.schema_registry import SchemaContractError, migrate_document, validate_document

ROOT = Path(__file__).resolve().parents[1]


def test_status_v2_to_v3_is_deterministic_and_keeps_dimensions_separate() -> None:
    source = json.loads(
        (ROOT / "tests" / "fixtures" / "schemas" / "status-v2.json").read_text(encoding="utf-8")
    )
    first = migrate_document("status", source)
    second = migrate_document("status", deepcopy(source))
    assert first == second
    assert first["schema_version"] == "3.0.0"
    assert first["pipeline"]["last_main_commit_at"] is None
    assert first["pipeline"]["last_successful_run_at"] == "2026-07-20T04:31:12Z"
    assert first["pipeline"]["last_successful_write_at"] == "2026-07-19T04:31:12Z"
    assert first["corpus"]["analysis_corpus_complete_through_year"] == 2020
    assert first["corpus"]["public_mirror_complete_through_year"] is None
    validate_document("status", first)


def test_only_one_predecessor_version_is_accepted() -> None:
    with pytest.raises(SchemaContractError, match="weder aktuell noch"):
        migrate_document("status", {"schema_version": "1.0.0"})


def _fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / "tests" / "fixtures" / "schemas" / name).read_text(encoding="utf-8"))


def test_source_manifest_v1_to_v1_1_is_deterministic_and_preserves_unknowns() -> None:
    source = _fixture("source-manifest-v1.0.json")
    original = deepcopy(source)
    first = migrate_document("source-manifest", source)
    second = migrate_document("source-manifest", deepcopy(source))

    assert source == original
    assert first == second
    assert first["schema_version"] == "1.1.0"
    assert first["provenance_state"] == "legacy_needs_review"
    assert first["bitstream_id"] is None
    assert first["bitstream_url"] is None
    assert first["bitstream_version"] is None
    assert first["decision_sha256"] is None
    assert first["rights_evidence"] == {
        "label": None,
        "license_url": None,
        "copyright_notice": None,
        "open_access": None,
    }
    validate_document("source-manifest", first)


def test_document_manifest_v1_to_v1_1_is_deterministic_and_derives_periods() -> None:
    source = _fixture("document-manifest-v1.0.json")
    original = deepcopy(source)
    first = migrate_document("document-manifest", source)
    second = migrate_document("document-manifest", deepcopy(source))

    assert source == original
    assert first == second
    assert first["schema_version"] == "1.1.0"
    assert first["provenance_state"] == "legacy_needs_review"
    assert first["bitstream_id"] is None
    assert first["bitstream_version"] is None
    assert first["canonical_periods"] == {"week": "1996-W12", "month": "1996-03", "year": 1996}
    assert first["superseded_by"] is None
    validate_document("document-manifest", first)


def test_storage_reference_v1_to_v1_1_is_deterministic_without_authorization_invention() -> None:
    source = _fixture("storage-reference-v1.0.json")
    original = deepcopy(source)
    first = migrate_document("storage-reference", source)
    second = migrate_document("storage-reference", deepcopy(source))

    assert source == original
    assert first == second
    assert migrate_document("storage-reference", first) == first
    assert first["schema_version"] == "1.1.0"
    assert first["provenance_state"] == "legacy_needs_review"
    assert first["source_id"] is None
    assert first["source_sha256"] is None
    assert first["document_id"] is None
    assert first["conversion_id"] is None
    assert first["decision_sha256"] is None
    validate_document("storage-reference", first)


def test_conversion_manifest_v1_to_v1_1_is_deterministic_without_invented_evidence() -> None:
    source = _fixture("conversion-manifest-v1.0.json")
    original = deepcopy(source)
    first = migrate_document("conversion-manifest", source)
    second = migrate_document("conversion-manifest", deepcopy(source))

    assert source == original
    assert first == second
    assert migrate_document("conversion-manifest", first) == first
    assert first["schema_version"] == "1.1.0"
    assert first["provenance_state"] == "legacy_needs_review"
    for field in (
        "conversion_id",
        "bitstream_id",
        "page_count",
        "toolchain",
        "runtime",
        "fingerprint_sha256",
        "storage_reference",
    ):
        assert first[field] is None
    assert first["output_sha256"] == original["output_sha256"]
    validate_document("conversion-manifest", first)


@pytest.mark.parametrize(
    "name",
    ["source-manifest", "document-manifest", "conversion-manifest", "storage-reference"],
)
def test_manifest_migrations_reject_versions_other_than_exact_v1(name: str) -> None:
    with pytest.raises(SchemaContractError, match="weder aktuell noch"):
        migrate_document(name, {"schema_version": "0.9.0"})


def test_source_manifest_rejects_unsorted_content_aliases() -> None:
    payload = migrate_document("source-manifest", _fixture("source-manifest-v1.0.json"))
    payload["same_content_as"] = [
        "rki-bitstream-" + "b" * 64,
        "rki-bitstream-" + "a" * 64,
    ]

    with pytest.raises(SchemaContractError, match="sortiert"):
        validate_document("source-manifest", payload)


def test_source_manifest_accepts_canonical_uppercase_pdf_extension() -> None:
    payload = migrate_document("source-manifest", _fixture("source-manifest-v1.0.json"))
    payload["bitstream_url"] = bitstream_identity(
        "https://edoc.rki.de/bitstream/handle/176904/12345.2/file.PDF?sequence=2"
    ).canonical_url

    validate_document("source-manifest", payload)


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("document_id", "rki-not-canonical"),
        ("supersedes", "legacy-document"),
        ("superseded_by", "other-document"),
    ],
)
def test_document_manifest_rejects_noncanonical_relation_ids(field: str, invalid: str) -> None:
    payload = migrate_document("document-manifest", _fixture("document-manifest-v1.0.json"))
    payload[field] = invalid

    with pytest.raises(SchemaContractError, match="does not match"):
        validate_document("document-manifest", payload)


def test_document_manifest_accepts_canonical_and_nullable_relation_ids() -> None:
    payload = migrate_document("document-manifest", _fixture("document-manifest-v1.0.json"))
    payload["superseded_by"] = "rki-176904-12345-v3"
    validate_document("document-manifest", payload)

    payload["supersedes"] = None
    payload["superseded_by"] = None
    validate_document("document-manifest", payload)


@pytest.mark.parametrize(
    ("name", "field", "invalid"),
    [
        ("source-manifest", "handle", "176904/12345.1"),
        ("source-manifest", "source_id", "rki:176904/12345.1"),
        (
            "source-manifest",
            "bitstream_url",
            "https://edoc.rki.de/bitstream/handle/176904/12345.1/file.pdf",
        ),
        ("source-manifest", "handle", "999999/12345"),
        ("document-manifest", "document_id", "rki-999999-12345-v1"),
        ("document-manifest", "source_id", "rki:999999/12345"),
        ("document-manifest", "supersedes", "rki-999999-12345-v1"),
        ("document-manifest", "superseded_by", "rki-999999-12345-v3"),
    ],
)
def test_manifest_contracts_reject_non_rki_or_explicit_v1_handles(
    name: str, field: str, invalid: str
) -> None:
    payload = migrate_document(name, _fixture(f"{name}-v1.0.json"))
    payload[field] = invalid

    with pytest.raises(SchemaContractError, match="does not match"):
        validate_document(name, payload)
