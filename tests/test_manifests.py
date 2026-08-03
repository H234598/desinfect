"""Reference-integrity contracts for P06 manifest collections."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path

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
from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.paths import DocumentType, repository_document_paths
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode

SOURCE_ID = "rki:176904/900000001"
DOCUMENT_ID = "rki-176904-900000001-v1"
BITSTREAM_URL = "https://edoc.rki.de/bitstream/handle/176904/900000001/source.pdf?sequence=1"
BITSTREAM_ID = bitstream_identity(BITSTREAM_URL).bitstream_id
SOURCE_SHA256 = "4665c3b8cfa6de8d9792a8defb977bfd200465b513575419e0a88541000f5b2a"
OUTPUT_SHA256 = "c" * 64
DECISION_SHA256 = "e36c7613fc7b87bf1c6a6b497355ab63a317a24d3d590ef0e255a3098f9ff926"


def _authorizer():
    from scripts.rki_pipeline.rights import load_rights_authority, load_rights_policy
    from scripts.rki_pipeline.storage.base import RightsStorageAuthorizer

    return RightsStorageAuthorizer(load_rights_authority(), load_rights_policy())


def _source() -> dict[str, object]:
    return {
        "schema_version": "1.1.0",
        "source_id": SOURCE_ID,
        "handle": "176904/900000001",
        "version": 1,
        "source_url": "https://edoc.rki.de/handle/176904/900000001",
        "title": "Synthetic bulletin",
        "publication_date": "1996-03-22",
        "etag": None,
        "last_modified": None,
        "sha256": SOURCE_SHA256,
        "rights": {
            "state": "approved",
            "basis": "Synthetic P06 conversion fixture; no external publication rights claim",
            "reviewed_at": "2026-08-03T14:00:00Z",
            "reviewed_by": "Desinfect maintainers",
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
        authorizer=_authorizer(),
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
        ("sources", "sha256", "2" * 64, "Rechteentscheidung"),
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


def test_graph_rejects_source_and_bitstream_cross_handle_drift() -> None:
    source = _source()
    source["source_url"] = "https://edoc.rki.de/handle/176904/999999999"
    with pytest.raises(ValueError, match="Source-URL"):
        _build(sources=(source,), documents=(), conversions=(), storage=())

    source = _source()
    source["bitstream_url"] = (
        "https://edoc.rki.de/bitstream/handle/176904/999999999/source.pdf?sequence=1"
    )
    source["bitstream_id"] = bitstream_identity(source["bitstream_url"]).bitstream_id
    with pytest.raises(ValueError, match="Source-Bitstream"):
        _build(sources=(source,), documents=(), conversions=(), storage=())


def test_graph_rejects_stale_embedded_rights_decision() -> None:
    source = _source()
    source["decision_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="Rechteentscheidung.*veraltet"):
        _build(sources=(source,))


def _second_bitstream() -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
    url = "https://edoc.rki.de/bitstream/handle/176904/900000001/source.pdf?sequence=3"
    bitstream = bitstream_identity(url)
    source = deepcopy(_source())
    source.update(
        bitstream_id=bitstream.bitstream_id,
        bitstream_url=url,
        bitstream_version=3,
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
        bitstream_version=3,
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


def test_graph_rejects_storage_object_id_with_conflicting_hashes() -> None:
    pdf_reference, markdown_reference = _storage()
    for reference in (pdf_reference, markdown_reference):
        reference["storage_backend"] = "release"
        reference["storage_object_id"] = "release:shared-object"

    with pytest.raises(ValueError, match="Storage-Objekt-ID.*widersprüchliche Hashes"):
        _build(storage=(pdf_reference, markdown_reference))


def test_graph_rejects_public_reference_without_public_visibility() -> None:
    pdf_reference, markdown_reference = _storage()
    pdf_reference["public_reference"] = "https://example.test/archive.pdf"

    with pytest.raises(ValueError, match="public visibility"):
        _build(storage=(pdf_reference, markdown_reference))


def test_graph_enforces_rights_visibility_matrix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.rki_pipeline import rights

    register = tmp_path / "rights-register.yml"
    register.write_text(
        "schema_version: 1\n"
        "decisions:\n"
        f'  - source_id: "{SOURCE_ID}"\n'
        f'    source_sha256: "{SOURCE_SHA256}"\n'
        '    state: "internal_only"\n'
        '    basis: "Synthetic internal-only manifest test"\n'
        '    reviewed_by: "Test Reviewer"\n'
        '    reviewed_at: "2026-08-03T16:00:00Z"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rights, "_canonical_authority_source", register.resolve)
    authorizer = _authorizer()
    decision = rights.resolve_rights(
        SOURCE_ID,
        SOURCE_SHA256,
        authority=authorizer.authority,
        policy=authorizer.policy,
    )
    source = _source()
    source["decision_sha256"] = decision.decision_sha256
    source["rights"] = {
        "basis": decision.basis,
        "reviewed_at": decision.reviewed_at,
        "reviewed_by": decision.reviewed_by,
        "state": decision.state.value,
    }
    pdf_reference, markdown_reference = _storage()
    for reference in (pdf_reference, markdown_reference):
        reference["decision_sha256"] = decision.decision_sha256
        reference["rights_state"] = "internal_only"
        reference["visibility"] = "internal"

    graph = _build(sources=(source,), storage=(pdf_reference, markdown_reference))
    assert len(graph.storage_references) == 2

    for reference in (pdf_reference, markdown_reference):
        reference["visibility"] = "public"

    with pytest.raises(ValueError, match="Storage-Referenz verletzt aktuelle Rechtepolicy"):
        _build(sources=(source,), storage=(pdf_reference, markdown_reference))


def test_stage_writer_rejects_symlinked_collection_directory(tmp_path: Path) -> None:
    import os

    from scripts.rki_pipeline.manifests import _write_stage_file

    stage = tmp_path / "stage"
    outside = tmp_path / "outside"
    stage.mkdir()
    outside.mkdir()
    (stage / "Quellen").symlink_to(outside, target_is_directory=True)
    stage_fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with pytest.raises(ValueError, match="Symlink"):
            _write_stage_file(stage_fd, "Quellen/manifest.jsonl", b"{}\n")
    finally:
        os.close(stage_fd)
    assert list(outside.iterdir()) == []


def _write_rendered(root: Path, files: tuple[tuple[str, bytes], ...]) -> None:
    for relative, payload in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _rewrite_catalog_descriptor(root: Path, relative: str) -> None:
    catalog_path = root / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    payload = (root / relative).read_bytes()
    descriptor = next(item for item in catalog["collections"] if item["path"] == relative)
    descriptor["bytes"] = len(payload)
    descriptor["sha256"] = hashlib.sha256(payload).hexdigest()
    descriptor["count"] = len(payload.splitlines())
    catalog_path.write_text(stable_json_dumps(catalog), encoding="utf-8")


def test_catalog_rendering_is_canonical_and_order_independent() -> None:
    from scripts.rki_pipeline.manifests import render_manifest_catalog

    second_source, second_document, second_storage = _second_bitstream()
    first = render_manifest_catalog(
        _build(
            sources=(_source(), second_source),
            documents=(_document(), second_document),
            storage=(*_storage(), second_storage),
        )
    )
    second = render_manifest_catalog(
        _build(
            sources=(second_source, _source()),
            documents=(second_document, _document()),
            storage=(second_storage, *_storage()),
        )
    )

    assert first == second
    assert tuple(path for path, _payload in first.files) == (
        "Quellen/manifest.jsonl",
        "Dokumente/manifest.jsonl",
        "Konvertierungen/manifest.jsonl",
        "Storage/manifest.jsonl",
        "catalog.json",
    )
    for relative, payload in first.files[:-1]:
        assert payload.endswith(b"\n")
        assert b"\n\n" not in payload
        for line in payload.splitlines():
            value = json.loads(line)
            assert line + b"\n" == stable_json_dumps(value, indent=None).encode()
    assert first.catalog_sha256 == hashlib.sha256(first.files[-1][1]).hexdigest()


def test_catalog_loader_rejects_blank_noncanonical_extra_symlink_and_drift(
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline.manifests import (
        ManifestCatalogError,
        load_manifest_catalog,
        render_manifest_catalog,
    )

    rendered = render_manifest_catalog(_build())

    blank = tmp_path / "blank"
    _write_rendered(blank, rendered.files)
    source_path = blank / "Quellen/manifest.jsonl"
    source_path.write_bytes(source_path.read_bytes() + b"\n")
    with pytest.raises(ManifestCatalogError, match="Leerzeile"):
        load_manifest_catalog(blank)

    noncanonical = tmp_path / "noncanonical"
    _write_rendered(noncanonical, rendered.files)
    source_path = noncanonical / "Quellen/manifest.jsonl"
    value = json.loads(source_path.read_text(encoding="utf-8"))
    source_path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    _rewrite_catalog_descriptor(noncanonical, "Quellen/manifest.jsonl")
    with pytest.raises(ManifestCatalogError, match="nicht kanonisch"):
        load_manifest_catalog(noncanonical)

    extra = tmp_path / "extra"
    _write_rendered(extra, rendered.files)
    (extra / "extra.json").write_text("{}\n", encoding="utf-8")
    with pytest.raises(ManifestCatalogError, match="unregistriert"):
        load_manifest_catalog(extra)

    linked = tmp_path / "linked"
    _write_rendered(linked, rendered.files)
    (linked / "Quellen/manifest.jsonl").unlink()
    (linked / "Quellen/manifest.jsonl").symlink_to(rendered.files[0][0])
    with pytest.raises(ManifestCatalogError, match="Symlink"):
        load_manifest_catalog(linked)

    drift = tmp_path / "drift"
    _write_rendered(drift, rendered.files)
    catalog = json.loads((drift / "catalog.json").read_text(encoding="utf-8"))
    catalog["collections"][0]["bytes"] += 1
    (drift / "catalog.json").write_text(stable_json_dumps(catalog), encoding="utf-8")
    with pytest.raises(ManifestCatalogError, match="Katalog.*driftet"):
        load_manifest_catalog(drift)


def test_catalog_loader_rejects_malformed_oversized_and_graph_drift(tmp_path: Path) -> None:
    from scripts.rki_pipeline.manifests import (
        MAX_MANIFEST_LINE_BYTES,
        ManifestCatalogError,
        load_manifest_catalog,
        render_manifest_catalog,
    )

    rendered = render_manifest_catalog(_build())

    malformed = tmp_path / "malformed"
    _write_rendered(malformed, rendered.files)
    (malformed / "Quellen/manifest.jsonl").write_bytes(b"{\n")
    with pytest.raises(ManifestCatalogError, match="JSON"):
        load_manifest_catalog(malformed)

    oversized = tmp_path / "oversized"
    _write_rendered(oversized, rendered.files)
    (oversized / "Quellen/manifest.jsonl").write_bytes(
        b"{" + b" " * MAX_MANIFEST_LINE_BYTES + b"}\n"
    )
    with pytest.raises(ManifestCatalogError, match="Zeilenlimit"):
        load_manifest_catalog(oversized)

    graph_drift = tmp_path / "graph-drift"
    _write_rendered(graph_drift, rendered.files)
    conversion_path = graph_drift / "Konvertierungen/manifest.jsonl"
    conversion = json.loads(conversion_path.read_text(encoding="utf-8"))
    conversion["source_sha256"] = "9" * 64
    conversion_path.write_text(
        stable_json_dumps(conversion, indent=None),
        encoding="utf-8",
    )
    _rewrite_catalog_descriptor(graph_drift, "Konvertierungen/manifest.jsonl")
    with pytest.raises(ValueError, match="Fingerprint|Source-SHA"):
        load_manifest_catalog(graph_drift)


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b'\xef\xbb\xbf{"schema_version": "1.1.0"}\n', "BOM"),
        (b'{"schema_version": "1.1.0", "schema_version": "1.1.0"}\n', "Doppelter"),
        (b'{"value": NaN}\n', "Nichtendlicher"),
        (b'{"schema_version": "1.1.0"}', "Newline"),
    ),
)
def test_catalog_loader_rejects_ambiguous_json(
    tmp_path: Path,
    payload: bytes,
    message: str,
) -> None:
    from scripts.rki_pipeline.manifests import (
        ManifestCatalogError,
        load_manifest_catalog,
        render_manifest_catalog,
    )

    rendered = render_manifest_catalog(_build())
    _write_rendered(tmp_path, rendered.files)
    (tmp_path / "Quellen/manifest.jsonl").write_bytes(payload)

    with pytest.raises(ManifestCatalogError, match=message):
        load_manifest_catalog(tmp_path)


def test_catalog_materialization_is_atomic_and_noop_preserves_mtimes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.rki_pipeline import manifests

    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    first = manifests.materialize_manifest_catalog(
        _build(),
        temp_root=tmp_path,
        ledger=ledger,
        authorizer=_authorizer(),
    )
    before = {
        path.relative_to(first.root).as_posix(): path.stat().st_mtime_ns
        for path in first.root.rglob("*")
        if path.is_file()
    }
    assert first.changed is True
    assert len(ledger.events) == 5
    assert {event.kind for event in ledger.events} == {EffectKind.TEMP_FILE}

    noop_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    second = manifests.materialize_manifest_catalog(
        _build(),
        temp_root=tmp_path,
        ledger=noop_ledger,
        authorizer=_authorizer(),
    )
    after = {
        path.relative_to(second.root).as_posix(): path.stat().st_mtime_ns
        for path in second.root.rglob("*")
        if path.is_file()
    }
    assert second.changed is False
    assert noop_ledger.events == []
    assert after == before

    original = {path: (first.root / path).read_bytes() for path, _ in first.rendered.files}
    pdf_reference, markdown_reference = _storage()
    pdf_reference["bytes"] += 1
    changed = _build(storage=(pdf_reference, markdown_reference))
    real_write = manifests._write_stage_file
    calls = 0

    def fail_third_write(stage, relative, payload):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("synthetic stage failure")
        return real_write(stage, relative, payload)

    monkeypatch.setattr(manifests, "_write_stage_file", fail_third_write)
    failed_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    with pytest.raises(OSError, match="synthetic stage failure"):
        manifests.materialize_manifest_catalog(
            changed,
            temp_root=tmp_path,
            ledger=failed_ledger,
            authorizer=_authorizer(),
        )
    assert failed_ledger.events == []
    assert {path: (first.root / path).read_bytes() for path, _ in first.rendered.files} == original


def test_catalog_materialization_replaces_corrupt_snapshot(tmp_path: Path) -> None:
    from scripts.rki_pipeline import manifests

    first = manifests.materialize_manifest_catalog(
        _build(),
        temp_root=tmp_path,
        ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path),
        authorizer=_authorizer(),
    )
    (first.root / "Quellen/manifest.jsonl").write_bytes(b"corrupt\n")
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    repaired = manifests.materialize_manifest_catalog(
        _build(),
        temp_root=tmp_path,
        ledger=ledger,
        authorizer=_authorizer(),
    )

    assert repaired.changed is True
    assert repaired.rendered == first.rendered
    assert len(ledger.events) == 5
    assert manifests.load_manifest_catalog(repaired.root, authorizer=_authorizer()).rendered == first.rendered


def test_concurrent_catalog_materializers_are_serialized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time

    from scripts.rki_pipeline import manifests

    second_pdf, second_markdown = _storage()
    second_pdf["bytes"] += 1
    graphs = (_build(), _build(storage=(second_pdf, second_markdown)))
    real_write = manifests._write_stage_file
    entered = threading.Event()
    release = threading.Event()
    active = 0
    maximum_active = 0
    guard = threading.Lock()

    def tracked_write(stage_fd, relative, payload):
        nonlocal active, maximum_active
        with guard:
            active += 1
            maximum_active = max(maximum_active, active)
            first = not entered.is_set()
            entered.set()
        try:
            if first:
                assert release.wait(timeout=2)
            return real_write(stage_fd, relative, payload)
        finally:
            with guard:
                active -= 1

    def publish(graph):
        ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
        result = manifests.materialize_manifest_catalog(
            graph,
            temp_root=tmp_path,
            ledger=ledger,
            authorizer=_authorizer(),
        )
        return result, ledger

    monkeypatch.setattr(manifests, "_write_stage_file", tracked_write)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(publish, graphs[0])
        assert entered.wait(timeout=2)
        second = executor.submit(publish, graphs[1])
        time.sleep(0.05)
        release.set()
        results = (first.result(timeout=5), second.result(timeout=5))

    assert maximum_active == 1
    assert all(result.changed for result, _ledger in results)
    assert all(len(ledger.events) == 5 for _result, ledger in results)
    loaded = manifests.load_manifest_catalog(
        results[-1][0].root,
        authorizer=_authorizer(),
    )
    assert loaded.rendered == manifests.render_manifest_catalog(graphs[1])
    assert not any(".staging-" in path.name for path in tmp_path.rglob("*"))
    assert not any(path.name.endswith(".backup") for path in tmp_path.rglob("*"))


def test_offline_manifest_fixture_is_valid() -> None:
    from scripts.validate_manifests import validate

    validate(Path(__file__).parent / "fixtures" / "manifests")


@pytest.mark.parametrize("workflow", ("p00-baseline.yml", "rki-pipeline.yml"))
def test_manifest_validation_blocks_repository_workflows(workflow: str) -> None:
    root = Path(__file__).resolve().parents[1]
    text = (root / ".github" / "workflows" / workflow).read_text(encoding="utf-8")

    assert "python3 scripts/validate_manifests.py --root tests/fixtures/manifests" in text
    assert "tests/test_manifests.py" in text
