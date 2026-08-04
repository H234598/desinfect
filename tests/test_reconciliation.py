from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import NoReturn
from pathlib import Path

import pytest

from scripts.rki_pipeline.reconciliation import (
    FindingCode,
    ReconciliationIntegrityError,
    ReconciliationCounts,
    ReconciliationFinding,
    RemoteSnapshotError,
    SubjectKind,
    build_reconciliation_result,
    compare_remote_sources,
    reconcile_rights,
    reconcile_periods,
    reconcile_storage,
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
    RightsPolicyError,
    RightsState,
    load_rights_authority,
    load_rights_policy,
    resolve_rights,
)
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.source_manifest import build_document_manifest, build_source_manifests
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    RightsStorageAuthorizer,
    StorageBackend,
    StorageError,
    StorageIntent,
    StorageReference,
)
from scripts.rki_pipeline.storage.config import LfsConfig
from scripts.rki_pipeline.storage.lfs import LfsStorageAdapter
from scripts.rki_pipeline.manifests import ManifestGraph
from scripts.rki_pipeline.run_modes import EffectLedger
from scripts.rki_pipeline import aggregation as aggregation_module
from scripts.rki_pipeline.io_utils import stable_json_dumps
from tests.test_period_archives import _materialize_complete_publication, _plan_inputs


def finding(code: FindingCode, subject_id: str) -> ReconciliationFinding:
    return ReconciliationFinding(
        code=code,
        subject_kind=SubjectKind.SOURCE,
        subject_id=subject_id,
        relative_path=None,
        message=code.value,
    )


_RECONCILIATION_SOURCE_ID = "rki:176904/900000001"
_RECONCILIATION_SOURCE_SHA256 = "a" * 64
_RECONCILIATION_BITSTREAM_ID = "rki-bitstream-" + "b" * 64
_RECONCILIATION_DOCUMENT_ID = "rki-176904-900000001-v1"


@dataclass
class RecordingAdapter:
    backend: StorageBackend
    references: tuple[StorageReference, ...]
    failures: dict[str, Exception]
    verified: list[str] = field(default_factory=list)

    def authorize(
        self,
        subject: StorageIntent | PreparedObject | StorageReference,
        *,
        operation: str,
    ) -> NoReturn:
        raise AssertionError("reconciliation must not authorize storage")

    def exists(self, intent: StorageIntent) -> NoReturn:
        raise AssertionError("reconciliation must not query storage existence")

    def materialize(
        self,
        intent: StorageIntent,
        *,
        temp_root: Path,
        ledger: EffectLedger,
    ) -> NoReturn:
        raise AssertionError("reconciliation must not materialize storage")

    def export(
        self,
        reference: StorageReference,
        *,
        temp_root: Path,
        ledger: EffectLedger,
    ) -> NoReturn:
        raise AssertionError("reconciliation must not export storage")

    def apply(self, prepared: PreparedObject, *, ledger: EffectLedger) -> NoReturn:
        raise AssertionError("reconciliation must not apply storage")

    def verify(self, reference: StorageReference) -> None:
        self.verified.append(reference.artifact_id)
        failure = self.failures.get(reference.artifact_id)
        if failure is not None:
            raise failure

    def list_references(self) -> tuple[StorageReference, ...]:
        return self.references


def storage_reference(
    artifact_id: str = "artifact-a",
    *,
    decision_sha256: str = "c" * 64,
    rights_state: str = "approved",
) -> StorageReference:
    return StorageReference(
        artifact_id=artifact_id,
        relative_path=f"rki/Bulletins/Jahre/2026/PDF/{artifact_id}.pdf",
        storage_backend=StorageBackend.LFS,
        storage_object_id=f"sha256:{'d' * 64}",
        sha256="d" * 64,
        size=12,
        source_id=_RECONCILIATION_SOURCE_ID,
        source_sha256=_RECONCILIATION_SOURCE_SHA256,
        document_id=_RECONCILIATION_DOCUMENT_ID,
        conversion_id=None,
        decision_sha256=decision_sha256,
        provenance_state="current",
        visibility="repository_authorized",
        rights_state=rights_state,
        public_reference=None,
    )


def storage_graph(
    *references: StorageReference,
    decision_sha256: str = "c" * 64,
    rights_state: str = "approved",
) -> ManifestGraph:
    return ManifestGraph(
        sources=(
            {
                "source_id": _RECONCILIATION_SOURCE_ID,
                "sha256": _RECONCILIATION_SOURCE_SHA256,
                "bitstream_id": _RECONCILIATION_BITSTREAM_ID,
                "decision_sha256": decision_sha256,
                "rights": {"state": rights_state},
            },
        ),
        documents=(),
        conversions=(),
        storage_references=tuple(reference.to_dict() for reference in references),
    )


def test_storage_verifies_each_manifest_reference_once() -> None:
    first = storage_reference("artifact-a")
    second = storage_reference("artifact-b")
    adapter = RecordingAdapter(StorageBackend.LFS, (), {})

    findings = reconcile_storage(
        storage_graph(first, second),
        {StorageBackend.LFS: adapter},
    )

    assert findings == ()
    assert adapter.verified == ["artifact-a", "artifact-b"]


def _period_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    markdown: bool = True,
) -> tuple[ManifestGraph, Path]:
    result = _materialize_complete_publication(
        tmp_path,
        monkeypatch,
        markdown=markdown,
    )
    graph = _plan_inputs(tmp_path, markdown=markdown)["graph"]
    assert isinstance(graph, ManifestGraph)
    return graph, result.root


def _period_manifest(root: Path, kind: str, value: str) -> Path:
    return root / f"rki/Bulletins/Manifeste/Archive/{kind}/{value}.json"


def _rewrite_period_manifest(path: Path, mutate) -> None:
    manifest = json.loads(path.read_bytes())
    mutate(manifest)
    manifest["input_fingerprint"] = aggregation_module._manifest_fingerprint(manifest)
    path.write_text(stable_json_dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("markdown", (False, True), ids=("pdf-only", "both"))
def test_period_completeness_accepts_available_formats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    markdown: bool,
) -> None:
    graph, root = _period_fixture(tmp_path, monkeypatch, markdown=markdown)

    assert reconcile_periods(graph, root) == ()


def test_period_completeness_accepts_markdown_only_without_pdf_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, root = _period_fixture(tmp_path, monkeypatch)
    document = graph.documents[-1]
    markdown_path = document["paths"]["markdown"]
    markdown_graph = ManifestGraph(
        sources=graph.sources,
        documents=(*graph.documents[:-1], {**document, "paths": {"pdf": None, "markdown": markdown_path}}),
        conversions=graph.conversions,
        storage_references=tuple(
            reference
            for reference in graph.storage_references
            if reference["relative_path"] == markdown_path
        ),
    )
    for kind, value in (("week", "2026-W28"), ("month", "2026-07"), ("year", "2026")):
        def remove_pdf(manifest: dict[str, object]) -> None:
            manifest["archives"] = [
                archive
                for archive in manifest["archives"]
                if archive["kind"] != f"{kind}-pdf"
            ]
            for row in manifest["documents"]:
                row["pdf_artifact_id"] = None
                row["pdf_sha256"] = None

        _rewrite_period_manifest(_period_manifest(root, kind, value), remove_pdf)

    assert reconcile_periods(markdown_graph, root) == ()


def test_period_completeness_checks_year_only_for_partial_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, root = _period_fixture(tmp_path, monkeypatch)
    current = graph.documents[-1]
    partial = {
        **current,
        "publication_date": None,
        "canonical_periods": {"week": None, "month": None, "year": 2026},
    }
    partial_graph = ManifestGraph(
        sources=graph.sources,
        documents=(*graph.documents[:-1], partial),
        conversions=graph.conversions,
        storage_references=graph.storage_references,
    )
    _period_manifest(root, "week", "2026-W28").unlink()
    _period_manifest(root, "month", "2026-07").unlink()

    assert reconcile_periods(partial_graph, root) == ()


def test_period_completeness_emits_one_canonical_finding_per_affected_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph, root = _period_fixture(tmp_path, monkeypatch)
    current = graph.documents[-1]
    partial = {
        **current,
        "publication_date": None,
        "canonical_periods": {"week": None, "month": None, "year": 2026},
        "paths": {"pdf": None, "markdown": None},
    }
    second = {
        **partial,
        "document_id": "rki-176904-900000099-v1",
        "bitstream_id": "rki-bitstream-" + "9" * 64,
        "source_id": "rki:176904/900000099",
    }
    partial_graph = ManifestGraph(
        sources=(),
        documents=(partial, second),
        conversions=(),
        storage_references=(),
    )
    _period_manifest(root, "year", "2026").unlink()

    findings = reconcile_periods(partial_graph, root)

    assert [(item.code, item.subject_id) for item in findings] == [
        (FindingCode.MISSING_LOCAL, "year:2026"),
    ]


@pytest.mark.parametrize(
    ("mutation", "code", "relative_path"),
    (
        (
            "missing-manifest",
            FindingCode.MISSING_LOCAL,
            "rki/Bulletins/Manifeste/Archive/week/2026-W28.json",
        ),
        (
            "missing-bundle",
            FindingCode.MISSING_LOCAL,
            "rki/Bulletins/Manifeste/Archive/week/2026-W28.json",
        ),
        (
            "corrupt-bundle",
            FindingCode.CHANGED,
            "rki/Bulletins/Manifeste/Archive/week/2026-W28.json",
        ),
        (
            "mismatched-bundle",
            FindingCode.CHANGED,
            "rki/Bulletins/Manifeste/Archive/week/2026-W28.json",
        ),
        (
            "wrong-membership",
            FindingCode.CHANGED,
            "rki/Bulletins/Manifeste/Archive/month/2026-07.json",
        ),
        (
            "month-week-link",
            FindingCode.CHANGED,
            "rki/Bulletins/Manifeste/Archive/week/2026-W28.json",
        ),
        (
            "year-month-link",
            FindingCode.CHANGED,
            "rki/Bulletins/Manifeste/Archive/year/2026.json",
        ),
    ),
)
def test_period_completeness_classifies_missing_and_changed_publications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    code: FindingCode,
    relative_path: str,
) -> None:
    graph, root = _period_fixture(tmp_path, monkeypatch)
    week = _period_manifest(root, "week", "2026-W28")
    if mutation == "missing-manifest":
        week.unlink()
    elif mutation in {"missing-bundle", "corrupt-bundle", "mismatched-bundle"}:
        manifest = json.loads(week.read_bytes())
        bundle = root / manifest["archives"][0]["relative_bundle"]
        if mutation == "missing-bundle":
            bundle.rename(tmp_path / "missing-bundle")
        elif mutation == "corrupt-bundle":
            (bundle / "archive.zip").write_bytes(b"corrupt")
        else:
            def mismatch(current: dict[str, object]) -> None:
                current["archives"][0]["output_sha256"] = "0" * 64

            _rewrite_period_manifest(week, mismatch)
    elif mutation in {"wrong-membership", "month-week-link"}:
        def change_membership(manifest: dict[str, object]) -> None:
            manifest["documents"][0]["document_id"] = "rki-176904-900000099-v1"

        target = (
            _period_manifest(root, "month", "2026-07")
            if mutation == "wrong-membership"
            else week
        )
        _rewrite_period_manifest(target, change_membership)
    else:
        def remove_month_link(manifest: dict[str, object]) -> None:
            manifest["month_manifests"] = []

        _rewrite_period_manifest(_period_manifest(root, "year", "2026"), remove_month_link)

    findings = reconcile_periods(graph, root)

    assert [(item.code, item.subject_kind, item.subject_id, item.relative_path) for item in findings] == [
        (
            code,
            SubjectKind.PERIOD,
            f"{Path(relative_path).parent.name}:{Path(relative_path).stem}",
            relative_path,
        )
    ]
    assert str(tmp_path) not in findings[0].message


@pytest.mark.parametrize(
    ("adapters", "failures"),
    (
        ({}, {}),
        ({StorageBackend.LFS: RecordingAdapter(StorageBackend.LFS, (), {})}, {"artifact-a": FileNotFoundError()}),
    ),
    ids=("adapter-missing", "reference-missing"),
)
def test_storage_missing_adapter_or_reference_is_missing_local(adapters, failures) -> None:
    reference = storage_reference()
    if adapters:
        adapters[StorageBackend.LFS].failures.update(failures)

    findings = reconcile_storage(storage_graph(reference), adapters)

    assert [(item.code, item.subject_id) for item in findings] == [
        (FindingCode.MISSING_LOCAL, "artifact-a"),
    ]


def test_storage_integrity_error_is_changed_without_exception_details() -> None:
    reference = storage_reference()
    adapter = RecordingAdapter(
        StorageBackend.LFS,
        (),
        {"artifact-a": StorageError("/private/secret payload")},
    )

    findings = reconcile_storage(storage_graph(reference), {StorageBackend.LFS: adapter})

    assert [item.code for item in findings] == [FindingCode.CHANGED]
    assert "/private/" not in findings[0].message
    assert "secret payload" not in findings[0].message


def test_storage_inventory_extra_reference_is_orphan() -> None:
    reference = storage_reference()
    extra = storage_reference("orphan-a")
    adapter = RecordingAdapter(StorageBackend.LFS, (extra,), {})

    findings = reconcile_storage(storage_graph(reference), {StorageBackend.LFS: adapter})

    assert [(item.code, item.subject_id) for item in findings] == [
        (FindingCode.ORPHAN, "orphan-a"),
    ]


def test_storage_reconciliation_matches_legacy_lfs_inventory_by_path(
    tmp_path: Path,
    storage_rights,
) -> None:
    decision_sha256 = storage_rights.set_decisions(
        (_RECONCILIATION_SOURCE_ID, _RECONCILIATION_SOURCE_SHA256, "approved"),
    )[(_RECONCILIATION_SOURCE_ID, _RECONCILIATION_SOURCE_SHA256)]
    repository = tmp_path / "repository"
    repository.mkdir()
    artifact = repository / "rki/Bulletins/Jahre/2026/PDF/current.pdf"
    artifact.parent.mkdir(parents=True)
    payload = b"%PDF-1.4\n%%EOF\n"
    artifact.write_bytes(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    reference = StorageReference(
        artifact_id="durable-pdf",
        relative_path="rki/Bulletins/Jahre/2026/PDF/current.pdf",
        storage_backend=StorageBackend.LFS,
        storage_object_id=f"sha256:{sha256}",
        sha256=sha256,
        size=len(payload),
        source_id=_RECONCILIATION_SOURCE_ID,
        source_sha256=_RECONCILIATION_SOURCE_SHA256,
        document_id=_RECONCILIATION_DOCUMENT_ID,
        conversion_id=None,
        decision_sha256=decision_sha256,
        provenance_state="current",
        visibility="repository_authorized",
        rights_state="approved",
        public_reference=None,
    )
    adapter = LfsStorageAdapter(
        repository_root=repository,
        config=LfsConfig(
            artifact_root="rki/Bulletins",
            max_run_objects=1,
            max_run_bytes=1,
            warn_total_bytes=2,
            block_total_bytes=3,
        ),
        authorizer=storage_rights.authorizer,
    )

    assert reconcile_storage(
        storage_graph(reference, decision_sha256=decision_sha256),
        {StorageBackend.LFS: adapter},
    ) == ()


def test_storage_duplicate_inventory_identity_fails_closed() -> None:
    reference = storage_reference()
    duplicate = storage_reference("orphan-a")
    adapter = RecordingAdapter(StorageBackend.LFS, (duplicate, duplicate), {})

    with pytest.raises(ReconciliationIntegrityError, match="doppelt"):
        reconcile_storage(storage_graph(reference), {StorageBackend.LFS: adapter})


def _rights_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: RightsState,
) -> tuple[object, object, RightsDecision]:
    from scripts.rki_pipeline import rights

    register = tmp_path / "rights-register.yml"
    register.write_text(
        "schema_version: 1\n"
        "decisions:\n"
        f'  - source_id: "{_RECONCILIATION_SOURCE_ID}"\n'
        f'    source_sha256: "{_RECONCILIATION_SOURCE_SHA256}"\n'
        f'    state: "{state.value}"\n'
        '    basis: "Synthetic reconciliation test"\n'
        '    reviewed_by: "Test Reviewer"\n'
        '    reviewed_at: "2026-08-04T04:00:00Z"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(rights, "_canonical_authority_source", register.resolve)
    authority = load_rights_authority()
    policy = load_rights_policy()
    return (
        authority,
        policy,
        resolve_rights(
            _RECONCILIATION_SOURCE_ID,
            _RECONCILIATION_SOURCE_SHA256,
            authority=authority,
            policy=policy,
        ),
    )


def test_rights_persisted_decision_mismatch_marks_source_and_storage_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, policy, decision = _rights_authority(
        tmp_path, monkeypatch, state=RightsState.APPROVED
    )
    reference = storage_reference(decision_sha256="f" * 64)

    findings = reconcile_rights(
        storage_graph(reference, decision_sha256="f" * 64),
        authority=authority,
        policy=policy,
    )

    assert [(item.code, item.subject_kind) for item in findings] == [
        (FindingCode.RIGHTS_CHANGED, SubjectKind.SOURCE),
        (FindingCode.RIGHTS_CHANGED, SubjectKind.STORAGE),
    ]
    assert all(decision.decision_sha256 not in item.message for item in findings)


@pytest.mark.parametrize(
    "state",
    (RightsState.INTERNAL_ONLY, RightsState.TAKEDOWN, RightsState.METADATA_ONLY),
    ids=("restricted", "takedown", "metadata-only"),
)
def test_rights_nonapproved_current_decision_is_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: RightsState,
) -> None:
    authority, policy, decision = _rights_authority(tmp_path, monkeypatch, state=state)
    reference = storage_reference(
        decision_sha256=decision.decision_sha256 or "0" * 64,
        rights_state=state.value,
    )

    findings = reconcile_rights(
        storage_graph(
            reference,
            decision_sha256=decision.decision_sha256 or "0" * 64,
            rights_state=state.value,
        ),
        authority=authority,
        policy=policy,
    )

    assert [item.code for item in findings] == [
        FindingCode.RIGHTS_CHANGED,
        FindingCode.RIGHTS_CHANGED,
    ]


def test_rights_unchanged_approved_decision_has_no_finding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, policy, decision = _rights_authority(
        tmp_path, monkeypatch, state=RightsState.APPROVED
    )
    assert decision.decision_sha256 is not None
    reference = storage_reference(decision_sha256=decision.decision_sha256)

    assert reconcile_rights(
        storage_graph(reference, decision_sha256=decision.decision_sha256),
        authority=authority,
        policy=policy,
    ) == ()


def test_rights_contract_error_is_path_free_integrity_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, policy, _decision = _rights_authority(
        tmp_path, monkeypatch, state=RightsState.APPROVED
    )
    from scripts.rki_pipeline import reconciliation

    def reject(*_args, **_kwargs) -> NoReturn:
        raise RightsPolicyError("/private/secret payload")

    monkeypatch.setattr(reconciliation, "resolve_rights", reject)

    with pytest.raises(ReconciliationIntegrityError) as error:
        reconcile_rights(
            storage_graph(storage_reference()),
            authority=authority,
            policy=policy,
        )

    assert "/private/" not in str(error.value)
    assert "secret payload" not in str(error.value)


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


@pytest.mark.parametrize(
    "state",
    (RecordState.PLANNED, RecordState.EXISTING, RecordState.DOWNLOADED, RecordState.RESUMED),
)
def test_remote_download_claim_without_pdf_url_is_rejected(state: RecordState) -> None:
    remote = replace(remote_record(pdf_url=None, sha256=None), state=state)

    with pytest.raises(RemoteSnapshotError, match="PDF"):
        compare_remote_sources(remote_catalog(), (remote,))


@pytest.mark.parametrize(
    "claim",
    (
        {"sha256": LOCAL_SOURCE_SHA256},
        {"bytes": 0},
        {"relative_path": "Jahre/2026/PDF/source.pdf"},
    ),
    ids=("hash", "bytes", "path"),
)
def test_remote_content_claim_without_pdf_url_is_rejected(claim: dict[str, object]) -> None:
    remote = replace(remote_record(pdf_url=None, sha256=None), **claim)

    with pytest.raises(RemoteSnapshotError, match="PDF"):
        compare_remote_sources(remote_catalog(), (remote,))


def test_remote_no_pdf_record_without_content_claim_is_ignored() -> None:
    remote = remote_record(pdf_url=None, sha256=None)

    assert compare_remote_sources(remote_catalog(), (remote,)) == ()
