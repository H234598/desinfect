"""Reference-integrity contracts for P06 manifest collections."""
from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.rki_pipeline.conversion.base import (
    EnvironmentVariable,
    NamedDigest,
    RuntimeEvidence,
    ToolEvidence,
    conversion_fingerprint,
    conversion_id,
)
from scripts.rki_pipeline.documents import bitstream_identity
from scripts.rki_pipeline.paths import DocumentType, repository_document_paths

SOURCE_ID = "rki:176904/12345"
DOCUMENT_ID = "rki-176904-12345-v1"
BITSTREAM_URL = "https://edoc.rki.de/bitstream/handle/176904/12345/source.pdf?sequence=1"
BITSTREAM_ID = bitstream_identity(BITSTREAM_URL).bitstream_id
SOURCE_SHA256 = "a" * 64
OUTPUT_SHA256 = "c" * 64
DECISION_SHA256 = "d" * 64


def _source() -> dict[str, object]:
    return {
        "schema_version": "1.1.0",
        "source_id": SOURCE_ID,
        "handle": "176904/12345",
        "version": 1,
        "source_url": "https://edoc.rki.de/handle/176904/12345",
        "title": "Synthetic bulletin",
        "publication_date": "1996-03-22",
        "etag": None,
        "last_modified": None,
        "sha256": SOURCE_SHA256,
        "rights": {
            "state": "approved",
            "basis": "Reviewed fixture",
            "reviewed_at": "2026-08-03T08:00:00Z",
            "reviewed_by": "Test Reviewer",
        },
        "provenance_state": "current",
        "bitstream_id": BITSTREAM_ID,
        "bitstream_url": BITSTREAM_URL,
        "bitstream_version": 1,
        "rights_evidence": {
            "label": "CC BY 4.0",
            "license_url": "https://creativecommons.org/licenses/by/4.0/",
            "copyright_notice": None,
            "open_access": True,
        },
        "decision_sha256": DECISION_SHA256,
        "same_content_as": [],
    }


def _document() -> dict[str, object]:
    paths = repository_document_paths(
        document_id=DOCUMENT_ID,
        bitstream_id=BITSTREAM_ID,
        document_type=DocumentType.ISSUE,
        publication_date="1996-03-22",
    )
    return {
        "schema_version": "1.1.0",
        "document_id": DOCUMENT_ID,
        "version": 1,
        "source_id": SOURCE_ID,
        "document_type": "gesamtausgabe",
        "publication_date": "1996-03-22",
        "paths": {"pdf": paths.pdf, "markdown": paths.markdown},
        "supersedes": None,
        "provenance_state": "current",
        "bitstream_id": BITSTREAM_ID,
        "bitstream_version": 1,
        "canonical_periods": {
            "week": "1996-W12",
            "month": "1996-03",
            "year": 1996,
        },
        "superseded_by": None,
    }


def _conversion(options_sha256: str = "b" * 64) -> dict[str, object]:
    tool = ToolEvidence(
        name="pdftotext",
        version_output="pdftotext version 25.05.0",
        executable_sha256="e" * 64,
        argv=("pdftotext", "-layout", "$INPUT", "$OUTPUT"),
        environment=(
            EnvironmentVariable("LANG", "C.UTF-8"),
            EnvironmentVariable("LC_ALL", "C.UTF-8"),
        ),
        ocr_settings=None,
    )
    runtime = RuntimeEvidence(
        platform="linux-x86_64",
        libc="glibc-2.39",
        shared_libraries=(NamedDigest("libpoppler.so.140", "f" * 64),),
        fonts=(NamedDigest("DejaVuSans.ttf", "1" * 64),),
    )
    fingerprint = conversion_fingerprint(
        source_sha256=SOURCE_SHA256,
        converter="pdftotext-layout",
        converter_version="25.05.0",
        options_sha256=options_sha256,
        toolchain=(tool,),
        runtime=runtime,
    )
    identity = conversion_id(DOCUMENT_ID, BITSTREAM_ID, fingerprint)
    return {
        "schema_version": "1.1.0",
        "conversion_id": identity,
        "document_id": DOCUMENT_ID,
        "bitstream_id": BITSTREAM_ID,
        "source_sha256": SOURCE_SHA256,
        "converter": "pdftotext-layout",
        "converter_version": "25.05.0",
        "options_sha256": options_sha256,
        "page_count": 2,
        "toolchain": [tool.to_dict()],
        "runtime": runtime.to_dict(),
        "fingerprint_sha256": fingerprint,
        "output_sha256": OUTPUT_SHA256,
        "storage_reference": identity,
        "state": "converted",
        "quality": "good",
        "ocr_used": False,
        "provenance_state": "current",
    }


def _storage() -> tuple[dict[str, object], dict[str, object]]:
    document = _document()
    conversion = _conversion()
    common = {
        "schema_version": "1.1.0",
        "storage_backend": "lfs",
        "source_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA256,
        "document_id": DOCUMENT_ID,
        "decision_sha256": DECISION_SHA256,
        "provenance_state": "current",
        "visibility": "repository_authorized",
        "rights_state": "approved",
        "public_reference": None,
    }
    return (
        {
            **common,
            "artifact_id": BITSTREAM_ID,
            "relative_path": document["paths"]["pdf"],
            "storage_object_id": f"sha256:{SOURCE_SHA256}",
            "sha256": SOURCE_SHA256,
            "bytes": 100,
            "conversion_id": None,
        },
        {
            **common,
            "artifact_id": conversion["conversion_id"],
            "relative_path": document["paths"]["markdown"],
            "storage_object_id": f"sha256:{OUTPUT_SHA256}",
            "sha256": OUTPUT_SHA256,
            "bytes": 200,
            "conversion_id": conversion["conversion_id"],
        },
    )


def _build(
    *,
    sources: tuple[dict[str, object], ...] | None = None,
    documents: tuple[dict[str, object], ...] | None = None,
    conversions: tuple[dict[str, object], ...] | None = None,
    storage: tuple[dict[str, object], ...] | None = None,
):
    from scripts.rki_pipeline.manifests import build_manifest_graph

    return build_manifest_graph(
        sources=(_source(),) if sources is None else sources,
        documents=(_document(),) if documents is None else documents,
        conversions=(_conversion(),) if conversions is None else conversions,
        storage_references=_storage() if storage is None else storage,
    )


def test_graph_accepts_exact_linked_current_manifests() -> None:
    graph = _build()

    assert tuple(item["bitstream_id"] for item in graph.sources) == (BITSTREAM_ID,)
    assert tuple(item["document_id"] for item in graph.documents) == (DOCUMENT_ID,)
    assert tuple(item["conversion_id"] for item in graph.conversions) == (
        _conversion()["conversion_id"],
    )
    assert tuple(item["artifact_id"] for item in graph.storage_references) == tuple(
        sorted((BITSTREAM_ID, _conversion()["conversion_id"]))
    )


@pytest.mark.parametrize(
    ("collection", "field", "value", "message"),
    (
        ("documents", "bitstream_id", "rki-bitstream-" + "2" * 64, "Bitstream"),
        ("documents", "source_id", "rki:176904/54321", "verknüpfter Source"),
        ("sources", "sha256", "2" * 64, "Source-SHA"),
        ("storage", "sha256", "2" * 64, "Artefakt-SHA"),
        ("storage", "storage_object_id", "sha256:" + "2" * 64, "LFS-Objekt"),
        ("storage", "decision_sha256", "2" * 64, "Rechteentscheidung"),
    ),
)
def test_graph_rejects_dangling_or_drifting_links(
    collection: str,
    field: str,
    value: str,
    message: str,
) -> None:
    values = {
        "sources": (_source(),),
        "documents": (_document(),),
        "conversions": (_conversion(),),
        "storage": _storage(),
    }
    changed = deepcopy(values[collection])
    changed[0][field] = value
    values[collection] = changed

    with pytest.raises(ValueError, match=message):
        _build(**values)


def test_graph_rejects_duplicate_primary_identity() -> None:
    with pytest.raises(ValueError, match="doppelt"):
        _build(sources=(_source(), _source()))


def _second_bitstream() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    url = "https://edoc.rki.de/bitstream/handle/176904/12345/source.pdf?sequence=2"
    bitstream = bitstream_identity(url)
    source = deepcopy(_source())
    source.update(
        bitstream_id=bitstream.bitstream_id,
        bitstream_url=url,
        bitstream_version=2,
        same_content_as=[BITSTREAM_ID],
    )
    document = deepcopy(_document())
    paths = repository_document_paths(
        document_id=DOCUMENT_ID,
        bitstream_id=bitstream.bitstream_id,
        document_type=DocumentType.ISSUE,
        publication_date="1996-03-22",
    )
    document.update(
        bitstream_id=bitstream.bitstream_id,
        bitstream_version=2,
        paths={"pdf": paths.pdf, "markdown": None},
    )
    storage = deepcopy(_storage()[0])
    storage.update(
        artifact_id=bitstream.bitstream_id,
        relative_path=paths.pdf,
    )
    return source, document, storage


def test_graph_accepts_same_document_with_two_sorted_bitstreams() -> None:
    second_source, second_document, second_storage = _second_bitstream()
    graph = _build(
        sources=(second_source, _source()),
        documents=(second_document, _document()),
        storage=(second_storage, *_storage()),
    )

    assert [item["bitstream_id"] for item in graph.sources] == sorted(
        (BITSTREAM_ID, second_source["bitstream_id"])
    )
    assert [item["document_id"] for item in graph.documents] == [DOCUMENT_ID, DOCUMENT_ID]


def test_graph_rejects_alias_cycle() -> None:
    second_source, second_document, second_storage = _second_bitstream()
    first_source = _source()
    first_source["same_content_as"] = [second_source["bitstream_id"]]

    with pytest.raises(ValueError, match="Alias.*Zyklus"):
        _build(
            sources=(first_source, second_source),
            documents=(_document(), second_document),
            storage=(*_storage(), second_storage),
        )


def test_graph_rejects_second_active_conversion_for_same_document_bitstream() -> None:
    with pytest.raises(ValueError, match="Persistierte aktive Conversion.*doppelt"):
        _build(conversions=(_conversion(), _conversion("2" * 64)))


def test_graph_rejects_legacy_manifest_before_linking() -> None:
    source = _source()
    source["provenance_state"] = "legacy_needs_review"

    with pytest.raises(ValueError, match="nur current"):
        _build(sources=(source,))


def test_graph_represents_due_conversion_and_storage_without_dangling_edges() -> None:
    document = _document()
    document["paths"]["markdown"] = None
    conversion = _conversion()
    conversion["storage_reference"] = None

    graph = _build(
        documents=(document,),
        conversions=(conversion,),
        storage=(),
    )

    assert graph.storage_references == ()
    assert graph.documents[0]["paths"]["markdown"] is None


def test_graph_rejects_explicit_dangling_conversion_storage_edge() -> None:
    with pytest.raises(ValueError, match="Storage-Referenz löst nicht"):
        _build(storage=(_storage()[0],))


def test_pdf_storage_artifact_id_is_independent_from_bitstream_id() -> None:
    pdf_reference, markdown_reference = _storage()
    pdf_reference["artifact_id"] = "pdf-object-1"

    graph = _build(storage=(pdf_reference, markdown_reference))

    assert {item["artifact_id"] for item in graph.storage_references} == {
        "pdf-object-1",
        markdown_reference["artifact_id"],
    }


def test_graph_rejects_public_reference_without_public_visibility() -> None:
    pdf_reference, markdown_reference = _storage()
    pdf_reference["public_reference"] = "https://example.test/archive.pdf"

    with pytest.raises(ValueError, match="public visibility"):
        _build(storage=(pdf_reference, markdown_reference))
