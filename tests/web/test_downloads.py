from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.rki_pipeline.documents import bitstream_identity
from scripts.rki_pipeline.manifests import ManifestGraph
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


SOURCE_ID = "rki:176904/7"
SOURCE_SHA256 = "a" * 64
BITSTREAM_URL = "https://edoc.rki.de/bitstream/handle/176904/7/source.pdf"
BITSTREAM_ID = bitstream_identity(BITSTREAM_URL).bitstream_id
DOCUMENT_ID = "rki-176904-7-v1"
CONVERSION_ID = "conv-" + "c" * 64


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


@pytest.mark.parametrize(
    "public_reference",
    (
        None,
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
def test_materialized_rejects_missing_or_unsafe_public_reference(
    fixture_register, public_reference: str | None
) -> None:
    register, policy, decision = fixture_register
    with pytest.raises(DownloadProjectionError):
        project_downloads(
            _storage_change(_graph(decision), 0, public_reference=public_reference),
            register=register,
            policy=policy,
        )


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
    assert 'href="/Wartung/Wiederherstellung/"' in rendered


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
    assert "Permitted bibliography title" in rendered
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
