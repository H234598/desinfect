from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from typing import NoReturn
from pathlib import Path

import pytest

from scripts.rki_pipeline.reconciliation import (
    FindingCode,
    ReconciliationIntegrityError,
    ReconciliationMaterialization,
    ReconciliationCounts,
    ReconciliationFinding,
    ReconciliationResult,
    RemoteSnapshotError,
    SubjectKind,
    build_reconciliation_result,
    compare_remote_sources,
    materialize_reconciliation,
    plan_reconciliation,
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
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
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
        documents=(
            {
                "source_id": _RECONCILIATION_SOURCE_ID,
                "document_id": _RECONCILIATION_DOCUMENT_ID,
                "bitstream_id": _RECONCILIATION_BITSTREAM_ID,
                "superseded_by": None,
            },
        ),
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
        (FindingCode.MISSING_LOCAL, source_subject_id(partial["source_id"], partial["bitstream_id"])),
        (FindingCode.MISSING_LOCAL, source_subject_id(second["source_id"], second["bitstream_id"])),
    ]
    result = build_reconciliation_result(
        as_of=_RECONCILIATION_AS_OF,
        from_year=2026,
        to_year=2026,
        source_manifest_sha256="a" * 64,
        findings=findings,
    )
    assert result.counts.missing_local == result.counts.unresolved == 2


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
            relative_path = manifest["archives"][0]["relative_bundle"]
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
            source_subject_id(graph.documents[-1]["source_id"], graph.documents[-1]["bitstream_id"]),
            relative_path,
        )
    ]
    assert str(tmp_path) not in findings[0].message


@pytest.mark.parametrize(
    "mutation",
    ("version", "doi", "publication-date-and-month-link"),
)
def test_period_expected_document_rows_reject_coherent_manifest_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    graph, root = _period_fixture(tmp_path, monkeypatch)
    year = _period_manifest(root, "year", "2026")

    def tamper(manifest: dict[str, object]) -> None:
        row = manifest["documents"][0]
        if mutation == "version":
            row["version"] += 1
        elif mutation == "doi":
            row["doi"] = "10.1234/coherent-tamper"
        else:
            row["publication_date"] = "2026-08-10"
            manifest["month_manifests"] = [
                "rki/Bulletins/Manifeste/Archive/month/2026-08.json"
            ]

    _rewrite_period_manifest(year, tamper)

    findings = reconcile_periods(graph, root)

    owner = source_subject_id(
        graph.documents[-1]["source_id"],
        graph.documents[-1]["bitstream_id"],
    )
    assert [(item.code, item.subject_id, item.relative_path) for item in findings] == [
        (
            FindingCode.CHANGED,
            owner,
            "rki/Bulletins/Manifeste/Archive/year/2026.json",
        ),
    ]


def test_period_expected_archive_spec_binds_storage_member_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, root = _period_fixture(tmp_path, monkeypatch)
    current = graph.documents[-1]
    old_pdf_path = current["paths"]["pdf"]
    new_pdf_path = str(Path(old_pdf_path).with_name("renamed-source.pdf"))
    changed_document = {
        **current,
        "paths": {**current["paths"], "pdf": new_pdf_path},
    }
    changed_references = tuple(
        {
            **reference,
            "relative_path": new_pdf_path,
        }
        if reference["relative_path"] == old_pdf_path
        else reference
        for reference in graph.storage_references
    )
    changed_graph = ManifestGraph(
        sources=graph.sources,
        documents=(*graph.documents[:-1], changed_document),
        conversions=graph.conversions,
        storage_references=changed_references,
    )

    findings = reconcile_periods(changed_graph, root)

    owner = source_subject_id(current["source_id"], current["bitstream_id"])
    assert {(item.code, item.subject_id) for item in findings} == {
        (FindingCode.CHANGED, owner),
    }
    assert {item.relative_path for item in findings} == {
        "rki/Bulletins/Manifeste/Archive/week/2026-W28.json",
        "rki/Bulletins/Manifeste/Archive/month/2026-07.json",
        "rki/Bulletins/Manifeste/Archive/year/2026.json",
    }


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
        (
            FindingCode.MISSING_LOCAL,
            source_subject_id(_RECONCILIATION_SOURCE_ID, _RECONCILIATION_BITSTREAM_ID),
        ),
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
        (
            FindingCode.ORPHAN,
            source_subject_id(_RECONCILIATION_SOURCE_ID, _RECONCILIATION_BITSTREAM_ID),
        ),
    ]


def test_storage_ownerless_orphan_uses_artifact_identity() -> None:
    reference = storage_reference()
    extra = replace(
        storage_reference("orphan-a"),
        source_id=None,
        source_sha256=None,
        document_id=None,
        decision_sha256=None,
        provenance_state="legacy_needs_review",
    )
    adapter = RecordingAdapter(StorageBackend.LFS, (extra,), {})

    findings = reconcile_storage(storage_graph(reference), {StorageBackend.LFS: adapter})

    assert [(item.code, item.subject_id) for item in findings] == [
        (FindingCode.ORPHAN, "orphan-a"),
    ]


def test_storage_failures_emit_one_finding_per_compound_owner() -> None:
    first = storage_reference("artifact-a")
    second = replace(
        storage_reference("artifact-b"),
        source_id="rki:176904/900000002",
        source_sha256="e" * 64,
        document_id="rki-176904-900000002-v1",
    )
    graph = ManifestGraph(
        sources=(
            storage_graph().sources[0],
            {
                "source_id": second.source_id,
                "sha256": second.source_sha256,
                "bitstream_id": "rki-bitstream-" + "e" * 64,
                "decision_sha256": second.decision_sha256,
                "rights": {"state": second.rights_state},
            },
        ),
        documents=(
            storage_graph().documents[0],
            {
                "source_id": second.source_id,
                "document_id": second.document_id,
                "bitstream_id": "rki-bitstream-" + "e" * 64,
                "superseded_by": None,
            },
        ),
        conversions=(),
        storage_references=(first.to_dict(), second.to_dict()),
    )
    adapter = RecordingAdapter(
        StorageBackend.LFS,
        (),
        {first.artifact_id: StorageError("first"), second.artifact_id: StorageError("second")},
    )

    findings = reconcile_storage(graph, {StorageBackend.LFS: adapter})

    assert [(item.code, item.subject_id) for item in findings] == [
        (
            FindingCode.CHANGED,
            source_subject_id(_RECONCILIATION_SOURCE_ID, _RECONCILIATION_BITSTREAM_ID),
        ),
        (
            FindingCode.CHANGED,
            source_subject_id(second.source_id, "rki-bitstream-" + "e" * 64),
        ),
    ]
    result = build_reconciliation_result(
        as_of=_RECONCILIATION_AS_OF,
        from_year=2026,
        to_year=2026,
        source_manifest_sha256="a" * 64,
        findings=findings,
    )
    assert result.counts.changed == result.counts.unresolved == 2


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


def test_rights_ignores_historical_superseded_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, policy, decision = _rights_authority(
        tmp_path, monkeypatch, state=RightsState.APPROVED
    )
    assert decision.decision_sha256 is not None
    current = storage_graph(
        storage_reference(decision_sha256=decision.decision_sha256),
        decision_sha256=decision.decision_sha256,
    )
    historical_source = {
        "source_id": "rki:176904/900000099",
        "sha256": "9" * 64,
        "bitstream_id": "rki-bitstream-" + "9" * 64,
        "decision_sha256": "f" * 64,
        "rights": {"state": "approved"},
    }
    historical_document = {
        "source_id": historical_source["source_id"],
        "document_id": "rki-176904-900000099-v1",
        "bitstream_id": historical_source["bitstream_id"],
        "superseded_by": "rki-176904-900000099-v2",
    }
    graph = ManifestGraph(
        sources=(*current.sources, historical_source),
        documents=(*current.documents, historical_document),
        conversions=(),
        storage_references=current.storage_references,
    )

    assert reconcile_rights(graph, authority=authority, policy=policy) == ()


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


def test_result_rejects_fractional_utc_timestamp() -> None:
    with pytest.raises(ValueError, match="Sekunden"):
        build_reconciliation_result(
            as_of=datetime(2026, 8, 4, 4, 0, 0, 1, tzinfo=timezone.utc),
            from_year=1996,
            to_year=2026,
            source_manifest_sha256="a" * 64,
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


def test_result_groups_ok_conflicts_by_full_compound_subject() -> None:
    first = source_subject_id("source-a", "bitstream-a")
    second = source_subject_id("source-a", "bitstream-b")

    result = build_reconciliation_result(
        as_of=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
        from_year=1996,
        to_year=2026,
        source_manifest_sha256="a" * 64,
        findings=(
            finding(FindingCode.OK, first),
            finding(FindingCode.CHANGED, second),
        ),
    )

    assert result.counts.ok == 1
    assert result.counts.changed == 1

    with pytest.raises(ValueError, match="ok"):
        build_reconciliation_result(
            as_of=datetime(2026, 8, 4, 4, 0, tzinfo=timezone.utc),
            from_year=1996,
            to_year=2026,
            source_manifest_sha256="a" * 64,
            findings=(
                finding(FindingCode.OK, first),
                finding(FindingCode.CHANGED, first),
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


_RECONCILIATION_AS_OF = datetime(2026, 8, 4, 4, 5, 6, tzinfo=timezone.utc)


def test_plan_reconciliation_composes_empty_consistent_snapshot(tmp_path: Path) -> None:
    catalog = remote_catalog()

    result = plan_reconciliation(
        as_of=_RECONCILIATION_AS_OF,
        from_year=2026,
        to_year=2026,
        catalog=catalog,
        remote_records=(),
        adapters={},
        period_root=tmp_path / "periods",
        authority=load_rights_authority(),
        policy=load_rights_policy(),
    )

    assert result.conclusion == "success"
    assert result.counts.unresolved == 0
    assert result.successful_at == _RECONCILIATION_AS_OF
    assert result.report["source_manifest_sha256"] == hashlib.sha256(
        dict(catalog.rendered.files)["Quellen/manifest.jsonl"]
    ).hexdigest()


def test_plan_reconciliation_rejects_duplicate_component_finding_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    catalog = remote_catalog(remote_record())
    source = source_subject_id(
        _RECONCILIATION_SOURCE_ID,
        catalog.graph.sources[0]["bitstream_id"],
    )
    remote_finding = finding(FindingCode.CHANGED, source)
    storage_finding = ReconciliationFinding(
        code=FindingCode.CHANGED,
        subject_kind=SubjectKind.STORAGE,
        subject_id="artifact-a",
        relative_path="rki/Bulletins/Jahre/2026/PDF/artifact-a.pdf",
        message="storage changed",
    )
    rights_finding = finding(FindingCode.RIGHTS_CHANGED, source)
    period_finding = ReconciliationFinding(
        code=FindingCode.MISSING_LOCAL,
        subject_kind=SubjectKind.PERIOD,
        subject_id="year:2026",
        relative_path="rki/Bulletins/Manifeste/Archive/year/2026.json",
        message="period missing",
    )
    monkeypatch.setattr(reconciliation, "compare_remote_sources", lambda *_args, **_kwargs: (remote_finding,))
    monkeypatch.setattr(
        reconciliation,
        "reconcile_storage",
        lambda *_args, **_kwargs: (storage_finding, storage_finding),
    )
    monkeypatch.setattr(reconciliation, "reconcile_rights", lambda *_args, **_kwargs: (rights_finding,))
    monkeypatch.setattr(reconciliation, "reconcile_periods", lambda *_args, **_kwargs: (period_finding,))

    with pytest.raises(ReconciliationIntegrityError, match="doppelt"):
        plan_reconciliation(
            as_of=_RECONCILIATION_AS_OF,
            from_year=2026,
            to_year=2026,
            catalog=catalog,
            remote_records=(),
            adapters={},
            period_root=tmp_path / "periods",
            authority=load_rights_authority(),
            policy=load_rights_policy(),
        )


def test_plan_reconciliation_adds_ok_for_current_source_without_open_finding(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    monkeypatch.setattr(reconciliation, "compare_remote_sources", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(reconciliation, "reconcile_storage", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(reconciliation, "reconcile_rights", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(reconciliation, "reconcile_periods", lambda *_args, **_kwargs: ())
    catalog = remote_catalog(remote_record())
    source = source_subject_id(_RECONCILIATION_SOURCE_ID, catalog.graph.sources[0]["bitstream_id"])

    result = plan_reconciliation(
        as_of=_RECONCILIATION_AS_OF,
        from_year=2026,
        to_year=2026,
        catalog=catalog,
        remote_records=(),
        adapters={},
        period_root=tmp_path / "periods",
        authority=load_rights_authority(),
        policy=load_rights_policy(),
    )

    assert [(item.code, item.subject_id) for item in result.findings] == [
        (
            FindingCode.OK,
            source,
        ),
    ]


def test_plan_suppresses_ok_for_findings_from_every_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    catalog = remote_catalog(remote_record())
    owner = source_subject_id(
        _RECONCILIATION_SOURCE_ID,
        catalog.graph.sources[0]["bitstream_id"],
    )
    storage_finding = ReconciliationFinding(
        code=FindingCode.CHANGED,
        subject_kind=SubjectKind.STORAGE,
        subject_id=owner,
        relative_path="rki/Bulletins/Jahre/2026/PDF/artifact-a.pdf",
        message="storage changed",
    )
    monkeypatch.setattr(reconciliation, "compare_remote_sources", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(
        reconciliation,
        "reconcile_storage",
        lambda *_args, **_kwargs: (storage_finding,),
    )
    monkeypatch.setattr(reconciliation, "reconcile_rights", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(reconciliation, "reconcile_periods", lambda *_args, **_kwargs: ())

    result = plan_reconciliation(
        as_of=_RECONCILIATION_AS_OF,
        from_year=2026,
        to_year=2026,
        catalog=catalog,
        remote_records=(),
        adapters={},
        period_root=tmp_path / "periods",
        authority=load_rights_authority(),
        policy=load_rights_policy(),
    )

    assert [(item.code, item.subject_kind, item.subject_id) for item in result.findings] == [
        (FindingCode.CHANGED, SubjectKind.STORAGE, owner),
    ]


def _successful_reconciliation_result(as_of: datetime = _RECONCILIATION_AS_OF) -> ReconciliationResult:
    return build_reconciliation_result(
        as_of=as_of,
        from_year=1996,
        to_year=1996,
        source_manifest_sha256="a" * 64,
        findings=(finding(FindingCode.OK, "source-a"),),
    )


def test_materialize_reconciliation_writes_valid_immutable_report(tmp_path: Path) -> None:
    result = _successful_reconciliation_result()
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    materialization = materialize_reconciliation(result, temp_root=tmp_path, ledger=ledger)

    expected = tmp_path / "rki/Bulletins/Manifeste/Reconciliation/reconciliation-20260804T040506Z.json"
    assert materialization == ReconciliationMaterialization(result, expected, True)
    validate_document("reconciliation-report", json.loads(expected.read_bytes()))
    assert expected.read_bytes() == stable_json_dumps(result.report).encode("utf-8") + b"\n"
    assert [(event.kind, event.target) for event in ledger.events] == [
        (EffectKind.TEMP_FILE, expected.absolute().as_posix()),
    ]


def test_materialize_reconciliation_reuses_identical_immutable_report(tmp_path: Path) -> None:
    result = _successful_reconciliation_result()
    first_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)
    first = materialize_reconciliation(result, temp_root=tmp_path, ledger=first_ledger)
    assert first.path is not None
    mtime = first.path.stat().st_mtime_ns
    second_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    second = materialize_reconciliation(result, temp_root=tmp_path, ledger=second_ledger)

    assert second == ReconciliationMaterialization(result, first.path, False)
    assert first.path.stat().st_mtime_ns == mtime
    assert second_ledger.events == []


def test_materialize_reconciliation_skips_blocked_result(tmp_path: Path) -> None:
    blocked = build_reconciliation_result(
        as_of=_RECONCILIATION_AS_OF,
        from_year=1996,
        to_year=1996,
        source_manifest_sha256="a" * 64,
        findings=(finding(FindingCode.CHANGED, "source-a"),),
    )
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    materialization = materialize_reconciliation(blocked, temp_root=tmp_path, ledger=ledger)

    assert materialization == ReconciliationMaterialization(blocked, None, False)
    assert not (tmp_path / "rki").exists()
    assert ledger.events == []


def test_materialize_reconciliation_rejects_different_existing_report(tmp_path: Path) -> None:
    result = _successful_reconciliation_result()
    path = tmp_path / "rki/Bulletins/Manifeste/Reconciliation/reconciliation-20260804T040506Z.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"different")
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    with pytest.raises(ReconciliationIntegrityError, match="unveränderlich"):
        materialize_reconciliation(result, temp_root=tmp_path, ledger=ledger)

    assert path.read_bytes() == b"different"
    assert ledger.events == []


@pytest.mark.parametrize(
    "mutate",
    (
        lambda report: report.update(conclusion="blocked"),
        lambda report: report.update(as_of="2026-08-04T04:05:07Z"),
        lambda report: report["counts"].update(ok=2),
        lambda report: report.update(source_manifest_sha256="b" * 64),
    ),
    ids=("conclusion", "as-of", "counts", "source-hash"),
)
def test_materialize_reconciliation_rejects_report_inconsistent_with_result(
    tmp_path: Path,
    mutate,
) -> None:
    result = _successful_reconciliation_result()
    mutate(result.report)
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    with pytest.raises(ReconciliationIntegrityError, match="Bericht"):
        materialize_reconciliation(result, temp_root=tmp_path, ledger=ledger)

    assert not (tmp_path / "rki").exists()
    assert ledger.events == []


def test_materialize_reconciliation_rolls_back_atomic_write_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    old = tmp_path / "rki/Bulletins/Manifeste/Reconciliation/reconciliation-20260803T040506Z.json"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"prior report")
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    def reject(*_args, **_kwargs) -> NoReturn:
        raise OSError("injected write error")

    monkeypatch.setattr(reconciliation, "atomic_write_bytes", reject)
    with pytest.raises(OSError, match="injected"):
        materialize_reconciliation(_successful_reconciliation_result(), temp_root=tmp_path, ledger=ledger)

    assert old.read_bytes() == b"prior report"
    assert not (old.parent / "reconciliation-20260804T040506Z.json").exists()
    assert ledger.events == []


def test_materialize_reconciliation_removes_report_after_postwrite_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    old = tmp_path / "rki/Bulletins/Manifeste/Reconciliation/reconciliation-20260803T040506Z.json"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"prior report")
    original = reconciliation.atomic_write_bytes
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    def write_then_fail(*args, **kwargs) -> None:
        original(*args, **kwargs)
        raise OSError("injected post-write error")

    monkeypatch.setattr(reconciliation, "atomic_write_bytes", write_then_fail)
    with pytest.raises(OSError, match="post-write"):
        materialize_reconciliation(_successful_reconciliation_result(), temp_root=tmp_path, ledger=ledger)

    assert old.read_bytes() == b"prior report"
    assert not (old.parent / "reconciliation-20260804T040506Z.json").exists()
    assert ledger.events == []


def test_materialize_reconciliation_preserves_concurrent_different_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    original = reconciliation.os.link
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    def create_conflict(source, destination, *args, **kwargs) -> None:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644,
            dir_fd=kwargs["dst_dir_fd"],
        )
        try:
            os.write(descriptor, b"concurrent different report")
        finally:
            os.close(descriptor)
        original(source, destination, *args, **kwargs)

    monkeypatch.setattr(reconciliation.os, "link", create_conflict)
    with pytest.raises(ReconciliationIntegrityError, match="unveränderlich"):
        materialize_reconciliation(_successful_reconciliation_result(), temp_root=tmp_path, ledger=ledger)

    target = tmp_path / "rki/Bulletins/Manifeste/Reconciliation/reconciliation-20260804T040506Z.json"
    assert target.read_bytes() == b"concurrent different report"
    assert ledger.events == []


@pytest.mark.parametrize("mode", (RunMode.PLAN, RunMode.APPLY))
def test_materialize_reconciliation_rejects_nonmaterialize_ledger(
    tmp_path: Path,
    mode: RunMode,
) -> None:
    with pytest.raises(ReconciliationIntegrityError, match="MATERIALIZE"):
        materialize_reconciliation(
            _successful_reconciliation_result(),
            temp_root=tmp_path,
            ledger=EffectLedger(mode),
        )


def test_materialize_reconciliation_rejects_mismatched_temp_root(tmp_path: Path) -> None:
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path / "other")

    with pytest.raises(ReconciliationIntegrityError, match="temp_root"):
        materialize_reconciliation(_successful_reconciliation_result(), temp_root=tmp_path, ledger=ledger)


def test_materialize_reconciliation_rejects_symlinked_report_directory(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    report_parent = tmp_path / "rki/Bulletins/Manifeste"
    report_parent.mkdir(parents=True)
    (report_parent / "Reconciliation").symlink_to(target, target_is_directory=True)
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=tmp_path)

    with pytest.raises(ValueError, match="Symlink"):
        materialize_reconciliation(_successful_reconciliation_result(), temp_root=tmp_path, ledger=ledger)

    assert ledger.events == []


LOCAL_SOURCE_SHA256 = hashlib.sha256(b"local source").hexdigest()
_CANDIDATE_DECISION_SHA256 = "d" * 64


def remote_record(
    *,
    item_handle: str = "176904/900000001",
    pdf_url: str | None = (
        "https://edoc.rki.de/bitstream/handle/176904/900000001/source.pdf?sequence=1"
    ),
    sha256: str | None = LOCAL_SOURCE_SHA256,
    publication_date: str = "2026-07-10",
    year: int = 2026,
    rights: RightsMetadata | None = None,
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
        publication_date=publication_date,
        year=year,
        doi=None,
        rights=rights
        or RightsMetadata(
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


def test_remote_bitstream_identity_replacement_is_bounded_changed(tmp_path: Path) -> None:
    matching = remote_record()
    replacement = replace(matching, pdf_url=matching.pdf_url.replace("sequence=1", "sequence=2"))
    calls: list[str] = []

    def loader(record: ArtifactRecord) -> PreparedObject:
        calls.append(record.source_id)
        return candidate_prepared_object(tmp_path, record=record)

    findings = compare_remote_sources(
        remote_catalog(matching), (replacement,), candidate_loader=loader
    )

    assert calls == [replacement.source_id]
    assert [(item.code, item.subject_id) for item in findings] == [
        (
            FindingCode.CHANGED,
            source_subject_id(matching.source_id, bitstream_identity(matching.pdf_url).bitstream_id),
        )
    ]


def test_remote_partial_bitstream_replacement_preserves_exact_match_and_changes_unmatched(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    first = remote_record()
    second = replace(first, pdf_url=first.pdf_url.replace("sequence=1", "sequence=2"))
    replacement = replace(first, pdf_url=first.pdf_url.replace("sequence=1", "sequence=3"))
    calls: list[str] = []

    def loader(record: ArtifactRecord) -> PreparedObject:
        calls.append(record.pdf_url or "")
        return candidate_prepared_object(tmp_path, record=record)

    monkeypatch.setattr(reconciliation, "reconcile_storage", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(reconciliation, "reconcile_rights", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(reconciliation, "reconcile_periods", lambda *_args, **_kwargs: ())
    catalog = remote_catalog(first, second)

    result = plan_reconciliation(
        as_of=_RECONCILIATION_AS_OF,
        from_year=2026,
        to_year=2026,
        catalog=catalog,
        remote_records=(first, replacement),
        adapters={},
        period_root=tmp_path / "periods",
        authority=load_rights_authority(),
        policy=load_rights_policy(),
        candidate_loader=loader,
    )

    first_subject = source_subject_id(first.source_id, bitstream_identity(first.pdf_url).bitstream_id)
    second_subject = source_subject_id(second.source_id, bitstream_identity(second.pdf_url).bitstream_id)
    assert calls == [replacement.pdf_url]
    assert [(item.code, item.subject_id) for item in result.findings] == [
        (FindingCode.CHANGED, second_subject),
        (FindingCode.OK, first_subject),
    ]


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
        lambda record: replace(record, etag='"changed"'),
        lambda record: replace(record, last_modified="Sat, 11 Jul 2026 00:00:00 GMT"),
        lambda record: replace(record, sha256="a" * 64),
    ),
    ids=("version", "source-url", "bitstream-url", "etag", "last-modified", "supplied-hash"),
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


@pytest.mark.parametrize(
    "changed",
    (
        lambda record: replace(record, publication_date="2026-07-11"),
        lambda record: replace(
            record,
            rights=RightsMetadata(
                label=record.rights.label,
                uri=record.rights.uri,
                copyright_notice="Changed notice",
                open_access=record.rights.open_access,
            ),
        ),
    ),
    ids=("publication-date", "rights-evidence"),
)
def test_metadata_only_drift_does_not_load_candidate(tmp_path: Path, changed) -> None:
    matching = remote_record()
    calls: list[str] = []

    def loader(record: ArtifactRecord) -> PreparedObject:
        calls.append(record.source_id)
        return candidate_prepared_object(tmp_path, record=record)

    findings = compare_remote_sources(
        remote_catalog(matching),
        (changed(matching),),
        candidate_loader=loader,
    )

    assert calls == []
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


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("as_of", datetime(2026, 8, 4, 4, 0, 0, 1, tzinfo=timezone.utc)),
        ("from_year", 2027),
        ("catalog", object()),
        ("remote_records", []),
        ("adapters", []),
        ("period_root", "periods"),
        ("authority", object()),
        ("policy", object()),
        ("candidate_loader", object()),
    ),
)
def test_plan_rejects_every_malformed_top_level_input_before_callbacks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    from scripts.rki_pipeline import reconciliation

    callbacks: list[str] = []

    def called(*_args, **_kwargs):
        callbacks.append("called")
        return ()

    monkeypatch.setattr(reconciliation, "compare_remote_sources", called)
    monkeypatch.setattr(reconciliation, "reconcile_storage", called)
    monkeypatch.setattr(reconciliation, "reconcile_rights", called)
    monkeypatch.setattr(reconciliation, "reconcile_periods", called)
    arguments = {
        "as_of": _RECONCILIATION_AS_OF,
        "from_year": 2026,
        "to_year": 2026,
        "catalog": remote_catalog(),
        "remote_records": (),
        "adapters": {},
        "period_root": tmp_path / "periods",
        "authority": load_rights_authority(),
        "policy": load_rights_policy(),
        "candidate_loader": None,
    }
    arguments[field] = value

    with pytest.raises((ValueError, TypeError)):
        plan_reconciliation(**arguments)

    assert callbacks == []


def test_plan_rejects_invalid_remote_snapshot_before_adapters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    record = remote_record()
    callbacks: list[str] = []
    monkeypatch.setattr(
        reconciliation,
        "reconcile_storage",
        lambda *_args, **_kwargs: callbacks.append("storage") or (),
    )

    with pytest.raises(ValueError, match="doppelt"):
        plan_reconciliation(
            as_of=_RECONCILIATION_AS_OF,
            from_year=2026,
            to_year=2026,
            catalog=remote_catalog(record),
            remote_records=(record, record),
            adapters={},
            period_root=tmp_path / "periods",
            authority=load_rights_authority(),
            policy=load_rights_policy(),
        )

    assert callbacks == []


def test_plan_scope_excludes_out_of_scope_graph_remote_drift_and_candidate_load(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from scripts.rki_pipeline import reconciliation

    current = remote_record()
    excluded = remote_record(
        item_handle="176904/900000002",
        pdf_url="https://edoc.rki.de/bitstream/handle/176904/900000002/source.pdf?sequence=1",
        publication_date="2025-07-10",
        year=2025,
    )
    catalog = remote_catalog(current, excluded)
    references = tuple(
        replace(
            storage_reference(record.document_id),
            relative_path=document["paths"]["pdf"],
            source_id=record.source_id,
            source_sha256=record.sha256 or "0" * 64,
            document_id=record.document_id,
        )
        for record, document in zip(
            (current, excluded),
            catalog.graph.documents,
            strict=True,
        )
    )
    graph = ManifestGraph(
        sources=catalog.graph.sources,
        documents=catalog.graph.documents,
        conversions=catalog.graph.conversions,
        storage_references=tuple(reference.to_dict() for reference in references),
    )
    catalog = LoadedManifestCatalog(graph=graph, rendered=render_manifest_catalog(graph))
    storage_graphs: list[ManifestGraph] = []
    rights_graphs: list[ManifestGraph] = []
    monkeypatch.setattr(
        reconciliation,
        "reconcile_storage",
        lambda scoped, *_args, **_kwargs: storage_graphs.append(scoped) or (),
    )
    monkeypatch.setattr(
        reconciliation,
        "reconcile_rights",
        lambda scoped, *_args, **_kwargs: rights_graphs.append(scoped) or (),
    )
    monkeypatch.setattr(reconciliation, "reconcile_periods", lambda *_args, **_kwargs: ())
    calls: list[str] = []

    result = plan_reconciliation(
        as_of=_RECONCILIATION_AS_OF,
        from_year=2026,
        to_year=2026,
        catalog=catalog,
        remote_records=(current, replace(excluded, etag='"drift"')),
        adapters={},
        period_root=tmp_path / "periods",
        authority=load_rights_authority(),
        policy=load_rights_policy(),
        candidate_loader=lambda record: calls.append(record.source_id),
    )

    assert result.conclusion == "success"
    assert result.counts == ReconciliationCounts(1, 0, 0, 0, 0, 0, 0)
    assert calls == []
    assert len(storage_graphs) == len(rights_graphs) == 1
    for scoped in (*storage_graphs, *rights_graphs):
        assert [document["document_id"] for document in scoped.documents] == [current.document_id]
        assert [source["source_id"] for source in scoped.sources] == [current.source_id]
        assert [reference["document_id"] for reference in scoped.storage_references] == [
            current.document_id
        ]
