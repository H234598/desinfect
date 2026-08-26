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
) -> RightsDecision:
    key = ApprovalKey(SOURCE_ID, BITSTREAM_URL, BITSTREAM_ID, SOURCE_SHA256)
    attribution = RightsAttribution(
        creators=("RKI",),
        attribution_parties=("Robert Koch-Institut",),
        copyright_notice="© RKI",
        license_notice="CC BY 4.0",
        license_url="https://creativecommons.org/licenses/by/4.0/",
        disclaimer_notice="Keine Gewähr.",
        origin_url="https://edoc.rki.de/handle/176904/7",
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


def _fixture_register(tmp_path: Path, decision: RightsDecision):
    payload = {
        "schema_version": 2,
        "decisions": [
            {
                "source_id": SOURCE_ID,
                "canonical_url": BITSTREAM_URL,
                "version_or_bitstream": BITSTREAM_ID,
                "source_sha256": SOURCE_SHA256,
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
        ],
    }
    path = tmp_path / "rights.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    authority = load_fixture_rights_authority(path)
    return load_authority_register(authority), load_rights_policy(), decision


@pytest.fixture
def fixture_register(tmp_path: Path):
    return _fixture_register(tmp_path, _decision())


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
    "state", ("not_initialized", "operational", "degraded", "blocked", "unknown")
)
def test_rendered_status_names_every_public_state(state: str) -> None:
    rendered = render_download_page(
        f"<main>{DOWNLOAD_MARKER}</main>",
        DownloadInputs(rows=(), tombstones=(), public_status={"status": state}, input_hashes={}),
    )
    assert 'class="publication-status"' in rendered
    assert "Datenstatus" in rendered


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
