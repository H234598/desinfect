"""Fail-closed source and document manifest builders."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from scripts.rki_grabber.models import (
    ArtifactRecord,
    RecordState,
    RightsMetadata,
    Scope,
)
from scripts.rki_pipeline.source_manifest import (
    ManifestBuildError,
    build_document_manifest,
    build_source_manifest,
    build_source_manifests,
    write_manifest,
)
from scripts.rki_pipeline.io_utils import UnsafePathError, stable_json_dumps
from scripts.rki_pipeline.documents import bitstream_identity
from scripts.rki_pipeline.rights import RightsDecision, RightsState
from scripts.rki_pipeline import rights


def _record(
    *,
    item_handle: str = "176904/12345.2",
    item_url: str = "https://edoc.rki.de/handle/176904/12345.2",
    pdf_url: str = (
        "https://edoc.rki.de/bitstream/handle/176904/12345.2/"
        "issue.pdf?sequence=2"
    ),
    sha256: str = "a" * 64,
    doi: str | None = None,
) -> ArtifactRecord:
    return ArtifactRecord(
        scope=Scope.ISSUES,
        document_id="rki-176904-12345-v2",
        source_id="rki:176904/12345.2",
        version=2,
        item_handle=item_handle,
        item_url=item_url,
        title="Synthetic RKI bulletin",
        publication_date="1996-03-22",
        year=1996,
        doi=doi,
        rights=RightsMetadata(
            label="Synthetic fixture — no publication decision",
            uri="https://example.invalid/synthetic-license",
            copyright_notice="Synthetic RKI fixture",
            open_access=True,
        ),
        pdf_url=pdf_url,
        source_filename="issue.pdf",
        relative_path="Jahre/1996/PDF/issue.pdf",
        state=RecordState.DOWNLOADED,
        bytes=42,
        md5="b" * 32,
        sha256=sha256,
        expected_md5="b" * 32,
        etag='"fixture"',
        last_modified="Fri, 22 Mar 1996 00:00:00 GMT",
    )


def _decision(
    record: ArtifactRecord,
    *,
    state: RightsState = RightsState.UNKNOWN,
    basis: str = "rights_register_no_match",
    reviewed_by: str | None = None,
    reviewed_at: str | None = None,
    decision_sha256: str | None = None,
) -> RightsDecision:
    identity = bitstream_identity(record.pdf_url or (
        "https://edoc.rki.de/bitstream/handle/176904/12345.2/missing.pdf"
    ))
    mode = (
        rights.PublicationMode.MATERIALIZED
        if state is RightsState.APPROVED
        else rights.PublicationMode.ORIGIN_LINK
    )
    draft = RightsDecision(
        approval_key=rights.ApprovalKey(
            source_id=record.source_id,
            canonical_url=identity.canonical_url,
            version_or_bitstream=identity.bitstream_id,
            source_sha256=record.sha256 or "0" * 64,
        ),
        state=state,
        mode=mode,
        allowed_actions=(rights.RightsAction.CACHE,) if state is RightsState.APPROVED else (),
        components_state=rights.ComponentsState.UNKNOWN,
        attribution=None,
        basis=basis,
        reviewed_by=reviewed_by,
        reviewed_at=reviewed_at,
        decision_sha256=None,
    )
    if decision_sha256 == "auto":
        return replace(draft, decision_sha256=rights.decision_sha256(draft))
    if decision_sha256 is not None:
        return _unchecked_decision(draft, decision_sha256=decision_sha256)
    return draft


def _unchecked_decision(
    decision: RightsDecision,
    **overrides: object,
) -> RightsDecision:
    """Forge an invalid instance only to pressure-test downstream guards."""

    values: dict[str, object] = {
        "approval_key": decision.approval_key,
        "state": decision.state,
        "mode": decision.mode,
        "allowed_actions": decision.allowed_actions,
        "components_state": decision.components_state,
        "attribution": decision.attribution,
        "basis": decision.basis,
        "reviewed_by": decision.reviewed_by,
        "reviewed_at": decision.reviewed_at,
        "decision_sha256": decision.decision_sha256,
    }
    source_id = overrides.pop("source_id", None)
    source_sha256 = overrides.pop("source_sha256", None)
    if source_id is not None or source_sha256 is not None:
        key = object.__new__(rights.ApprovalKey)
        object.__setattr__(
            key,
            "source_id",
            decision.source_id if source_id is None else source_id,
        )
        object.__setattr__(key, "canonical_url", decision.approval_key.canonical_url)
        object.__setattr__(
            key,
            "version_or_bitstream",
            decision.approval_key.version_or_bitstream,
        )
        object.__setattr__(
            key,
            "source_sha256",
            decision.source_sha256 if source_sha256 is None else source_sha256,
        )
        values["approval_key"] = key
    values.update(overrides)
    forged = object.__new__(RightsDecision)
    for name, value in values.items():
        object.__setattr__(forged, name, value)
    return forged


def _source_manifest(record: ArtifactRecord, **kwargs: object) -> dict[str, object]:
    return build_source_manifest(record, rights_decision=_decision(record), **kwargs)


def _decision_map(*records: ArtifactRecord) -> dict[rights.ApprovalKey, RightsDecision]:
    decisions = (_decision(record) for record in records if record.sha256 is not None)
    return {decision.approval_key: decision for decision in decisions}


def test_builders_produce_fail_closed_valid_manifests() -> None:
    """Changing record identity, period, paths, or rights mapping must break manifests."""

    record = _record()

    source = _source_manifest(record)
    document = build_document_manifest(
        record,
        markdown_materialized=False,
        superseded_by="rki-176904-12345-v3",
    )

    assert source["schema_version"] == "1.3.0"
    assert source["bitstream_version"] == 2
    assert source["rights"] == {
        "state": "unknown",
        "mode": "origin_link",
        "allowed_actions": [],
        "components_state": "unknown",
        "attribution": None,
        "approval_key": {
            "source_id": "rki:176904/12345.2",
            "canonical_url": (
                "https://edoc.rki.de/bitstream/handle/176904/12345.2/"
                "issue.pdf?sequence=2"
            ),
            "version_or_bitstream": (
                "rki-bitstream-ca16f3bf368deddef0cc580b31c0105db58edcfd486fa689a8860cb8aea67176"
            ),
            "source_sha256": "a" * 64,
        },
        "basis": "rights_register_no_match",
        "reviewed_at": None,
        "reviewed_by": None,
    }
    assert source["decision_sha256"] is None
    assert source["rights_evidence"]["label"] == "Synthetic fixture — no publication decision"
    assert source["rights_evidence"]["open_access"] is True
    assert document == {
        "schema_version": "1.1.0",
        "document_id": "rki-176904-12345-v2",
        "version": 2,
        "source_id": "rki:176904/12345.2",
        "document_type": "gesamtausgabe",
        "publication_date": "1996-03-22",
        "paths": {
            "pdf": (
                "rki/Bulletins/Jahre/1996/PDF/1996-03-22_gesamtausgabe_"
                "rki-176904-12345-v2_rki-bitstream-"
                "ca16f3bf368deddef0cc580b31c0105db58edcfd486fa689a8860cb8aea67176.pdf"
            ),
            "markdown": None,
        },
        "supersedes": "rki-176904-12345-v1",
        "provenance_state": "current",
        "bitstream_id": "rki-bitstream-ca16f3bf368deddef0cc580b31c0105db58edcfd486fa689a8860cb8aea67176",
        "bitstream_version": 2,
        "canonical_periods": {"week": "1996-W12", "month": "1996-03", "year": 1996},
        "superseded_by": "rki-176904-12345-v3",
    }


def test_source_manifest_v1_3_binds_exact_rights_contract() -> None:
    """Current manifests must embed exact revision, mode, actions, and components."""

    record = _record()
    source = _source_manifest(record)
    bitstream = bitstream_identity(record.pdf_url or "")

    assert source["schema_version"] == "1.3.0"
    assert source["rights"]["approval_key"] == {
        "source_id": record.source_id,
        "canonical_url": bitstream.canonical_url,
        "version_or_bitstream": bitstream.bitstream_id,
        "source_sha256": record.sha256,
    }
    assert source["rights"]["mode"] == "origin_link"
    assert source["rights"]["allowed_actions"] == []
    assert source["rights"]["components_state"] == "unknown"
    assert source["rights"]["attribution"] is None


def test_source_manifest_carries_nullable_artifact_record_doi() -> None:
    with_doi = _source_manifest(_record(doi="10.25646/12345.2"))
    without_doi = _source_manifest(_record())

    assert with_doi["schema_version"] == "1.3.0"
    assert with_doi["doi"] == "10.25646/12345.2"
    assert without_doi["doi"] is None


@pytest.mark.parametrize("doi", ("10.25646/12345.2\n", "10.25646/12345\x00.2"))
def test_source_manifest_rejects_doi_control_characters(doi: str) -> None:
    with pytest.raises(ManifestBuildError):
        _source_manifest(_record(doi=doi))


def test_source_manifest_maps_exact_reviewed_rights_decision() -> None:
    """Dropping review provenance or mixing source tuples must break the mapper."""

    record = _record()
    decision = _decision(
        record,
        state=RightsState.APPROVED,
        basis="Reviewed RKI reuse terms",
        reviewed_by="Legal Reviewer",
        reviewed_at="2026-08-03T08:00:00Z",
        decision_sha256="auto",
    )

    source = build_source_manifest(record, rights_decision=decision)

    assert source["rights"]["state"] == "approved"
    assert source["rights"]["mode"] == "materialized"
    assert source["rights"]["allowed_actions"] == ["cache"]
    assert source["rights"]["basis"] == "Reviewed RKI reuse terms"
    assert source["decision_sha256"] == decision.decision_sha256


@pytest.mark.parametrize(
    "decision",
    (
        _unchecked_decision(_decision(_record()), source_id="rki:176904/99999"),
        _unchecked_decision(_decision(_record()), source_sha256="b" * 64),
    ),
)
def test_source_manifest_rejects_rights_decision_for_other_source_bytes(
    decision: RightsDecision,
) -> None:
    """A valid decision for other identity or bytes must never authorize this record."""

    with pytest.raises(ManifestBuildError, match="Rechteentscheidung"):
        build_source_manifest(_record(), rights_decision=decision)


def test_source_manifest_rejects_authorization_without_review_hash() -> None:
    """Caller-constructed approved state without provenance hash must not reach manifest."""

    record = _record()
    decision = _unchecked_decision(
        _decision(record),
        state=RightsState.APPROVED,
        basis="Reviewed RKI reuse terms",
        reviewed_by="Legal Reviewer",
        reviewed_at="2026-08-03T08:00:00Z",
        decision_sha256=None,
    )

    with pytest.raises(ManifestBuildError, match="Rechteentscheidung"):
        build_source_manifest(record, rights_decision=decision)


@pytest.mark.parametrize(
    "decision",
    (
        _unchecked_decision(_decision(_record()), mode="invalid"),
        _unchecked_decision(_decision(_record()), basis=""),
        _unchecked_decision(
            _decision(_record()), decision_sha256="not-a-sha256"
        ),
    ),
)
def test_source_manifest_rejects_noncanonical_decision_fields(
    decision: RightsDecision,
) -> None:
    """Unknown state, empty basis, and malformed hashes never reach manifests."""

    with pytest.raises(ManifestBuildError, match="Rechteentscheidung"):
        build_source_manifest(_record(), rights_decision=decision)


@pytest.mark.parametrize(
    "state",
    (RightsState.INTERNAL_ONLY, RightsState.TAKEDOWN),
)
@pytest.mark.parametrize(
    "missing_field",
    ("reviewed_by", "reviewed_at", "decision_sha256"),
)
def test_source_manifest_requires_complete_review_for_restrictive_decisions(
    state: RightsState,
    missing_field: str,
) -> None:
    """Restrictive reviewed states still require full canonical provenance."""

    record = _record()
    decision = _unchecked_decision(
        _decision(record),
        state=state,
        basis="Reviewed restriction",
        reviewed_by="Legal Reviewer",
        reviewed_at="2026-08-03T08:00:00Z",
        decision_sha256="f" * 64,
    )

    with pytest.raises(ManifestBuildError, match="Rechteentscheidung"):
        build_source_manifest(
            record,
            rights_decision=_unchecked_decision(
                decision,
                **{missing_field: None},
            ),
        )


@pytest.mark.parametrize(
    "record",
    [
        replace(_record(), state=RecordState.PLANNED),
        replace(_record(), pdf_url=None),
        replace(_record(), state=RecordState.ERROR),
        replace(_record(), sha256=None),
        replace(_record(), publication_date=None),
    ],
)
def test_builders_reject_incomplete_or_unmaterialized_records(record: ArtifactRecord) -> None:
    """Removing required downloaded-record evidence must fail before manifest creation."""

    with pytest.raises(ManifestBuildError):
        build_source_manifest(record, rights_decision=_decision(_record()))
    with pytest.raises(ManifestBuildError):
        build_document_manifest(record)


@pytest.mark.parametrize(
    "aliases",
    [
        (
            "rki-bitstream-ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "rki-bitstream-0000000000000000000000000000000000000000000000000000000000000000",
        ),
        ("rki-bitstream-0000000000000000000000000000000000000000000000000000000000000000",) * 2,
        ("rki-bitstream-ca16f3bf368deddef0cc580b31c0105db58edcfd486fa689a8860cb8aea67176",),
    ],
)
def test_source_builder_rejects_unsorted_duplicate_or_self_aliases(aliases: tuple[str, ...]) -> None:
    """Alias lists must remain sorted, unique, and external to their own bitstream."""

    with pytest.raises(ManifestBuildError):
        _source_manifest(_record(), same_content_as=aliases)


@pytest.mark.parametrize("superseded_by", ("bad", "rki-176904-12345-v2", "rki-176904-12345-v1"))
def test_document_builder_rejects_invalid_superseded_by(superseded_by: str) -> None:
    """A version cannot supersede itself or point outside its document lineage."""

    with pytest.raises(ManifestBuildError):
        build_document_manifest(_record(), superseded_by=superseded_by)


def test_source_manifests_link_same_content_to_sorted_canonical_bitstream() -> None:
    """Equal hashes must form one explicit alias relation to lowest bitstream ID."""

    first = _record(
        pdf_url=(
            "https://edoc.rki.de/bitstream/handle/176904/12345.2/"
            "first.pdf?sequence=1"
        ),
    )
    second = replace(
        _record(
            pdf_url=(
                "https://edoc.rki.de/bitstream/handle/176904/12345.2/"
                "second.pdf?sequence=2"
            ),
        ),
        source_filename="second.pdf",
    )
    unrelated = replace(
        _record(
            pdf_url=(
                "https://edoc.rki.de/bitstream/handle/176904/12345.2/"
                "third.pdf?sequence=3"
            ),
            sha256="b" * 64,
        ),
        source_filename="third.pdf",
    )

    manifests = build_source_manifests(
        (second, unrelated, first),
        rights_decisions=_decision_map(second, unrelated, first),
    )

    assert [manifest["bitstream_id"] for manifest in manifests] == sorted(
        manifest["bitstream_id"] for manifest in manifests
    )
    by_id = {manifest["bitstream_id"]: manifest for manifest in manifests}
    shared = sorted(manifest["bitstream_id"] for manifest in manifests if manifest["sha256"] == "a" * 64)
    assert by_id[shared[0]]["same_content_as"] == []
    assert by_id[shared[1]]["same_content_as"] == [shared[0]]
    assert next(manifest for manifest in manifests if manifest["sha256"] == "b" * 64)["same_content_as"] == []


@pytest.mark.parametrize("builder", (build_source_manifest, build_document_manifest))
def test_builders_reject_nonconcrete_scope(builder: object) -> None:
    """Allowing Scope.ALL would produce a source manifest without document type."""

    record = replace(_record(), scope=Scope.ALL)
    with pytest.raises(ManifestBuildError):
        if builder is build_source_manifest:
            build_source_manifest(record, rights_decision=_decision(record))
        else:
            build_document_manifest(record)


@pytest.mark.parametrize(
    "record",
    [
        _record(
            pdf_url=(
                "https://edoc.rki.de/bitstream/handle/176904/99999.1/"
                "issue.pdf?sequence=2"
            )
        ),
        _record(item_url="https://edoc.rki.de/handle/176904/99999.1"),
    ],
)
def test_builders_reject_urls_for_different_handle(record: ArtifactRecord) -> None:
    """URL handle mismatch could attach evidence from a different RKI document."""

    with pytest.raises(ManifestBuildError):
        build_source_manifest(record, rights_decision=_decision(_record()))
    with pytest.raises(ManifestBuildError):
        build_document_manifest(record)


def test_source_manifests_deduplicate_canonically_equivalent_bitstream_urls() -> None:
    """isAllowed=y must not create duplicate output for same canonical bitstream."""

    canonical = _record()
    equivalent = replace(canonical, pdf_url=canonical.pdf_url + "&isAllowed=y")

    manifests = build_source_manifests(
        (equivalent, canonical),
        rights_decisions=_decision_map(equivalent, canonical),
    )

    assert manifests == (_source_manifest(canonical),)


def test_source_manifests_reject_conflicting_canonical_duplicate() -> None:
    """Deduplication must not discard differing manifest-relevant source data."""

    canonical = _record()
    conflict = replace(canonical, pdf_url=canonical.pdf_url + "&isAllowed=y", title="Other title")

    with pytest.raises(ManifestBuildError):
        build_source_manifests(
            (canonical, conflict),
            rights_decisions=_decision_map(canonical, conflict),
        )


def test_writer_validates_before_atomic_replacement(tmp_path: Path) -> None:
    """Skipping validation could replace a good manifest with invalid JSON contract data."""

    root = tmp_path / "root"
    path = root / "manifests" / "source.json"
    payload = _source_manifest(_record())
    write_manifest(path, payload, contract_name="source-manifest", allowed_root=root)
    first = path.read_bytes()
    invalid = deepcopy(payload)
    invalid["schema_version"] = "0.0.0"

    with pytest.raises(ManifestBuildError):
        write_manifest(path, invalid, contract_name="source-manifest", allowed_root=root)

    assert path.read_bytes() == first == stable_json_dumps(payload).encode("utf-8")
    assert not list(path.parent.glob("*.part"))


def test_writer_rejects_target_outside_allowed_root(tmp_path: Path) -> None:
    """Path escape must not create a manifest outside caller-owned root."""

    root = tmp_path / "root"
    escaped = tmp_path / "escaped.json"

    with pytest.raises(UnsafePathError):
        write_manifest(
            escaped,
            _source_manifest(_record()),
            contract_name="source-manifest",
            allowed_root=root,
        )

    assert not escaped.exists()


def test_writer_rejects_symlink_target(tmp_path: Path) -> None:
    """Symlinked manifest target must never redirect atomic write outside root."""

    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.json"
    target = root / "source.json"
    target.symlink_to(outside)

    with pytest.raises(UnsafePathError):
        write_manifest(
            target,
            _source_manifest(_record()),
            contract_name="source-manifest",
            allowed_root=root,
        )

    assert not outside.exists()
