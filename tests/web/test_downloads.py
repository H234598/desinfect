from __future__ import annotations

from copy import deepcopy
from html.parser import HTMLParser
import json
import re
from dataclasses import replace
from pathlib import Path
import shutil
from types import MappingProxyType
from urllib.parse import urljoin

import pytest
import yaml

import scripts.web.build_docs as build_docs_module

from scripts.rki_pipeline.documents import bitstream_identity
from scripts.rki_pipeline.manifests import ManifestGraph, render_manifest_catalog
from scripts.rki_pipeline.rights import (
    ApprovalKey,
    ComponentsState,
    PublicationMode,
    RightsAction,
    RightsAttribution,
    RightsDecision,
    RightsRegister,
    RightsPolicyError,
    RightsState,
    decision_sha256,
    load_authority_register,
    load_fixture_rights_authority,
    load_rights_policy,
)
from scripts.rki_pipeline.storage.base import StorageBackend
from scripts.web.build_downloads import (
    DOWNLOAD_MARKER,
    DownloadInputs,
    DownloadProjectionError,
    _artifact_rows,
    project_downloads,
    render_download_page,
    validate_origin_url,
)
from scripts.web.build_site import SiteBuildError, build_site
from tests.test_manifests import _conversion, _document, _source, _storage


SOURCE_ID = "rki:176904/7"
SOURCE_SHA256 = "a" * 64
BITSTREAM_URL = "https://edoc.rki.de/bitstream/handle/176904/7/source.pdf"
BITSTREAM_ID = bitstream_identity(BITSTREAM_URL).bitstream_id
DOCUMENT_ID = "rki-176904-7-v1"
CONVERSION_ID = "conv-" + "c" * 64
SOURCE_ONLY_TITLE_TOKEN = "SOURCE-ONLY-TITLE-4BC451"
SOURCE_ONLY_BIBLIOGRAPHY_DATE = "1996-03-22"
BODY_TOKEN = "BODY-SECRET-9E127A"
OCR_TOKEN = "OCR-SECRET-6C40E1"
TOKEN_QUERY = "token=TOKEN-SECRET-861B3F"
OLD_DOWNLOAD_HREF = "https://downloads.example.test/old-download-51B9F2"
CANONICAL_LICENSE_URL = "https://creativecommons.org/licenses/by/4.0/"


class _PublishedDownloadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []
        self.srcs: list[str] = []
        self.text: list[str] = []
        self.statuses: list[dict[str, str]] = []
        self.preview_tags: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        if "href" in attributes:
            self.hrefs.append(attributes["href"])
        if "src" in attributes:
            self.srcs.append(attributes["src"])
        if attributes.get("role") == "status":
            self.statuses.append(attributes)
        if tag in {"iframe", "object", "embed", "img"}:
            self.preview_tags.append(tag)

    def handle_data(self, data: str) -> None:
        self.text.append(data)


@pytest.fixture
def strict_repo(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2]
    target = tmp_path / "strict-repo"
    target.mkdir()
    for name in ("content", "config", "web"):
        shutil.copytree(source / name, target / name)
    for name in ("mkdocs.yml", "status.json"):
        shutil.copy2(source / name, target / name)
    (target / "research").mkdir()
    shutil.copy2(
        source / "research/rights-register.yml",
        target / "research/rights-register.yml",
    )
    return target


def _decision(
    *,
    mode: PublicationMode = PublicationMode.MATERIALIZED,
    state: RightsState = RightsState.APPROVED,
    source_id: str = SOURCE_ID,
    source_sha256: str = SOURCE_SHA256,
    bitstream_url: str = BITSTREAM_URL,
) -> RightsDecision:
    bitstream_id = bitstream_identity(bitstream_url).bitstream_id
    key = ApprovalKey(source_id, bitstream_url, bitstream_id, source_sha256)
    attribution = RightsAttribution(
        creators=("RKI",),
        attribution_parties=("Robert Koch-Institut",),
        copyright_notice="© RKI",
        license_notice="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        disclaimer_notice="Keine Gewähr.",
        origin_url="https://edoc.rki.de/handle/" + source_id.removeprefix("rki:"),
        prior_change_history=(),
        current_change_notice="Originalgetreue Bereitstellung.",
    )
    draft = RightsDecision(
        approval_key=key,
        state=state,
        mode=mode,
        allowed_actions=(RightsAction.PUBLISH,) if mode is PublicationMode.MATERIALIZED else (),
        components_state=ComponentsState.CLEARED
        if mode is PublicationMode.MATERIALIZED
        else ComponentsState.UNKNOWN,
        attribution=attribution if mode is PublicationMode.MATERIALIZED else None,
        basis="Fixture decision",
        reviewed_by="Fixture reviewer"
        if state in {RightsState.APPROVED, RightsState.TAKEDOWN}
        else None,
        reviewed_at="2026-08-01T00:00:00Z"
        if state in {RightsState.APPROVED, RightsState.TAKEDOWN}
        else None,
        decision_sha256=None,
    )
    return replace(draft, decision_sha256=decision_sha256(draft))


def _fixture_register(tmp_path: Path, *decisions: RightsDecision):
    payload = {
        "schema_version": 2,
        "decisions": [
            {
                "source_id": decision.approval_key.source_id,
                "canonical_url": decision.approval_key.canonical_url,
                "version_or_bitstream": decision.approval_key.version_or_bitstream,
                "source_sha256": decision.approval_key.source_sha256,
                "state": decision.state.value,
                "mode": decision.mode.value,
                "allowed_actions": [action.value for action in decision.allowed_actions],
                "components_state": decision.components_state.value,
                "attribution": None
                if decision.attribution is None
                else {
                    "creators": list(decision.attribution.creators),
                    "attribution_parties": list(decision.attribution.attribution_parties),
                    "copyright_notice": decision.attribution.copyright_notice,
                    "license_notice": decision.attribution.license_notice,
                    "license_url": decision.attribution.license_url,
                    "disclaimer_notice": decision.attribution.disclaimer_notice,
                    "origin_url": decision.attribution.origin_url,
                    "prior_change_history": list(decision.attribution.prior_change_history),
                    "current_change_notice": decision.attribution.current_change_notice,
                },
                "basis": decision.basis,
                "reviewed_by": decision.reviewed_by,
                "reviewed_at": decision.reviewed_at,
            }
            for decision in sorted(
                decisions,
                key=lambda item: (
                    item.approval_key.source_id,
                    item.approval_key.canonical_url,
                    item.approval_key.version_or_bitstream,
                    item.approval_key.source_sha256,
                ),
            )
        ],
    }
    path = tmp_path / "rights.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    authority = load_fixture_rights_authority(path)
    return load_authority_register(authority), load_rights_policy(), decisions


def _decision_payload(decision: RightsDecision) -> dict[str, object]:
    attribution = decision.attribution
    return {
        "source_id": decision.approval_key.source_id,
        "canonical_url": decision.approval_key.canonical_url,
        "version_or_bitstream": decision.approval_key.version_or_bitstream,
        "source_sha256": decision.approval_key.source_sha256,
        "state": decision.state.value,
        "mode": decision.mode.value,
        "allowed_actions": [action.value for action in decision.allowed_actions],
        "components_state": decision.components_state.value,
        "attribution": None
        if attribution is None
        else {
            "creators": list(attribution.creators),
            "attribution_parties": list(attribution.attribution_parties),
            "copyright_notice": attribution.copyright_notice,
            "license_notice": attribution.license_notice,
            "license_url": attribution.license_url,
            "disclaimer_notice": attribution.disclaimer_notice,
            "origin_url": attribution.origin_url,
            "prior_change_history": list(attribution.prior_change_history),
            "current_change_notice": attribution.current_change_notice,
        },
        "basis": decision.basis,
        "reviewed_by": decision.reviewed_by,
        "reviewed_at": decision.reviewed_at,
    }


def _strict_decision(mode: PublicationMode) -> RightsDecision:
    source = _source()
    state = {
        PublicationMode.MATERIALIZED: RightsState.APPROVED,
        PublicationMode.SOURCE_ONLY: RightsState.METADATA_ONLY,
        PublicationMode.ORIGIN_LINK: RightsState.UNKNOWN,
        PublicationMode.REMOVE_ALL: RightsState.TAKEDOWN,
    }[mode]
    return _decision(
        mode=mode,
        state=state,
        source_id=source["source_id"],
        source_sha256=source["sha256"],
        bitstream_url=source["bitstream_url"],
    )


def _write_strict_publication(
    repo: Path,
    mode: PublicationMode,
    *,
    mutation: str | None = None,
) -> tuple[RightsDecision, tuple[dict[str, object], ...]]:
    decision = _strict_decision(mode)
    payload = _decision_payload(decision)
    if mutation == "publish_action":
        draft = replace(
            decision,
            allowed_actions=(),
            attribution=None,
            decision_sha256=None,
        )
        decision = replace(draft, decision_sha256=decision_sha256(draft))
        payload = _decision_payload(decision)
    elif mutation == "publication_mode":
        payload["mode"] = PublicationMode.SOURCE_ONLY.value
    elif mutation == "components_state":
        payload["components_state"] = ComponentsState.BLOCKED.value
    elif mutation == "attribution":
        attribution = payload["attribution"]
        assert isinstance(attribution, dict)
        attribution["creators"] = []
    (repo / "research/rights-register.yml").write_text(
        yaml.safe_dump(
            {"schema_version": 2, "decisions": [payload]},
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    source = deepcopy(_source())
    source.update(
        title=SOURCE_ONLY_TITLE_TOKEN,
        publication_date=SOURCE_ONLY_BIBLIOGRAPHY_DATE,
        etag=BODY_TOKEN,
        decision_sha256=decision.decision_sha256,
    )
    source["rights_evidence"] = {
        **source["rights_evidence"],
        "copyright_notice": OCR_TOKEN,
    }
    source["rights"] = {
        key: value
        for key, value in payload.items()
        if key not in {"source_id", "canonical_url", "version_or_bitstream", "source_sha256"}
    }
    source["rights"]["approval_key"] = {
        "source_id": decision.approval_key.source_id,
        "canonical_url": decision.approval_key.canonical_url,
        "version_or_bitstream": decision.approval_key.version_or_bitstream,
        "source_sha256": decision.approval_key.source_sha256,
    }
    if mutation == "decision_hash":
        source["decision_sha256"] = "f" * 64
    elif mutation == "bitstream_identity":
        source["bitstream_id"] = "rki-bitstream-" + "f" * 64

    references = tuple(deepcopy(reference) for reference in _storage())
    for index, reference in enumerate(references):
        reference.update(
            storage_backend="object",
            storage_object_id=f"secret-object-{index}-A91C7D",
            decision_sha256=decision.decision_sha256,
            rights_state=decision.state.value,
            visibility="public",
            public_reference=(
                f"https://downloads.example.test/materialized-{index}"
                if mode is PublicationMode.MATERIALIZED
                else OLD_DOWNLOAD_HREF
                if index == 0
                else f"https://downloads.example.test/private?{TOKEN_QUERY}"
            ),
        )
    if mutation == "provenance_state":
        references[0]["provenance_state"] = "superseded"
    elif mutation == "visibility":
        references[0]["visibility"] = "internal"
    elif mutation == "storage_decision_hash":
        references[0]["decision_sha256"] = "f" * 64
    elif mutation == "storage_rights_state":
        references[0]["rights_state"] = RightsState.INTERNAL_ONLY.value
    elif mutation == "storage_source_hash":
        references[0]["source_sha256"] = "f" * 64
    elif mutation == "storage_join":
        references[0]["document_id"] = "rki-176904-900000002-v1"
    elif mutation == "storage_backend":
        references[0]["storage_backend"] = "lfs"
    elif mutation == "public_reference":
        references[0]["public_reference"] = (
            "https://downloads.example.test/materialized-0?token=secret"
        )
    document = deepcopy(_document())
    selected = tuple(sorted(references, key=lambda item: item["artifact_id"]))
    conversions = (deepcopy(_conversion()),)
    graph = ManifestGraph((source,), (document,), conversions, selected)
    rendered = render_manifest_catalog(graph)
    root = repo / "rki/Bulletins/Manifeste"
    for relative, data in rendered.files:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return decision, selected


def _build_strict_download_tree(
    repo: Path, mode: PublicationMode
) -> tuple[_PublishedDownloadParser, str, str, tuple[dict[str, object], ...]]:
    _decision_value, references = _write_strict_publication(repo, mode)
    build_site(
        repo,
        check=True,
        dry_run=False,
        strict=True,
        force=False,
        site_url=None,
    )
    html = (repo / "site/Downloads/index.html").read_text(encoding="utf-8")
    parser = _PublishedDownloadParser()
    parser.feed(html)
    parser.close()
    search = json.loads((repo / "site/search/search_index.json").read_text(encoding="utf-8"))
    return parser, html, json.dumps(search, ensure_ascii=False, sort_keys=True), references


def _assert_public_tokens_absent(html: str, search: str, forbidden: tuple[object, ...]) -> None:
    public = html + search
    for token in forbidden:
        assert str(token) not in public


def _site_snapshot(repo: Path) -> dict[str, bytes | None]:
    root = repo / "site"
    snapshot: dict[str, bytes | None] = {}
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink()
        assert path.is_file() or path.is_dir()
        snapshot[path.relative_to(root).as_posix()] = path.read_bytes() if path.is_file() else None
    return snapshot


@pytest.fixture
def fixture_register(tmp_path: Path):
    register, policy, decisions = _fixture_register(tmp_path, _decision())
    return register, policy, decisions[0]


def _graph(
    decision: RightsDecision,
    *,
    backend: StorageBackend = StorageBackend.OBJECT,
    public_reference: str | None = "https://downloads.example.test/object.pdf",
) -> ManifestGraph:
    source = {
        "source_id": SOURCE_ID,
        "bitstream_url": BITSTREAM_URL,
        "bitstream_id": BITSTREAM_ID,
        "sha256": SOURCE_SHA256,
        "decision_sha256": decision.decision_sha256,
        "publication_date": "2026-08-01",
        "title": "Unique bulletin title",
        "source_url": "https://edoc.rki.de/handle/176904/7",
    }
    document = {
        "document_id": DOCUMENT_ID,
        "source_id": SOURCE_ID,
        "bitstream_id": BITSTREAM_ID,
        "publication_date": "2026-08-01",
        "paths": {"pdf": "rki/7.pdf", "markdown": "rki/7.md"},
    }
    common = {
        "storage_backend": backend.value,
        "source_id": SOURCE_ID,
        "source_sha256": SOURCE_SHA256,
        "document_id": DOCUMENT_ID,
        "decision_sha256": decision.decision_sha256,
        "provenance_state": "current",
        "visibility": "public",
        "rights_state": "approved",
        "public_reference": public_reference,
    }
    pdf = {
        **common,
        "artifact_id": "pdf-" + "a" * 64,
        "relative_path": "rki/7.pdf",
        "storage_object_id": "sha256:" + "d" * 64,
        "sha256": "d" * 64,
        "bytes": 12,
        "conversion_id": None,
    }
    markdown = {
        **common,
        "artifact_id": "markdown-" + "b" * 64,
        "relative_path": "rki/7.md",
        "storage_object_id": "sha256:" + "e" * 64,
        "sha256": "e" * 64,
        "bytes": 23,
        "conversion_id": CONVERSION_ID,
        "public_reference": "https://downloads.example.test/object.md",
    }
    return ManifestGraph((source,), (document,), (), (pdf, markdown))


def _storage_change(graph: ManifestGraph, index: int, **changes: object) -> ManifestGraph:
    storage = list(graph.storage_references)
    storage[index] = {**storage[index], **changes}
    return ManifestGraph(graph.sources, graph.documents, graph.conversions, tuple(storage))


def _with_sibling_bitstream(graph: ManifestGraph, decision: RightsDecision) -> ManifestGraph:
    source_id = decision.approval_key.source_id
    document_id = (
        graph.documents[0]["document_id"]
        if source_id == graph.sources[0]["source_id"]
        else "rki-176904-8-v1"
    )
    source = {
        **graph.sources[0],
        "source_id": source_id,
        "source_url": "https://edoc.rki.de/handle/" + source_id.removeprefix("rki:"),
        "bitstream_url": decision.approval_key.canonical_url,
        "bitstream_id": decision.approval_key.version_or_bitstream,
        "sha256": decision.approval_key.source_sha256,
        "decision_sha256": decision.decision_sha256,
        "title": "Sibling bulletin",
    }
    document = {
        **graph.documents[0],
        "document_id": document_id,
        "source_id": source_id,
        "bitstream_id": decision.approval_key.version_or_bitstream,
        "paths": {"pdf": "rki/sibling.pdf", "markdown": "rki/sibling.md"},
    }
    common = {
        **graph.storage_references[0],
        "source_id": source_id,
        "document_id": document_id,
        "source_sha256": decision.approval_key.source_sha256,
        "decision_sha256": decision.decision_sha256,
        "storage_backend": "object",
    }
    pdf = {
        **common,
        "artifact_id": "sibling-pdf-" + "1" * 64,
        "relative_path": "rki/sibling.pdf",
        "sha256": "1" * 64,
        "public_reference": "https://downloads.example.test/sibling.pdf",
    }
    markdown = {
        **common,
        "artifact_id": "sibling-markdown-" + "2" * 64,
        "relative_path": "rki/sibling.md",
        "sha256": "2" * 64,
        "conversion_id": "conv-" + "2" * 64,
        "public_reference": "https://downloads.example.test/sibling.md",
    }
    return ManifestGraph(
        (*graph.sources, source),
        (*graph.documents, document),
        (),
        (*graph.storage_references, pdf, markdown),
    )


@pytest.mark.parametrize("backend", tuple(StorageBackend))
def test_materialized_download_uses_only_public_reference(backend, fixture_register) -> None:
    register, policy, decision = fixture_register
    row = project_downloads(
        _graph(decision, backend=backend), register=register, policy=policy
    ).rows[0]
    assert row.mode is PublicationMode.MATERIALIZED
    assert row.artifact_url == "https://downloads.example.test/object.pdf"
    assert row.storage_backend is backend
    assert "relative_path" not in repr(row)
    assert "storage_object_id" not in repr(row)


def test_pdf_and_markdown_share_source_approval_but_not_artifact_identity(fixture_register) -> None:
    register, policy, decision = fixture_register
    graph = _graph(decision)
    pdf, markdown = project_downloads(graph, register=register, policy=policy).rows
    assert pdf.approval_key == markdown.approval_key
    assert pdf.approval_key.source_sha256 == graph.sources[0]["sha256"]
    assert pdf.approval_key.source_sha256 not in {pdf.artifact_sha256, markdown.artifact_sha256}
    assert (pdf.artifact_kind, markdown.artifact_kind) == ("pdf", "markdown")
    assert pdf.artifact_id != markdown.artifact_id


def test_artifact_rows_carry_explicit_decision_rights_state(fixture_register) -> None:
    _register, _policy, decision = fixture_register
    graph = _graph(decision)
    row = _artifact_rows(
        graph.sources[0],
        graph.documents[0],
        graph.storage_references,
        key=decision.approval_key,
        attribution=decision.attribution,
        decision_sha256=decision.decision_sha256,
        rights_state=RightsState.INTERNAL_ONLY,
    )[0]
    assert row.rights_state is RightsState.INTERNAL_ONLY


def test_materialized_row_renders_exact_rights_mode_and_availability(fixture_register) -> None:
    register, policy, decision = fixture_register
    row = project_downloads(_graph(decision), register=register, policy=policy).rows[0]
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        DownloadInputs(
            rows=(row,), tombstones=(), public_status={"status": "operational"}, input_hashes={}
        ),
    )
    assert "Rechtestatus: Freigegeben." in rendered
    assert "Veröffentlichungsmodus: Materialisiert." in rendered
    assert "Verfügbarkeit: Download verfügbar." in rendered


def test_materialized_metadata_is_per_artifact_without_independent_source_hash(fixture_register):
    register, policy, decision = fixture_register
    inputs = project_downloads(_graph(decision), register=register, policy=policy)
    rendered = render_download_page(DOWNLOAD_MARKER, inputs)
    parser = _PublishedDownloadParser()
    parser.feed(rendered)
    visible = " ".join(parser.text)
    assert SOURCE_SHA256 not in rendered
    for sha256, size in (("d" * 64, 12), ("e" * 64, 23)):
        assert f"Artifact-SHA-256: {sha256}" in visible
        assert f"Bytes: {size}" in visible
    assert visible.count("Backend: object") == 2


def test_canonical_rights_validation_rejects_publish_with_blocked_components() -> None:
    approved = _decision()
    with pytest.raises(RightsPolicyError, match="components_state"):
        RightsDecision(
            approval_key=approved.approval_key,
            state=approved.state,
            mode=approved.mode,
            allowed_actions=approved.allowed_actions,
            components_state=ComponentsState.BLOCKED,
            attribution=approved.attribution,
            basis=approved.basis,
            reviewed_by=approved.reviewed_by,
            reviewed_at=approved.reviewed_at,
            decision_sha256=None,
        )


def test_source_only_has_zero_payload_hash_or_index_fields(tmp_path: Path) -> None:
    decision = _decision(mode=PublicationMode.SOURCE_ONLY, state=RightsState.METADATA_ONLY)
    register, policy, _ = _fixture_register(tmp_path, decision)
    row = project_downloads(_graph(decision), register=register, policy=policy).rows[0]
    assert row.mode is PublicationMode.SOURCE_ONLY
    assert (row.artifact_url, row.artifact_sha256, row.artifact_bytes, row.storage_backend) == (
        None,
        None,
        None,
        None,
    )
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        DownloadInputs(
            rows=(row,), tombstones=(), public_status={"status": "operational"}, input_hashes={}
        ),
    )
    assert "a" * 64 not in rendered
    assert "storage_object_id" not in rendered


def test_remove_all_has_no_row_or_public_presence(tmp_path: Path) -> None:
    decision = _decision(mode=PublicationMode.REMOVE_ALL, state=RightsState.TAKEDOWN)
    register, policy, _ = _fixture_register(tmp_path, decision)
    inputs = project_downloads(_graph(decision), register=register, policy=policy)
    assert inputs.rows == ()
    assert len(inputs.tombstones) == 2
    rendered = render_download_page(f"<main>{DOWNLOAD_MARKER}</main>", inputs)
    for forbidden in ("Unique bulletin title", "https://edoc.rki.de/handle/176904/7", "sha256:"):
        assert forbidden not in rendered


def test_remove_all_tombstones_only_exact_bitstream_artifact_paths(tmp_path: Path) -> None:
    removed = _decision(mode=PublicationMode.REMOVE_ALL, state=RightsState.TAKEDOWN)
    sibling = _decision(bitstream_url="https://edoc.rki.de/bitstream/handle/176904/7/sibling.pdf")
    register, policy, _ = _fixture_register(tmp_path, removed, sibling)
    graph = _graph(removed)
    sibling_source = {
        **graph.sources[0],
        "bitstream_url": sibling.approval_key.canonical_url,
        "bitstream_id": sibling.approval_key.version_or_bitstream,
        "decision_sha256": sibling.decision_sha256,
    }
    sibling_document = {
        **graph.documents[0],
        "bitstream_id": sibling.approval_key.version_or_bitstream,
        "paths": {"pdf": "rki/7-sibling.pdf", "markdown": "rki/7-sibling.md"},
    }
    sibling_storage = tuple(
        {
            **reference,
            "artifact_id": kind + "-" + digest * 64,
            "relative_path": f"rki/7-sibling.{extension}",
            "public_reference": f"https://downloads.example.test/sibling.{extension}",
            "decision_sha256": sibling.decision_sha256,
        }
        for reference, kind, digest, extension in zip(
            graph.storage_references, ("pdf", "markdown"), ("c", "d"), ("pdf", "md"), strict=True
        )
    )
    inputs = project_downloads(
        ManifestGraph(
            (*graph.sources, sibling_source),
            (*graph.documents, sibling_document),
            graph.conversions,
            (*graph.storage_references, *sibling_storage),
        ),
        register=register,
        policy=policy,
    )

    assert {item.artifact_id for item in inputs.tombstones} == {
        "pdf-" + "a" * 64,
        "markdown-" + "b" * 64,
    }
    assert {row.artifact_id for row in inputs.rows} == {"pdf-" + "c" * 64, "markdown-" + "d" * 64}
    assert all(row.approval_key == sibling.approval_key for row in inputs.rows)


@pytest.mark.parametrize(
    "public_reference",
    (
        "http://downloads.example.test/object.pdf",
        "//downloads.example.test/object.pdf",
        "https://user@downloads.example.test/object.pdf",
        "https://downloads.example.test/object.pdf?token=x",
        "https://downloads.example.test/object.pdf#fragment",
        "https://downloads.example.test/object%2f.pdf",
        "javascript:alert(1)",
        "version https://git-lfs.github.com/spec/v1",
    ),
)
def test_materialized_rejects_unsafe_public_reference(
    fixture_register, public_reference: str
) -> None:
    register, policy, decision = fixture_register
    with pytest.raises(DownloadProjectionError):
        project_downloads(
            _storage_change(_graph(decision), 0, public_reference=public_reference),
            register=register,
            policy=policy,
        )


def test_missing_public_reference_downgrades_only_its_approval_key(tmp_path: Path) -> None:
    first = _decision()
    second = _decision(
        source_id="rki:176904/8",
        bitstream_url="https://edoc.rki.de/bitstream/handle/176904/8/second.pdf",
        source_sha256="3" * 64,
    )
    register, policy, _ = _fixture_register(tmp_path, first, second)
    graph = _with_sibling_bitstream(_graph(first), second)
    graph = _storage_change(graph, 0, public_reference=None)
    inputs = project_downloads(graph, register=register, policy=policy)
    own = [row for row in inputs.rows if row.approval_key == first.approval_key]
    assert len(own) == 1
    assert own[0].mode is PublicationMode.ORIGIN_LINK
    assert own[0].attribution is None
    assert own[0].artifact_url is None
    assert own[0].artifact_sha256 is None
    assert (
        len(
            [
                row
                for row in inputs.rows
                if row.approval_key == second.approval_key
                and row.mode is PublicationMode.MATERIALIZED
            ]
        )
        == 2
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"public_reference": "https://downloads.example.test/unsafe?token=x"},
        {"sha256": "not-a-hash"},
        {"bytes": -1},
        {"storage_backend": "unknown"},
        {"decision_sha256": "f" * 64},
        {"visibility": "internal"},
        {"document_id": "foreign-document"},
        {"source_sha256": "f" * 64},
    ),
)
def test_missing_reference_never_masks_other_invalid_artifacts(fixture_register, changes):
    register, policy, decision = fixture_register
    graph = _storage_change(_graph(decision), 0, public_reference=None)
    graph = _storage_change(graph, 1, **changes)
    with pytest.raises(DownloadProjectionError):
        project_downloads(graph, register=register, policy=policy)


@pytest.mark.parametrize(
    "changes",
    (
        {"sha256": "not-a-hash"},
        {"bytes": -1},
        {"storage_backend": "unknown"},
        {"decision_sha256": "f" * 64},
        {"visibility": "internal"},
        {"document_id": "foreign-document"},
        {"source_sha256": "f" * 64},
    ),
)
def test_missing_reference_itself_still_requires_valid_artifact(fixture_register, changes):
    register, policy, decision = fixture_register
    graph = _storage_change(_graph(decision), 0, public_reference=None, **changes)
    with pytest.raises(DownloadProjectionError):
        project_downloads(graph, register=register, policy=policy)


def test_missing_reference_does_not_hide_cross_key_duplicate_target(tmp_path: Path) -> None:
    first = _decision()
    second = _decision(
        source_id="rki:176904/8",
        bitstream_url="https://edoc.rki.de/bitstream/handle/176904/8/second.pdf",
        source_sha256="3" * 64,
    )
    register, policy, _ = _fixture_register(tmp_path, first, second)
    graph = _with_sibling_bitstream(_graph(first), second)
    graph = _storage_change(graph, 0, public_reference=None)
    graph = _storage_change(graph, 2, public_reference="https://downloads.example.test/object.md")
    with pytest.raises(DownloadProjectionError, match="doppelt"):
        project_downloads(graph, register=register, policy=policy)


def test_materialized_rejects_duplicate_public_reference(fixture_register) -> None:
    register, policy, decision = fixture_register
    graph = _graph(decision)
    with pytest.raises(DownloadProjectionError):
        project_downloads(
            _storage_change(
                graph, 1, public_reference=graph.storage_references[0]["public_reference"]
            ),
            register=register,
            policy=policy,
        )


def test_materialized_rejects_ambiguous_storage_reference(fixture_register) -> None:
    register, policy, decision = fixture_register
    graph = _graph(decision)
    duplicate = {
        **graph.storage_references[0],
        "artifact_id": "other-pdf-" + "f" * 64,
        "public_reference": "https://downloads.example.test/other.pdf",
    }
    with pytest.raises(DownloadProjectionError):
        project_downloads(
            ManifestGraph(
                graph.sources, graph.documents, (), (*graph.storage_references, duplicate)
            ),
            register=register,
            policy=policy,
        )


@pytest.mark.parametrize(
    "storage_changes",
    (
        {"decision_sha256": "f" * 64},
        {"visibility": "internal"},
        {"provenance_state": "legacy_needs_review"},
    ),
)
def test_materialized_rejects_stale_or_nonpublic_storage(fixture_register, storage_changes) -> None:
    register, policy, decision = fixture_register
    with pytest.raises(DownloadProjectionError):
        project_downloads(
            _storage_change(_graph(decision), 0, **storage_changes),
            register=register,
            policy=policy,
        )


def test_materialized_rejects_missing_publish_action(tmp_path: Path) -> None:
    approved = _decision()
    draft = replace(
        approved,
        allowed_actions=(),
        components_state=ComponentsState.UNKNOWN,
        attribution=None,
        decision_sha256=None,
    )
    decision = replace(draft, decision_sha256=decision_sha256(draft))
    register, policy, _ = _fixture_register(tmp_path, decision)
    with pytest.raises(DownloadProjectionError):
        project_downloads(_graph(decision), register=register, policy=policy)


def test_materialized_rejects_stale_source_decision(fixture_register) -> None:
    register, policy, decision = fixture_register
    graph = _graph(decision)
    stale_source = {**graph.sources[0], "decision_sha256": "f" * 64}
    with pytest.raises(DownloadProjectionError):
        project_downloads(
            ManifestGraph((stale_source,), graph.documents, (), graph.storage_references),
            register=register,
            policy=policy,
        )


def test_remove_all_does_not_consume_unsafe_public_data(tmp_path: Path) -> None:
    decision = _decision(mode=PublicationMode.REMOVE_ALL, state=RightsState.TAKEDOWN)
    register, policy, _ = _fixture_register(tmp_path, decision)
    graph = _graph(decision)
    source = {**graph.sources[0], "source_url": "javascript:alert(1)", "title": "<secret>"}
    inputs = project_downloads(
        ManifestGraph((source,), graph.documents, (), graph.storage_references),
        register=register,
        policy=policy,
    )
    assert inputs.rows == ()
    assert len(inputs.tombstones) == 2


@pytest.mark.parametrize(
    ("state", "label"),
    (
        ("not_initialized", "Noch nicht initialisiert"),
        ("operational", "Betriebsbereit"),
        ("degraded", "Eingeschränkt verfügbar"),
        ("blocked", "Blockiert"),
        ("unknown", "Unbekannt"),
    ),
)
def test_rendered_status_names_every_public_state(state: str, label: str) -> None:
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        DownloadInputs(rows=(), tombstones=(), public_status={"status": state}, input_hashes={}),
    )
    assert 'class="publication-status"' in rendered
    assert "Datenstatus" in rendered
    assert label in rendered


@pytest.mark.parametrize("state", ("degraded", "blocked"))
def test_degraded_and_blocked_show_last_success_and_recovery_link(state: str) -> None:
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        DownloadInputs(
            rows=(),
            tombstones=(),
            public_status={
                "status": state,
                "pipeline": {"last_successful_run_at": "2026-08-01T00:00:00Z"},
            },
            input_hashes={},
        ),
    )
    assert "Letzter erfolgreicher Lauf: 2026-08-01T00:00:00Z" in rendered
    assert 'href="../Wartung/Wiederherstellung/"' in rendered


@pytest.mark.parametrize("state", ("degraded", "blocked"))
@pytest.mark.parametrize(
    ("site_url", "recovery_url"),
    (
        ("https://example.test/", "https://example.test/Wartung/Wiederherstellung/"),
        (
            "https://example.test/desinfect/",
            "https://example.test/desinfect/Wartung/Wiederherstellung/",
        ),
    ),
)
def test_strict_recovery_link_preserves_site_prefix(
    strict_repo: Path, state: str, site_url: str, recovery_url: str
) -> None:
    status = json.loads((strict_repo / "status.json").read_text(encoding="utf-8"))
    status["status"] = state
    status["pipeline"]["last_successful_run_at"] = "2026-08-01T00:00:00Z"
    (strict_repo / "status.json").write_text(json.dumps(status), encoding="utf-8")
    build_site(strict_repo, check=True, dry_run=False, strict=True, force=False, site_url=site_url)
    parser = _PublishedDownloadParser()
    parser.feed((strict_repo / "site/Downloads/index.html").read_text(encoding="utf-8"))
    parser.close()
    recovery_links = [href for href in parser.hrefs if href.endswith("Wartung/Wiederherstellung/")]

    assert len(recovery_links) == 1
    assert urljoin(site_url + "Downloads/", recovery_links[0]) == recovery_url
    assert (strict_repo / "site/Wartung/Wiederherstellung/index.html").is_file()
    assert "Letzter erfolgreicher Lauf: 2026-08-01T00:00:00Z" in "".join(parser.text)


def test_render_escapes_title_and_never_uses_raw_license_input(fixture_register) -> None:
    register, policy, decision = fixture_register
    graph = _graph(decision)
    source = {**graph.sources[0], "title": '<img src=x onerror="alert(1)">'}
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        project_downloads(
            ManifestGraph((source,), graph.documents, (), graph.storage_references),
            register=register,
            policy=policy,
        ),
    )
    assert '<img src=x onerror="alert(1)">' not in rendered
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in rendered
    assert 'href="https://creativecommons.org/licenses/by/4.0/"' in rendered


def test_renderer_rejects_direct_malicious_artifact_url(fixture_register) -> None:
    register, policy, decision = fixture_register
    row = project_downloads(_graph(decision), register=register, policy=policy).rows[0]
    with pytest.raises(DownloadProjectionError):
        render_download_page(
            f"<main>{DOWNLOAD_MARKER}</main>",
            DownloadInputs(
                rows=(replace(row, artifact_url="javascript:alert(1)"),),
                tombstones=(),
                public_status={"status": "operational"},
                input_hashes={},
            ),
        )


def test_renderer_rejects_remove_all_row(fixture_register) -> None:
    register, policy, decision = fixture_register
    row = project_downloads(_graph(decision), register=register, policy=policy).rows[0]
    with pytest.raises(DownloadProjectionError):
        render_download_page(
            f"<main>{DOWNLOAD_MARKER}</main>",
            DownloadInputs(
                rows=(replace(row, mode=PublicationMode.REMOVE_ALL),),
                tombstones=(),
                public_status={"status": "operational"},
                input_hashes={},
            ),
        )


def test_renderer_rejects_artifact_data_on_source_only_row(tmp_path: Path) -> None:
    decision = _decision(mode=PublicationMode.SOURCE_ONLY, state=RightsState.METADATA_ONLY)
    register, policy, _decisions = _fixture_register(tmp_path, decision)
    row = project_downloads(_graph(decision), register=register, policy=policy).rows[0]
    with pytest.raises(DownloadProjectionError):
        render_download_page(
            f"<main>{DOWNLOAD_MARKER}</main>",
            DownloadInputs(
                rows=(replace(row, artifact_url="https://downloads.example.test/leak.pdf"),),
                tombstones=(),
                public_status={"status": "operational"},
                input_hashes={},
            ),
        )


def test_project_downloads_requires_injected_rights_inputs(fixture_register) -> None:
    _register, _policy, decision = fixture_register
    with pytest.raises(TypeError):
        project_downloads(_graph(decision))


def test_pdf_only_document_is_projected(fixture_register) -> None:
    register, policy, decision = fixture_register
    graph = _graph(decision)
    document = {**graph.documents[0], "paths": {"pdf": "rki/7.pdf", "markdown": None}}
    inputs = project_downloads(
        ManifestGraph(graph.sources, (document,), (), graph.storage_references[:1]),
        register=register,
        policy=policy,
    )
    assert [row.artifact_kind for row in inputs.rows] == ["pdf"]


def test_sibling_bitstreams_bind_only_their_own_artifacts(tmp_path: Path) -> None:
    first = _decision()
    second = _decision(
        source_sha256="b" * 64,
        bitstream_url="https://edoc.rki.de/bitstream/handle/176904/7/other.pdf",
    )
    register, policy, _decisions = _fixture_register(tmp_path, first, second)
    rows = project_downloads(
        _with_sibling_bitstream(_graph(first), second), register=register, policy=policy
    ).rows
    assert {(row.bitstream_id, row.artifact_kind) for row in rows} == {
        (first.approval_key.version_or_bitstream, "pdf"),
        (first.approval_key.version_or_bitstream, "markdown"),
        (second.approval_key.version_or_bitstream, "pdf"),
        (second.approval_key.version_or_bitstream, "markdown"),
    }


def test_source_only_rows_sort_by_bitstream_not_input_order(tmp_path: Path) -> None:
    first = _decision(mode=PublicationMode.SOURCE_ONLY, state=RightsState.METADATA_ONLY)
    second = _decision(
        mode=PublicationMode.SOURCE_ONLY,
        state=RightsState.METADATA_ONLY,
        source_sha256="b" * 64,
        bitstream_url="https://edoc.rki.de/bitstream/handle/176904/7/other.pdf",
    )
    register, policy, _decisions = _fixture_register(tmp_path, first, second)
    first_graph = _graph(first)
    sibling = _with_sibling_bitstream(first_graph, second)
    rows = project_downloads(
        ManifestGraph(tuple(reversed(sibling.sources)), (), (), ()),
        register=register,
        policy=policy,
    ).rows
    assert [row.bitstream_id for row in rows] == sorted(
        [first.approval_key.version_or_bitstream, second.approval_key.version_or_bitstream]
    )


@pytest.mark.parametrize("state", ("degraded", "blocked"))
def test_degraded_and_blocked_without_success_name_missing_success(state: str) -> None:
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        DownloadInputs(rows=(), tombstones=(), public_status={"status": state}, input_hashes={}),
    )
    assert "Kein erfolgreicher Lauf dokumentiert." in rendered


def test_cross_source_duplicate_public_reference_is_rejected(tmp_path: Path) -> None:
    first = _decision()
    second = _decision(
        source_id="rki:176904/8",
        source_sha256="b" * 64,
        bitstream_url="https://edoc.rki.de/bitstream/handle/176904/8/source.pdf",
    )
    register, policy, _decisions = _fixture_register(tmp_path, first, second)
    graph = _with_sibling_bitstream(_graph(first), second)
    duplicate = _storage_change(
        graph,
        2,
        public_reference=graph.storage_references[0]["public_reference"],
    )
    with pytest.raises(DownloadProjectionError):
        project_downloads(duplicate, register=register, policy=policy)


def test_origin_link_has_no_artifact_fields(tmp_path: Path) -> None:
    decision = _decision(mode=PublicationMode.ORIGIN_LINK, state=RightsState.UNKNOWN)
    register, policy, _decisions = _fixture_register(tmp_path, decision)
    row = project_downloads(_graph(decision), register=register, policy=policy).rows[0]
    assert row.mode is PublicationMode.ORIGIN_LINK
    assert (row.artifact_url, row.artifact_sha256, row.artifact_bytes, row.storage_backend) == (
        None,
        None,
        None,
        None,
    )


@pytest.mark.parametrize(
    ("mode", "state", "rights_label", "mode_label", "availability"),
    (
        (
            PublicationMode.SOURCE_ONLY,
            RightsState.METADATA_ONLY,
            "Nur Metadaten",
            "Nur Quelle",
            "Beim RKI verfügbar",
        ),
        (
            PublicationMode.ORIGIN_LINK,
            RightsState.UNKNOWN,
            "Ungeklärt",
            "Origin-Link",
            "Beim RKI verfügbar",
        ),
    ),
)
def test_nonmaterialized_rows_carry_exact_rights_state_and_text(
    tmp_path: Path,
    mode: PublicationMode,
    state: RightsState,
    rights_label: str,
    mode_label: str,
    availability: str,
) -> None:
    decision = _decision(mode=mode, state=state)
    register, policy, _decisions = _fixture_register(tmp_path, decision)
    row = project_downloads(_graph(decision), register=register, policy=policy).rows[0]
    assert row.rights_state is state
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        DownloadInputs(
            rows=(row,), tombstones=(), public_status={"status": "operational"}, input_hashes={}
        ),
    )
    assert f"Rechtestatus: {rights_label}." in rendered
    assert f"Veröffentlichungsmodus: {mode_label}." in rendered
    assert f"Verfügbarkeit: {availability}." in rendered


def test_missing_rights_decision_fails_closed(fixture_register) -> None:
    _register, policy, decision = fixture_register
    with pytest.raises(DownloadProjectionError):
        project_downloads(
            _graph(decision), register=RightsRegister(schema_version=2, entries=()), policy=policy
        )


def test_blocked_components_fail_closed(tmp_path: Path) -> None:
    approved = _decision()
    draft = replace(
        approved,
        allowed_actions=(),
        components_state=ComponentsState.BLOCKED,
        attribution=None,
        decision_sha256=None,
    )
    blocked = replace(draft, decision_sha256=decision_sha256(draft))
    register, policy, _decisions = _fixture_register(tmp_path, blocked)
    with pytest.raises(DownloadProjectionError):
        project_downloads(_graph(blocked), register=register, policy=policy)


@pytest.mark.parametrize(
    ("mode", "state"),
    (
        (PublicationMode.SOURCE_ONLY, RightsState.METADATA_ONLY),
        (PublicationMode.ORIGIN_LINK, RightsState.UNKNOWN),
    ),
)
def test_nonmaterialized_downgrade_strips_payload_secrets(
    tmp_path: Path, mode: PublicationMode, state: RightsState
) -> None:
    decision = _decision(mode=mode, state=state)
    register, policy, _decisions = _fixture_register(tmp_path, decision)
    graph = _graph(decision)
    source = {
        **graph.sources[0],
        "title": "Permitted bibliography title",
        "body": "BODY_SECRET",
        "ocr_text": "OCR_SECRET",
        "snippet": "SNIPPET_SECRET",
        "bibliography": "BIB_SECRET",
    }
    storage = tuple(
        {
            **reference,
            "sha256": "f" * 64,
            "index_text": "INDEX_SECRET",
            "payload": "PAYLOAD_SECRET",
        }
        for reference in graph.storage_references
    )
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        project_downloads(
            ManifestGraph((source,), graph.documents, (), storage), register=register, policy=policy
        ),
    )
    assert ("Permitted bibliography title" in rendered) is (mode is PublicationMode.SOURCE_ONLY)
    for secret in (
        "BODY_SECRET",
        "OCR_SECRET",
        "SNIPPET_SECRET",
        "BIB_SECRET",
        "INDEX_SECRET",
        "PAYLOAD_SECRET",
        "f" * 64,
    ):
        assert secret not in rendered


@pytest.mark.parametrize("page", ("<main></main>", f"{DOWNLOAD_MARKER}{DOWNLOAD_MARKER}"))
def test_renderer_requires_exactly_one_marker(page: str) -> None:
    with pytest.raises(DownloadProjectionError):
        render_download_page(
            page,
            DownloadInputs(
                rows=(), tombstones=(), public_status={"status": "operational"}, input_hashes={}
            ),
        )


def test_remove_all_strips_real_payload_secrets(tmp_path: Path) -> None:
    decision = _decision(mode=PublicationMode.REMOVE_ALL, state=RightsState.TAKEDOWN)
    register, policy, _decisions = _fixture_register(tmp_path, decision)
    graph = _graph(decision)
    source = {
        **graph.sources[0],
        "title": "OCR_SECRET_TOKEN bibliography-token",
        "ocr_text": "OCR_SECRET_TOKEN",
        "bibliography": "bibliography-token",
    }
    storage = tuple(
        {**reference, "index_text": "OCR_SECRET_TOKEN bibliography-token"}
        for reference in graph.storage_references
    )
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        project_downloads(
            ManifestGraph((source,), graph.documents, (), storage), register=register, policy=policy
        ),
    )
    assert "OCR_SECRET_TOKEN" not in rendered
    assert "bibliography-token" not in rendered


@pytest.mark.parametrize(
    "value",
    (
        "https://www.rki.de/handle/176904/7",
        "https://edoc.rki.de.example/handle/176904/7",
        "https://edoc.rki.de:443/handle/176904/7",
        "https://user@edoc.rki.de/handle/176904/7",
        "https://edoc.rki.de/handle/176904/7?x=1",
        "https://edoc.rki.de/handle/176904/7#x",
        "https://edoc.rki.de\\/handle/176904/7",
        "https://edoc.rki.de/handle%2f176904/7",
        "https://edoc.rki.de/bitstream/handle/176904/7/source.pdf",
        "https://edoc.rki.de/handle/176904/8",
        "javascript:alert(1)",
        "data:text/html,x",
        "file:///tmp/x",
        "//edoc.rki.de/handle/176904/7",
    ),
)
def test_origin_url_rejects_noncanonical_values(value: str) -> None:
    with pytest.raises(DownloadProjectionError):
        validate_origin_url(value, source_id=SOURCE_ID)


def test_origin_url_accepts_exact_source_handle() -> None:
    assert (
        validate_origin_url("https://edoc.rki.de/handle/176904/7", source_id=SOURCE_ID)
        == "https://edoc.rki.de/handle/176904/7"
    )


def test_bound_download_inputs_freeze_status_and_hash_mappings(fixture_register) -> None:
    register, policy, decision = fixture_register
    projected = project_downloads(
        _graph(decision),
        {"status": "operational", "pipeline": {"last_successful_run_at": None}},
        register=register,
        policy=policy,
    )

    frozen = build_docs_module.freeze_download_inputs(
        projected,
        {"status.json": "a" * 64},
    )

    assert isinstance(frozen.public_status, MappingProxyType)
    assert isinstance(frozen.public_status["pipeline"], MappingProxyType)
    assert isinstance(frozen.input_hashes, MappingProxyType)
    with pytest.raises(TypeError):
        frozen.public_status["pipeline"]["last_successful_run_at"] = "changed"
    with pytest.raises(TypeError):
        frozen.input_hashes["status.json"] = "b" * 64


def test_public_rights_page_documents_closed_review_and_publication_contract() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "content/Methodik/Rechte.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for mode in ("remove_all", "origin_link", "source_only", "materialized"):
        assert f"`{mode}`" in text
    assert "(`source_id`, `canonical_url`, `version_or_bitstream`, `source_sha256`)" in text
    for action in (
        "fetch",
        "cache",
        "hash",
        "ocr",
        "extract_text",
        "thumbnail",
        "index_text",
        "publish",
    ):
        assert f"`{action}`" in text
    for required in (
        "Fehlt der exakte Treffer, gilt `unknown` mit `origin_link`.",
        "keine Freigabe nach Host, Serie, Jahr oder URL-Muster",
        "§ 5 UrhG",
        "§ 12a EGovG",
        "manuell",
        "Attribution",
        "Komponentenprüfung",
        "Downgrade",
        "Takedown",
        "P16",
        "keine Rechtsberatung",
        "keine automatische Serienfreigabe",
        "RKI-Originallink-Regel",
    ):
        assert required in normalized
    for url in (
        "https://www.gesetze-im-internet.de/urhg/__5.html",
        "https://www.gesetze-im-internet.de/egovg/__12a.html",
        "https://creativecommons.org/licenses/by/4.0/deed.de",
        "https://creativecommons.org/licenses/by/4.0/legalcode.de",
    ):
        assert f"]({url})" in text


def test_historical_page_keeps_source_status_and_later_review_separate() -> None:
    root = Path(__file__).resolve().parents[2]
    text = (root / "content/Methodik/Historische-Einordnung.md").read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    for heading in (
        "## Historische Quelle",
        "## Technischer Rechte-/Verfügbarkeitszustand",
        "## Spätere fachliche Einordnung P16",
    ):
        assert heading in text
    for boundary in (
        "Eine historische Quelle ist keine heutige Empfehlung.",
        "Diese Seite enthält keine medizinische Bewertung.",
        "keine Anleitung, Konzentration oder Wirksamkeitsaussage",
        "Aus historischen Quellen wird keine automatische Freigabe abgeleitet.",
    ):
        assert boundary in normalized


def test_operations_docs_cover_download_build_matrix_and_accessibility_boundary() -> None:
    root = Path(__file__).resolve().parents[2]
    build_doc = (root / "docs/Wartung/WEB-BUILD.md").read_text(encoding="utf-8")
    ux_doc = (root / "docs/Wartung/UX.md").read_text(encoding="utf-8")

    for required in (
        "## 11. Downloadprojektion",
        "### Eingaben und Snapshotbindung",
        "vollständig fehlend",
        "teilweise vorhanden",
        "DESINFECT:DOWNLOADS",
        "Vollbuild",
        "Teilpreview",
        "Rollback",
        "Source-Purity",
        "Der integrierte `build_site()`-Vollbuild veröffentlicht Docs und Site als koordinierte Transaktion.",
        "Scheitert diese Transaktion, bleiben beide alten vollständigen Zielbäume aktiv.",
        "`build_docs()` ist ein separater Docs-only-Flow",
    ):
        assert required in build_doc
    assert (
        "ein bereits vollständig veröffentlichter neuer Docs-Baum kann daneben bestehen"
        not in build_doc
    )
    for obsolete in (
        "keine gemeinsame Transaktion über beide Ziele",
        "Normalbuild publiziert zuerst",
        "Beide Ziele sind getrennte atomare Transaktionen",
        "Docs und Site bleiben zwei getrennte atomare Veröffentlichungen",
    ):
        assert obsolete not in build_doc
    for required in (
        "## Downloads, Status und Rechte",
        "Betriebsbereit",
        "Eingeschränkt verfügbar",
        "Blockiert",
        "Unbekannt",
        "PDF herunterladen",
        "Quell-Markdown herunterladen",
        "Beim RKI öffnen",
        "Tastatur",
        "No-JS",
        "nicht nur durch Farbe",
        "Suchindex",
        "P11.4",
    ):
        assert required in ux_doc


@pytest.mark.parametrize(
    "injected",
    (
        f'<div data-secret="{BODY_TOKEN}"></div>',
        f"<!-- {BODY_TOKEN} -->",
        f'<meta name="secret" content="{BODY_TOKEN}">',
        f'<script type="application/json">"{BODY_TOKEN}"</script>',
    ),
)
def test_leak_guard_covers_raw_html_attributes_and_comments(
    strict_repo: Path, injected: str
) -> None:
    page = strict_repo / "content/Downloads.md"
    page.write_text(page.read_text(encoding="utf-8") + "\n" + injected, encoding="utf-8")
    _parser, html, search, _references = _build_strict_download_tree(
        strict_repo, PublicationMode.ORIGIN_LINK
    )

    with pytest.raises(AssertionError):
        _assert_public_tokens_absent(html, search, (BODY_TOKEN,))


@pytest.mark.parametrize("value", ("1996-02-30", "19960322", "1996-W12-5", "<script>"))
def test_renderer_rejects_invalid_or_noncanonical_publication_date(
    fixture_register, value: str
) -> None:
    register, policy, decision = fixture_register
    inputs = project_downloads(_graph(decision), register=register, policy=policy)
    inputs = replace(inputs, rows=(replace(inputs.rows[0], publication_date=value),))

    with pytest.raises(DownloadProjectionError, match="publication_date"):
        render_download_page(f"<main>{DOWNLOAD_MARKER}</main>", inputs)


def test_source_only_is_visible_but_absent_from_real_strict_search_tree(
    strict_repo: Path,
) -> None:
    parser, html, search, _references = _build_strict_download_tree(
        strict_repo, PublicationMode.SOURCE_ONLY
    )
    visible = " ".join(parser.text)

    assert SOURCE_ONLY_TITLE_TOKEN in visible
    assert f"Erscheinungsdatum: {SOURCE_ONLY_BIBLIOGRAPHY_DATE}" in visible
    _assert_public_tokens_absent(
        "", search, (SOURCE_ONLY_TITLE_TOKEN, SOURCE_ONLY_BIBLIOGRAPHY_DATE)
    )
    index = json.loads(search)
    assert all(not document["location"].startswith("Downloads/") for document in index["docs"])


@pytest.mark.parametrize(
    "mode",
    (PublicationMode.ORIGIN_LINK, PublicationMode.REMOVE_ALL),
)
def test_nonmaterialized_strict_tree_never_leaks_payload_or_storage_tokens(
    strict_repo: Path,
    mode: PublicationMode,
) -> None:
    parser, html, search, references = _build_strict_download_tree(strict_repo, mode)
    _assert_public_tokens_absent(
        html,
        search,
        (
            BODY_TOKEN,
            OCR_TOKEN,
            TOKEN_QUERY,
            OLD_DOWNLOAD_HREF,
            *(
                reference[field]
                for reference in references
                for field in ("sha256", "relative_path", "storage_object_id")
            ),
        ),
    )
    if mode is PublicationMode.ORIGIN_LINK:
        assert "https://edoc.rki.de/handle/176904/900000001" in parser.hrefs
        assert "Veröffentlichungsmodus: Origin-Link." in " ".join(parser.text)
        assert "Rechtestatus: Ungeklärt." in " ".join(parser.text)
        _assert_public_tokens_absent(
            html, search, (SOURCE_ONLY_TITLE_TOKEN, SOURCE_ONLY_BIBLIOGRAPHY_DATE)
        )
    if mode is PublicationMode.REMOVE_ALL:
        _assert_public_tokens_absent(
            html,
            search,
            (
                "https://edoc.rki.de/handle/176904/900000001",
                SOURCE_ONLY_TITLE_TOKEN,
                SOURCE_ONLY_BIBLIOGRAPHY_DATE,
            ),
        )


def test_materialized_real_strict_tree_proves_full_safe_link_conjunction(
    strict_repo: Path,
) -> None:
    parser, html, search, references = _build_strict_download_tree(
        strict_repo, PublicationMode.MATERIALIZED
    )
    visible = " ".join(parser.text)

    source_hash = _strict_decision(PublicationMode.MATERIALIZED).approval_key.source_sha256
    for reference in references:
        assert f"Artifact-SHA-256: {reference['sha256']}" in visible
        assert f"Bytes: {reference['bytes']}" in visible
        assert f"Backend: {reference['storage_backend']}" in visible
        assert reference["sha256"] not in search
        article = next(
            article
            for article in re.findall(r'<article class="download">(.*?)</article>', html, re.S)
            if f'href="{reference["public_reference"]}"' in article
        )
        assert f"Artifact-SHA-256: {reference['sha256']}" in article
        assert f"Bytes: {reference['bytes']}" in article
        assert f"Backend: {reference['storage_backend']}" in article
    assert "PDF herunterladen" in visible
    assert "Quell-Markdown herunterladen" in visible
    assert source_hash not in search
    assert any(reference["sha256"] != source_hash for reference in references)
    _assert_public_tokens_absent(html, search, (BODY_TOKEN, OCR_TOKEN, TOKEN_QUERY))

    assert "https://downloads.example.test/materialized-0" in parser.hrefs
    assert "https://downloads.example.test/materialized-1" in parser.hrefs
    assert f"Erscheinungsdatum: {SOURCE_ONLY_BIBLIOGRAPHY_DATE}" in visible
    assert "Rechtestatus: Freigegeben." in visible
    assert "Veröffentlichungsmodus: Materialisiert." in visible
    assert "Verfügbarkeit: Download verfügbar." in visible
    assert parser.hrefs.count(CANONICAL_LICENSE_URL) == 1
    assert parser.preview_tags == []
    assert len(parser.statuses) == 1
    assert parser.statuses[0]["aria-live"] == "polite"
    assert parser.statuses[0]["aria-labelledby"] == "publication-status-title"
    _assert_public_tokens_absent(
        html,
        search,
        tuple(
            reference[field]
            for reference in references
            for field in ("relative_path", "storage_object_id")
        ),
    )


@pytest.mark.parametrize(
    "mutation",
    (
        "decision_hash",
        "publish_action",
        "publication_mode",
        "components_state",
        "attribution",
        "provenance_state",
        "visibility",
        "storage_decision_hash",
        "storage_rights_state",
        "storage_source_hash",
        "storage_join",
        "storage_backend",
        "public_reference",
        "bitstream_identity",
    ),
)
def test_materialized_strict_build_rejects_each_broken_safety_link_before_publication(
    strict_repo: Path,
    mutation: str,
) -> None:
    _write_strict_publication(strict_repo, PublicationMode.MATERIALIZED)
    build_site(
        strict_repo,
        check=True,
        dry_run=False,
        strict=True,
        force=False,
        site_url=None,
    )
    before = _site_snapshot(strict_repo)

    _write_strict_publication(
        strict_repo,
        PublicationMode.MATERIALIZED,
        mutation=mutation,
    )

    with pytest.raises(SiteBuildError):
        build_site(
            strict_repo,
            check=True,
            dry_run=False,
            strict=True,
            force=True,
            site_url=None,
        )
    assert _site_snapshot(strict_repo) == before


@pytest.mark.parametrize(
    "value",
    (
        "javascript:alert(1)",
        "http://creativecommons.org/licenses/by/4.0/",
        "https://user@creativecommons.org/licenses/by/4.0/",
        "https://creativecommons.org:443/licenses/by/4.0/",
        "https://creativecommons.org/licenses/by/4.0/?token=x",
        "https://creativecommons.org/licenses/by/4.0/#terms",
        "https://creativecommons.org/licenses/by/4.0/%0a",
        "https://creativecommons.org/licenses/by-sa/4.0/",
        "https://creativecommons.org.evil.test/licenses/by/4.0/",
    ),
)
def test_unsafe_license_url_aborts_strict_build_before_html_publication(
    strict_repo: Path,
    value: str,
) -> None:
    _write_strict_publication(strict_repo, PublicationMode.MATERIALIZED)
    build_site(
        strict_repo,
        check=True,
        dry_run=False,
        strict=True,
        force=False,
        site_url=None,
    )
    before = _site_snapshot(strict_repo)
    register = strict_repo / "research/rights-register.yml"
    register.write_text(
        register.read_text(encoding="utf-8").replace(
            CANONICAL_LICENSE_URL,
            value,
        ),
        encoding="utf-8",
    )

    with pytest.raises(SiteBuildError, match="license_url"):
        build_site(
            strict_repo,
            check=True,
            dry_run=False,
            strict=True,
            force=True,
            site_url=None,
        )
    assert _site_snapshot(strict_repo) == before
