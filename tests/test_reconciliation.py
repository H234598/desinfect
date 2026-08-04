from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path

import pytest

from scripts.rki_pipeline.reconciliation import (
    FindingCode,
    ReconciliationCounts,
    ReconciliationFinding,
    SubjectKind,
    build_reconciliation_result,
    compare_remote_sources,
    source_subject_id,
)
from scripts.rki_grabber.models import ArtifactRecord, RecordState, RightsMetadata, Scope
from scripts.rki_pipeline.documents import bitstream_identity
from scripts.rki_pipeline.manifests import (
    LoadedManifestCatalog,
    build_manifest_graph,
    render_manifest_catalog,
)
from scripts.rki_pipeline.rights import (
    RightsDecision,
    RightsState,
    load_rights_authority,
    load_rights_policy,
)
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.source_manifest import build_document_manifest, build_source_manifests
from scripts.rki_pipeline.storage.base import PreparedObject, RightsStorageAuthorizer


def finding(code: FindingCode, subject_id: str) -> ReconciliationFinding:
    return ReconciliationFinding(
        code=code,
        subject_kind=SubjectKind.SOURCE,
        subject_id=subject_id,
        relative_path=None,
        message=code.value,
    )


def test_result_sorts_findings_and_maps_new_to_missing_local() -> None:
    result = build_reconciliation_result(
        as_of=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
        from_year=1996,
        to_year=2026,
        source_manifest_sha256="a" * 64,
        findings=(
            finding(FindingCode.OK, "source-b"),
            finding(FindingCode.NEW, "source-c"),
            finding(FindingCode.CHANGED, "source-a"),
        ),
    )
    assert [item.code for item in result.findings] == [
        FindingCode.CHANGED,
        FindingCode.NEW,
        FindingCode.OK,
    ]
    assert result.counts == ReconciliationCounts(
        ok=1,
        changed=1,
        missing_remote=0,
        missing_local=1,
        orphan=0,
        rights_changed=0,
        unresolved=2,
    )
    assert result.conclusion == "blocked"
    assert result.successful_at is None
    validate_document("reconciliation-report", result.report)


def test_result_success_records_as_of_as_successful_at() -> None:
    as_of = datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc)

    result = build_reconciliation_result(
        as_of=as_of,
        from_year=1996,
        to_year=2026,
        source_manifest_sha256="a" * 64,
        findings=(finding(FindingCode.OK, "source-a"),),
    )

    assert result.conclusion == "success"
    assert result.successful_at == as_of
    assert result.report == {
        "schema_version": "1.0.0",
        "scope": {"from_year": 1996, "to_year": 2026},
        "as_of": "2026-08-04T04:00:00Z",
        "counts": {
            "ok": 1,
            "changed": 0,
            "missing_remote": 0,
            "missing_local": 0,
            "orphan": 0,
            "rights_changed": 0,
            "unresolved": 0,
        },
        "conclusion": "success",
        "source_manifest_sha256": "a" * 64,
    }


@pytest.mark.parametrize(
    ("as_of", "from_year", "to_year", "source_manifest_sha256"),
    [
        (datetime(2026, 8, 4, 4, 0), 1996, 2026, "a" * 64),
        (
            datetime(2026, 8, 4, 4, 0, tzinfo=timezone(timedelta(hours=2))),
            1996,
            2026,
            "a" * 64,
        ),
        (datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 2027, 2026, "a" * 64),
        (datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 1989, 2026, "a" * 64),
        (datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 1996, 10_000, "a" * 64),
        (datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc), 1996, 2026, "A" * 64),
    ],
)
def test_result_rejects_noncanonical_metadata(
    as_of: datetime,
    from_year: int,
    to_year: int,
    source_manifest_sha256: str,
) -> None:
    with pytest.raises(ValueError):
        build_reconciliation_result(
            as_of=as_of,
            from_year=from_year,
            to_year=to_year,
            source_manifest_sha256=source_manifest_sha256,
            findings=(finding(FindingCode.OK, "source-a"),),
        )

def test_result_rejects_duplicate_finding_keys() -> None:
    item = finding(FindingCode.OK, "source-a")

    with pytest.raises(ValueError, match="doppelt"):
        build_reconciliation_result(
            as_of=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
            from_year=1996,
            to_year=2026,
            source_manifest_sha256="a" * 64,
            findings=(item, item),
        )


def test_result_rejects_ok_mixed_with_unresolved_finding_for_source() -> None:
    with pytest.raises(ValueError, match="ok"):
        build_reconciliation_result(
            as_of=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
            from_year=1996,
            to_year=2026,
            source_manifest_sha256="a" * 64,
            findings=(
                finding(FindingCode.OK, source_subject_id("source-a", "bitstream-a")),
                finding(FindingCode.CHANGED, source_subject_id("source-a", "bitstream-b")),
            ),
        )


@pytest.mark.parametrize(
    ("relative_path", "message"),
    [
        ("/absolute/path.pdf", "valid"),
        ("relative/path.pdf", "contains\x00control"),
        ("relative/path.pdf", "x" * 501),
    ],
)
def test_finding_rejects_unsafe_path_and_message(
    relative_path: str,
    message: str,
) -> None:
    with pytest.raises(ValueError):
        ReconciliationFinding(
            code=FindingCode.CHANGED,
            subject_kind=SubjectKind.STORAGE,
            subject_id="artifact-a",
            relative_path=relative_path,
            message=message,
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "artifacts/../report.pdf",
        "./report.pdf",
        "artifacts//report.pdf",
        "artifacts\\report.pdf",
        "C:report.pdf",
        "cafe\u0301.pdf",
    ],
)
def test_finding_rejects_noncanonical_relative_path_aliases(relative_path: str) -> None:
    with pytest.raises(ValueError):
        ReconciliationFinding(
            code=FindingCode.CHANGED,
            subject_kind=SubjectKind.STORAGE,
            subject_id="artifact-a",
            relative_path=relative_path,
            message="changed",
        )


def test_counts_reject_boolean_values() -> None:
    with pytest.raises(ValueError, match="Ganzzahl"):
        ReconciliationCounts(
            ok=True,
            changed=0,
            missing_remote=0,
            missing_local=0,
            orphan=0,
            rights_changed=0,
            unresolved=0,
        )


LOCAL_SOURCE_SHA256 = hashlib.sha256(b"local source").hexdigest()
_CANDIDATE_DECISION_SHA256 = "d" * 64


def remote_record(
    *,
    item_handle: str = "176904/900000001",
    pdf_url: str | None = (
        "https://edoc.rki.de/bitstream/handle/176904/900000001/source.pdf?sequence=1"
    ),
    sha256: str | None = LOCAL_SOURCE_SHA256,
) -> ArtifactRecord:
    version = 2 if item_handle.endswith(".2") else 1
    document_number = item_handle.split("/", 1)[1].split(".", 1)[0]
    return ArtifactRecord(
        scope=Scope.ISSUES,
        document_id=f"rki-176904-{document_number}-v{version}",
        source_id=f"rki:{item_handle}",
        version=version,
        item_handle=item_handle,
        item_url=f"https://edoc.rki.de/handle/{item_handle}",
        title="Synthetic remote bulletin",
        publication_date="2026-07-10",
        year=2026,
        doi=None,
        rights=RightsMetadata(
            label="Synthetic fixture",
            uri="https://example.invalid/license",
            copyright_notice=None,
            open_access=True,
        ),
        pdf_url=pdf_url,
        source_filename="source.pdf" if pdf_url is not None else None,
        relative_path="Jahre/2026/PDF/source.pdf" if pdf_url is not None else None,
        state=RecordState.DOWNLOADED if pdf_url is not None else RecordState.NO_PDF,
        bytes=12 if pdf_url is not None else None,
        sha256=sha256,
        etag='"fixture"',
        last_modified="Fri, 10 Jul 2026 00:00:00 GMT",
    )


def remote_catalog(*records: ArtifactRecord) -> LoadedManifestCatalog:
    decisions = tuple(
        RightsDecision(
            source_id=record.source_id,
            source_sha256=record.sha256 or "0" * 64,
            state=RightsState.METADATA_ONLY,
            basis="rights_register_no_match",
            reviewed_by=None,
            reviewed_at=None,
            decision_sha256=None,
        )
        for record in records
    )
    sources = build_source_manifests(
        records,
        rights_decisions={
            (record.source_id, record.sha256): decision
            for record, decision in zip(records, decisions, strict=True)
        },
    )
    documents = tuple(
        build_document_manifest(
            record,
            superseded_by=next(
                (
                    newer.document_id
                    for newer in records
                    if newer.document_id.rsplit("-v", 1)[0]
                    == record.document_id.rsplit("-v", 1)[0]
                    and newer.version > record.version
                ),
                None,
            ),
        )
        for record in records
    )
    authorizer = RightsStorageAuthorizer(load_rights_authority(), load_rights_policy())
    graph = build_manifest_graph(
        sources=sources,
        documents=documents,
        conversions=(),
        storage_references=(),
        authorizer=authorizer,
    )
    return LoadedManifestCatalog(graph=graph, rendered=render_manifest_catalog(graph))


def candidate_prepared_object(
    tmp_path: Path,
    *,
    record: ArtifactRecord,
    changed: bool = False,
) -> PreparedObject:
    payload = b"changed candidate" if changed else b"local source"
    path = tmp_path / ("changed.pdf" if changed else "matching.pdf")
    path.write_bytes(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    return PreparedObject(
        artifact_id="candidate-object",
        logical_key="candidate.pdf",
        path=path,
        temp_root=tmp_path,
        sha256=sha256,
        size=len(payload),
        source_id=record.source_id,
        source_sha256=sha256,
        decision_sha256=_CANDIDATE_DECISION_SHA256,
        visibility="internal",
        rights_state="metadata_only",
        document_id=record.document_id,
    )


def test_remote_metadata_avoids_blind_candidate_load(tmp_path: Path) -> None:
    catalog = remote_catalog(remote_record())
    calls: list[str] = []

    def loader(record: ArtifactRecord) -> PreparedObject:
        calls.append(record.source_id)
        return candidate_prepared_object(tmp_path, record=record)

    matching = remote_record()
    assert compare_remote_sources(catalog, (matching,), candidate_loader=loader) == ()
    assert calls == []

    changed = replace(matching, etag='"new"')
    findings = compare_remote_sources(catalog, (changed,), candidate_loader=loader)

    assert calls == [changed.source_id]
    assert [item.code for item in findings] == [FindingCode.CHANGED]


def test_remote_only_record_is_new() -> None:
    catalog = remote_catalog()
    remote = remote_record(
        item_handle="176904/900000002",
        pdf_url="https://edoc.rki.de/bitstream/handle/176904/900000002/source.pdf?sequence=1",
    )

    findings = compare_remote_sources(catalog, (remote,))

    assert [(item.code, item.subject_id) for item in findings] == [
        (FindingCode.NEW, source_subject_id(remote.source_id, bitstream_identity(remote.pdf_url).bitstream_id)),
    ]


def test_current_local_record_missing_from_remote() -> None:
    local = remote_record()
    catalog = remote_catalog(local)
    bitstream_id = catalog.graph.sources[0]["bitstream_id"]

    findings = compare_remote_sources(catalog, ())

    assert [(item.code, item.subject_id) for item in findings] == [
        (FindingCode.MISSING_REMOTE, source_subject_id(local.source_id, bitstream_id)),
    ]


def test_duplicate_remote_source_bitstream_is_rejected() -> None:
    remote = remote_record()

    with pytest.raises(ValueError, match="doppelt"):
        compare_remote_sources(remote_catalog(remote), (remote, remote))


@pytest.mark.parametrize(
    "changed",
    (
        lambda record: replace(record, version=2),
        lambda record: replace(record, item_url="https://edoc.rki.de/handle/176904/900000001?x=1"),
        lambda record: replace(record, pdf_url=f"{record.pdf_url}&isAllowed=y"),
        lambda record: replace(record, last_modified="Sat, 11 Jul 2026 00:00:00 GMT"),
        lambda record: replace(record, sha256="a" * 64),
    ),
    ids=("version", "source-url", "bitstream-url", "last-modified", "supplied-hash"),
)
def test_remote_metadata_drift_is_changed(tmp_path: Path, changed) -> None:
    matching = remote_record()
    catalog = remote_catalog(matching)
    candidate = changed(matching)
    calls: list[str] = []

    def loader(record: ArtifactRecord) -> PreparedObject:
        calls.append(record.source_id)
        return candidate_prepared_object(tmp_path, record=record)

    findings = compare_remote_sources(catalog, (candidate,), candidate_loader=loader)

    assert calls == [candidate.source_id]
    assert [item.code for item in findings] == [FindingCode.CHANGED]


def test_changed_remote_candidate_hash_remains_changed(tmp_path: Path) -> None:
    matching = remote_record()
    changed = replace(matching, etag='"new"')

    findings = compare_remote_sources(
        remote_catalog(matching),
        (changed,),
        candidate_loader=lambda record: candidate_prepared_object(
            tmp_path, record=record, changed=True
        ),
    )

    assert [item.code for item in findings] == [FindingCode.CHANGED]


def test_changed_remote_without_candidate_loader_is_changed() -> None:
    matching = remote_record()

    findings = compare_remote_sources(remote_catalog(matching), (replace(matching, etag='"new"'),))

    assert [item.code for item in findings] == [FindingCode.CHANGED]


def test_candidate_loader_error_is_bounded_changed() -> None:
    matching = remote_record()

    def loader(_record: ArtifactRecord) -> PreparedObject:
        raise RuntimeError("https://secret.invalid/loader-detail")

    findings = compare_remote_sources(
        remote_catalog(matching),
        (replace(matching, etag='"new"'),),
        candidate_loader=loader,
    )

    assert [item.code for item in findings] == [FindingCode.CHANGED]
    assert "secret" not in findings[0].message


def test_superseded_local_source_is_ignored() -> None:
    old = remote_record(item_handle="176904/900000001")
    current = remote_record(
        item_handle="176904/900000001.2",
        pdf_url="https://edoc.rki.de/bitstream/handle/176904/900000001.2/source.pdf?sequence=1",
    )

    assert compare_remote_sources(remote_catalog(old, current), (current,)) == ()


def test_remote_downloadable_record_needs_canonical_pdf_identity() -> None:
    remote = replace(remote_record(), pdf_url="https://example.invalid/source.pdf")

    with pytest.raises(ValueError, match="Bitstream"):
        compare_remote_sources(remote_catalog(remote_record()), (remote,))
