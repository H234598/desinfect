"""Deterministic reconciliation findings and report contract."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import TemporaryDirectory
from typing import Callable, Iterable, Mapping, TypeAlias
import unicodedata
import uuid

from scripts.rki_grabber.models import (
    AffectedPeriods,
    ArtifactRecord,
    RecordState,
    RightsMetadata,
    Scope,
)
from scripts.rki_grabber.download import PdfDownloadError
from scripts.rki_grabber.http import GrabberHttpError
from scripts.rki_pipeline.aggregation import (
    AggregationError,
    PeriodManifestError,
    PeriodPlan,
    PeriodPublicationMissing,
    materialize_period_archives,
    plan_period_archives,
    PeriodRef,
    inspect_period_publication,
    period_ref,
)
from scripts.rki_pipeline.archive import ArchiveError, archive_input_fingerprint
from scripts.rki_pipeline.documents import DocumentIdentityError, bitstream_identity
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind
from scripts.rki_pipeline.io_utils import (
    atomic_write_bytes,
    fsync_directory_fd,
    normalize_posix_path,
    open_directory_beneath,
    open_root_directory,
    relative_path_beneath,
    stable_json_dumps,
)
from scripts.rki_pipeline.manifests import (
    LoadedManifestCatalog,
    ManifestGraph,
    ManifestGraphError,
    RenderedManifestCatalog,
    build_manifest_graph,
    render_manifest_catalog,
    storage_reference_from_manifest,
)
from scripts.rki_pipeline.rights import (
    ApprovalKey,
    RightsAction,
    RightsAuthority,
    RightsDecision,
    RightsPolicy,
    RightsPolicyError,
    RightsState,
    load_authority_register,
    load_fixture_rights_authority,
    load_rights_policy,
    resolve_action,
)
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    RightsStorageAuthorizer,
    StorageAdapter,
    StorageAuthorizationError,
    StorageBackend,
    StorageError,
    StorageReference,
)
from scripts.rki_pipeline.source_manifest import (
    ManifestBuildError,
    build_document_manifest,
    build_source_manifests,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FIXTURE_MAX_BYTES = 64 * 1024
_FIXTURE_AS_OF = "2026-08-04T04:00:00Z"
_METADATA_FIELDS = (
    ("version", "version"),
    ("source_url", "item_url"),
    ("bitstream_url", "pdf_url"),
    ("etag", "etag"),
    ("last_modified", "last_modified"),
    ("publication_date", "publication_date"),
)
_CANDIDATE_METADATA_FIELDS = (
    ("version", "version"),
    ("source_url", "item_url"),
    ("bitstream_url", "pdf_url"),
    ("etag", "etag"),
    ("last_modified", "last_modified"),
)

CandidateLoader: TypeAlias = Callable[[ArtifactRecord], PreparedObject]
_DOWNLOADABLE_STATES = frozenset(
    {
        RecordState.PLANNED,
        RecordState.EXISTING,
        RecordState.DOWNLOADED,
        RecordState.RESUMED,
    }
)


class RemoteSnapshotError(ValueError):
    """A remote record cannot be compared safely."""


class ReconciliationIntegrityError(ValueError):
    """A reconciliation input or adapter contract is inconsistent."""


class FindingCode(StrEnum):
    NEW = "new"
    CHANGED = "changed"
    MISSING_REMOTE = "missing_remote"
    MISSING_LOCAL = "missing_local"
    ORPHAN = "orphan"
    RIGHTS_CHANGED = "rights_changed"
    OK = "ok"


class SubjectKind(StrEnum):
    SOURCE = "source"
    STORAGE = "storage"
    PERIOD = "period"
    ARCHIVE = "archive"


@dataclass(frozen=True, slots=True)
class ReconciliationFinding:
    code: FindingCode
    subject_kind: SubjectKind
    subject_id: str
    relative_path: str | None
    message: str

    def __post_init__(self) -> None:
        if type(self.code) is not FindingCode or type(self.subject_kind) is not SubjectKind:
            raise ValueError("Finding-Code und Subject-Kind müssen kanonisch sein")
        if type(self.subject_id) is not str or _has_control_character(self.subject_id):
            raise ValueError("subject_id enthält Steuerzeichen oder ist ungültig")
        if self.relative_path is not None:
            if type(self.relative_path) is not str or _has_control_character(self.relative_path):
                raise ValueError("relative_path ist nicht relativ oder enthält Steuerzeichen")
            try:
                normalized_path = normalize_posix_path(self.relative_path)
            except ValueError as exc:
                raise ValueError("relative_path ist nicht kanonisch") from exc
            if normalized_path != self.relative_path:
                raise ValueError("relative_path ist nicht kanonisch")
        if (
            type(self.message) is not str
            or len(self.message) > 500
            or _has_control_character(self.message)
        ):
            raise ValueError("message ist ungültig")

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (
            self.code.value,
            self.subject_kind.value,
            self.subject_id,
            self.relative_path or "",
        )


@dataclass(frozen=True, slots=True)
class ReconciliationCounts:
    ok: int
    changed: int
    missing_remote: int
    missing_local: int
    orphan: int
    rights_changed: int
    unresolved: int

    def __post_init__(self) -> None:
        for value in (
            self.ok,
            self.changed,
            self.missing_remote,
            self.missing_local,
            self.orphan,
            self.rights_changed,
            self.unresolved,
        ):
            if type(value) is not int or value < 0:
                raise ValueError("Counts müssen nichtnegative Ganzzahlen sein")

    def to_dict(self) -> dict[str, int]:
        return {
            "ok": self.ok,
            "changed": self.changed,
            "missing_remote": self.missing_remote,
            "missing_local": self.missing_local,
            "orphan": self.orphan,
            "rights_changed": self.rights_changed,
            "unresolved": self.unresolved,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    findings: tuple[ReconciliationFinding, ...]
    counts: ReconciliationCounts
    conclusion: str
    source_manifest_sha256: str
    report: dict[str, object]
    successful_at: datetime | None


@dataclass(frozen=True, slots=True)
class ReconciliationMaterialization:
    result: ReconciliationResult
    path: Path | None
    changed: bool


def source_subject_id(source_id: str, bitstream_id: str) -> str:
    return f"{source_id}#{bitstream_id}"


def compare_remote_sources(
    catalog: LoadedManifestCatalog,
    remote_records: tuple[ArtifactRecord, ...],
    *,
    candidate_loader: CandidateLoader | None = None,
) -> tuple[ReconciliationFinding, ...]:
    """Compare current manifest sources with a remote metadata snapshot."""

    if type(catalog) is not LoadedManifestCatalog or type(remote_records) is not tuple:
        raise ReconciliationIntegrityError("Remote-Vergleichseingaben sind ungültig")
    local_sources = _current_source_projection(catalog)
    remote_sources = _remote_source_index(remote_records)
    findings: list[ReconciliationFinding] = []
    exact_keys = local_sources.keys() & remote_sources.keys()
    unmatched_local: dict[str, list[tuple[str, str]]] = {}
    unmatched_remote: dict[str, list[tuple[str, str]]] = {}
    for key in local_sources.keys() - exact_keys:
        unmatched_local.setdefault(key[0], []).append(key)
    for key in remote_sources.keys() - exact_keys:
        unmatched_remote.setdefault(key[0], []).append(key)
    replacements: dict[tuple[str, str], tuple[str, str]] = {}
    for source_id in unmatched_local.keys() & unmatched_remote.keys():
        local_keys = unmatched_local[source_id]
        remote_keys = unmatched_remote[source_id]
        if len(local_keys) == len(remote_keys) == 1:
            replacements[remote_keys[0]] = local_keys[0]
    replaced_local_keys = set(replacements.values())

    for key, record in remote_sources.items():
        local = local_sources.get(key)
        subject_id = source_subject_id(*key)
        if local is None:
            replacement_key = replacements.get(key)
            if replacement_key is not None:
                _load_candidate_if_available(candidate_loader, record)
                findings.append(
                    _source_finding(
                        FindingCode.CHANGED,
                        source_subject_id(*replacement_key),
                        "Remote-Bitstreamidentität driftet",
                    )
                )
                continue
            findings.append(_source_finding(FindingCode.NEW, subject_id, "Remote-Quelle ist neu"))
            continue
        if _metadata_drifts(local, record):
            if _candidate_load_is_justified(local, record):
                _load_candidate_if_available(candidate_loader, record)
            findings.append(
                _source_finding(FindingCode.CHANGED, subject_id, "Remote-Metadaten driften")
            )

    for key in local_sources.keys() - remote_sources.keys() - replaced_local_keys:
        findings.append(
            _source_finding(
                FindingCode.MISSING_REMOTE,
                source_subject_id(*key),
                "Remote-Quelle fehlt",
            )
        )

    return tuple(sorted(findings, key=lambda item: item.key))


def reconcile_storage(
    graph: ManifestGraph,
    adapters: Mapping[StorageBackend, StorageAdapter],
    *,
    complete_graph: ManifestGraph | None = None,
) -> tuple[ReconciliationFinding, ...]:
    """Verify persisted storage references and detect adapter inventory orphans."""

    references = _manifest_storage_references(graph)
    complete_references = (
        references
        if complete_graph is None
        else _manifest_storage_references(complete_graph)
    )
    checked_adapters = _storage_adapters(adapters)
    findings: list[ReconciliationFinding] = []

    for reference in references:
        owner = _storage_reference_owner(graph, reference)
        if owner is None:
            if reference.source_id is None:
                raise ReconciliationIntegrityError("Storage-Manifest hat keinen kanonischen Owner")
            continue
        adapter = checked_adapters.get(reference.storage_backend)
        if adapter is None:
            findings.append(_storage_finding(FindingCode.MISSING_LOCAL, reference, owner))
            continue
        try:
            adapter.verify(reference)
        except FileNotFoundError:
            findings.append(_storage_finding(FindingCode.MISSING_LOCAL, reference, owner))
        except StorageAuthorizationError:
            findings.append(_storage_finding(FindingCode.RIGHTS_CHANGED, reference, owner))
        except StorageError:
            findings.append(_storage_finding(FindingCode.CHANGED, reference, owner))
        except Exception as exc:
            raise ReconciliationIntegrityError("Storage-Adapter-Vertrag ist ungültig") from exc

    inventory_ids: set[str] = set()
    by_artifact_id = {reference.artifact_id: reference for reference in references}
    by_backend_path = {
        (reference.storage_backend, reference.relative_path): reference
        for reference in references
    }
    complete_artifact_ids = {reference.artifact_id for reference in complete_references}
    complete_backend_paths = {
        (reference.storage_backend, reference.relative_path)
        for reference in complete_references
    }
    for backend, adapter in checked_adapters.items():
        try:
            inventory = adapter.list_references()
        except Exception as exc:
            raise ReconciliationIntegrityError("Storage-Inventar ist nicht prüfbar") from exc
        if type(inventory) is not tuple:
            raise ReconciliationIntegrityError("Storage-Inventar ist kein Tupel")
        for reference in sorted(inventory, key=lambda item: item.artifact_id):
            if type(reference) is not StorageReference:
                raise ReconciliationIntegrityError("Storage-Inventar enthält keine Referenz")
            if reference.storage_backend is not backend:
                raise ReconciliationIntegrityError("Storage-Inventar gehört zum falschen Backend")
            if reference.artifact_id in inventory_ids:
                raise ReconciliationIntegrityError("Storage-Artefaktidentität ist doppelt")
            inventory_ids.add(reference.artifact_id)
            expected = by_artifact_id.get(reference.artifact_id)
            if expected is not None and reference != expected:
                raise ReconciliationIntegrityError("Storage-Artefaktidentität ist widersprüchlich")
            if (
                expected is None
                and backend is StorageBackend.LFS
                and reference.provenance_state == "legacy_needs_review"
            ):
                expected = by_backend_path.get((backend, reference.relative_path))
            if expected is None:
                owner = _storage_reference_owner(graph, reference)
                outside_scope = reference.artifact_id in complete_artifact_ids or (
                    backend is StorageBackend.LFS
                    and reference.provenance_state == "legacy_needs_review"
                    and (backend, reference.relative_path) in complete_backend_paths
                )
                if complete_graph is not None and outside_scope:
                    continue
                findings.append(_storage_finding(FindingCode.ORPHAN, reference, owner))

    return tuple(sorted(findings, key=lambda item: item.key))


def reconcile_rights(
    graph: ManifestGraph,
    *,
    authority: RightsAuthority,
    policy: RightsPolicy,
) -> tuple[ReconciliationFinding, ...]:
    """Compare persisted source and storage rights decisions with current policy."""

    _require_graph(graph)
    references = _manifest_storage_references(graph)
    findings: list[ReconciliationFinding] = []
    decisions: dict[str, RightsDecision | None] = {}
    source_changed: dict[str, bool] = {}

    current_sources = _current_source_projection_from_graph(graph)
    for owner_key, source in sorted(current_sources.items()):
        try:
            source_id = source["source_id"]
            source_sha256 = source["sha256"]
            bitstream_id = source["bitstream_id"]
            persisted_hash = source["decision_sha256"]
            persisted_state = source["rights"]["state"]
        except (KeyError, TypeError) as exc:
            raise ReconciliationIntegrityError("Source-Rechteverknüpfung ist ungültig") from exc
        if not all(type(value) is str for value in (
            source_id,
            source_sha256,
            bitstream_id,
            persisted_hash,
            persisted_state,
        )):
            raise ReconciliationIntegrityError("Source-Rechteverknüpfung ist ungültig")
        subject_id = source_subject_id(*owner_key)
        try:
            approval_key = ApprovalKey(
                source_id=source_id,
                canonical_url=source["bitstream_url"],
                version_or_bitstream=bitstream_id,
                source_sha256=source_sha256,
            )
            register = load_authority_register(authority)
            decision = next(
                (
                    entry
                    for entry in register.entries
                    if entry.approval_key == approval_key
                ),
                None,
            )
        except (AttributeError, RightsPolicyError) as exc:
            raise ReconciliationIntegrityError("Rechteentscheidung ist nicht prüfbar") from exc
        if decision is not None and type(decision) is not RightsDecision:
            raise ReconciliationIntegrityError("Rechteentscheidung ist ungültig")
        decisions[subject_id] = decision
        changed = (
            decision is None
            or decision.decision_sha256 != persisted_hash
            or decision.state.value != persisted_state
            or decision.state is not RightsState.APPROVED
        )
        source_changed[subject_id] = changed
        if changed:
            findings.append(
                _source_finding(
                    FindingCode.RIGHTS_CHANGED,
                    subject_id,
                    "Aktuelle Rechteentscheidung weicht ab",
                )
            )

    for reference in references:
        owner = _storage_reference_owner(graph, reference)
        if owner is None:
            continue
        if reference.source_id is None or reference.source_sha256 is None:
            raise ReconciliationIntegrityError("Storage-Rechteverknüpfung ist ungültig")
        if owner not in decisions:
            raise ReconciliationIntegrityError("Storage-Source-Rechteverknüpfung fehlt")
        decision = decisions[owner]
        if (
            source_changed[owner]
            or decision is None
            or decision.decision_sha256 != reference.decision_sha256
            or decision.state.value != reference.rights_state
        ):
            findings.append(_storage_finding(FindingCode.RIGHTS_CHANGED, reference, owner))

    return tuple(sorted(findings, key=lambda item: item.key))


def reconcile_periods(
    graph: ManifestGraph,
    period_root: Path,
) -> tuple[ReconciliationFinding, ...]:
    """Verify required period manifests, memberships, formats, and archive bundles."""

    _require_graph(graph)
    if not isinstance(period_root, Path):
        raise ReconciliationIntegrityError("Periodenwurzel ist ungültig")
    storage_by_path: dict[str, dict[str, object]] = {}
    for reference in graph.storage_references:
        if type(reference) is not dict:
            raise ReconciliationIntegrityError("Storage-Manifest ist ungültig")
        relative_path = reference.get("relative_path")
        if type(relative_path) is not str or relative_path in storage_by_path:
            raise ReconciliationIntegrityError("Storage-Pfadidentität ist ungültig")
        storage_by_path[relative_path] = reference

    expected: dict[
        tuple[TaskKind, str],
        dict[tuple[str, str], tuple[str, str, str | None, str | None, str | None, str | None]],
    ] = {}
    identities: set[tuple[str, str]] = set()
    periods: dict[tuple[TaskKind, str], PeriodRef] = {}
    owners: dict[tuple[TaskKind, str], set[str]] = {}
    try:
        documents = sorted(
            graph.documents,
            key=lambda item: (str(item.get("document_id")), str(item.get("bitstream_id"))),
        )
    except AttributeError as exc:
        raise ReconciliationIntegrityError("Dokumentmanifest ist ungültig") from exc
    for document in documents:
        if type(document) is not dict:
            raise ReconciliationIntegrityError("Dokumentmanifest ist ungültig")
        if document.get("superseded_by") is not None:
            continue
        try:
            document_id = document["document_id"]
            bitstream_id = document["bitstream_id"]
            source_id = document["source_id"]
            publication_date = document["publication_date"]
            paths = document["paths"]
        except KeyError as exc:
            raise ReconciliationIntegrityError("Dokumentperiodenverknüpfung ist unvollständig") from exc
        if not all(type(value) is str for value in (document_id, bitstream_id, source_id)):
            raise ReconciliationIntegrityError("Dokumentperiodenidentität ist ungültig")
        if type(paths) is not dict:
            raise ReconciliationIntegrityError("Dokumentpfade sind ungültig")
        identity = (document_id, bitstream_id)
        if identity in identities:
            raise ReconciliationIntegrityError("Dokument-/Bitstream-Identität ist doppelt")
        identities.add(identity)
        document_periods = _document_periods(document, publication_date)
        pdf_id, pdf_sha256 = _period_artifact(paths.get("pdf"), storage_by_path, document_id)
        markdown_id, markdown_sha256 = _period_artifact(
            paths.get("markdown"),
            storage_by_path,
            document_id,
        )
        row = (
            source_id,
            document_id,
            pdf_id,
            pdf_sha256,
            markdown_id,
            markdown_sha256,
        )
        for period in document_periods:
            key = (period.kind, period.value)
            periods[key] = period
            owners.setdefault(key, set()).add(source_subject_id(source_id, bitstream_id))
            period_documents = expected.setdefault(key, {})
            if identity in period_documents:
                raise ReconciliationIntegrityError("Periodenmitgliedschaft ist doppelt")
            period_documents[identity] = row

    expected_plans = _expected_period_plans(graph, period_root)
    findings: list[ReconciliationFinding] = []
    for key in sorted(expected, key=lambda item: (item[0].value, item[1])):
        period = periods[key]
        relative_path = _period_manifest_path(period)
        try:
            inspection = inspect_period_publication(period_root, period)
        except PeriodPublicationMissing as exc:
            findings.extend(
                _period_findings(
                    FindingCode.MISSING_LOCAL,
                    owners[key],
                    exc.relative_path,
                )
            )
            continue
        except FileNotFoundError:
            findings.extend(
                _period_findings(FindingCode.MISSING_LOCAL, owners[key], relative_path)
            )
            continue
        except (ArchiveError, PeriodManifestError):
            findings.extend(
                _period_findings(FindingCode.CHANGED, owners[key], relative_path)
            )
            continue
        expected_plan = expected_plans.get(key)
        matches = (
            _period_plan_matches(inspection.manifest, expected_plan, expected_plans)
            if expected_plan is not None
            else _period_membership_matches(inspection.manifest, period, expected[key])
        )
        if not matches:
            findings.extend(
                _period_findings(FindingCode.CHANGED, owners[key], relative_path)
            )
    return tuple(sorted(findings, key=lambda item: item.key))


def _document_periods(
    document: dict[str, object],
    publication_date: object,
) -> tuple[PeriodRef, ...]:
    if publication_date is None:
        canonical = document.get("canonical_periods")
        year = canonical.get("year") if type(canonical) is dict else document.get("year")
        if type(year) is not int:
            raise ReconciliationIntegrityError("Dokumentjahr ist ungültig")
        try:
            return (period_ref(TaskKind.YEAR, f"{year:04d}"),)
        except ValueError as exc:
            raise ReconciliationIntegrityError("Dokumentjahr ist ungültig") from exc
    if type(publication_date) is not str:
        raise ReconciliationIntegrityError("Dokumentdatum ist ungültig")
    try:
        published = date.fromisoformat(publication_date)
    except ValueError as exc:
        raise ReconciliationIntegrityError("Dokumentdatum ist ungültig") from exc
    if published.isoformat() != publication_date:
        raise ReconciliationIntegrityError("Dokumentdatum ist nicht kanonisch")
    iso = published.isocalendar()
    try:
        return (
            period_ref(TaskKind.WEEK, f"{iso.year:04d}-W{iso.week:02d}"),
            period_ref(TaskKind.MONTH, f"{published.year:04d}-{published.month:02d}"),
            period_ref(TaskKind.YEAR, f"{published.year:04d}"),
        )
    except ValueError as exc:
        raise ReconciliationIntegrityError("Dokumentperioden sind ungültig") from exc


def _period_artifact(
    relative_path: object,
    storage_by_path: Mapping[str, dict[str, object]],
    document_id: str,
) -> tuple[str | None, str | None]:
    if relative_path is None:
        return None, None
    if type(relative_path) is not str:
        raise ReconciliationIntegrityError("Dokumentartefaktpfad ist ungültig")
    reference = storage_by_path.get(relative_path)
    if reference is None or reference.get("document_id") != document_id:
        raise ReconciliationIntegrityError("Dokumentartefakt-Storage fehlt oder widerspricht")
    artifact_id = reference.get("artifact_id")
    sha256 = reference.get("sha256")
    if type(artifact_id) is not str or type(sha256) is not str:
        raise ReconciliationIntegrityError("Dokumentartefaktidentität ist ungültig")
    return artifact_id, sha256


def _period_manifest_path(period: PeriodRef) -> str:
    return f"rki/Bulletins/Manifeste/Archive/{period.kind.value}/{period.value}.json"


def _period_findings(
    code: FindingCode,
    owners: Iterable[str],
    relative_path: str,
) -> tuple[ReconciliationFinding, ...]:
    messages = {
        FindingCode.MISSING_LOCAL: "Erforderliche Periodenveröffentlichung fehlt lokal",
        FindingCode.CHANGED: "Periodenveröffentlichung oder Archivintegrität weicht ab",
    }
    return tuple(
        ReconciliationFinding(
            code=code,
            subject_kind=SubjectKind.PERIOD,
            subject_id=owner,
            relative_path=relative_path,
            message=messages[code],
        )
        for owner in sorted(set(owners))
    )


def _expected_period_plans(
    graph: ManifestGraph,
    period_root: Path,
) -> dict[tuple[TaskKind, str], PeriodPlan]:
    documents = tuple(
        document
        for document in graph.documents
        if document.get("superseded_by") is None
        and type(document.get("publication_date")) is str
        and type(document.get("paths")) is dict
        and (
            type(document["paths"].get("pdf")) is str
            or type(document["paths"].get("markdown")) is str
        )
    )
    if not documents:
        return {}
    bitstream_ids = {document["bitstream_id"] for document in documents}
    document_ids = {document["document_id"] for document in documents}
    paths = {
        path
        for document in documents
        for path in document["paths"].values()
        if type(path) is str
    }
    sources = tuple(
        source for source in graph.sources if source.get("bitstream_id") in bitstream_ids
    )
    conversions = tuple(
        conversion
        for conversion in graph.conversions
        if conversion.get("document_id") in document_ids
        and conversion.get("bitstream_id") in bitstream_ids
    )
    conversion_ids = {
        conversion["conversion_id"]
        for conversion in conversions
        if type(conversion.get("conversion_id")) is str
    }
    storage = tuple(
        reference
        for reference in graph.storage_references
        if reference.get("relative_path") in paths
        and (
            reference.get("conversion_id") is None
            or reference.get("conversion_id") in conversion_ids
        )
    )
    planning_graph = ManifestGraph(sources, documents, conversions, storage)
    inspected_root = period_root.resolve()
    # Synthetic placeholders stay outside the inspected publication tree and are never read.
    prepared_root = inspected_root.parent / (
        f".desinfect-reconciliation-expected-{uuid.uuid4().hex}"
    )
    if prepared_root.exists() or prepared_root.is_symlink():
        raise ReconciliationIntegrityError("Synthetische Periodenplanung kollidiert")
    prepared: dict[str, PreparedObject] = {}
    try:
        for reference in planning_graph.storage_references:
            required = {
                "artifact_id",
                "relative_path",
                "sha256",
                "bytes",
                "source_id",
                "source_sha256",
                "decision_sha256",
                "visibility",
                "rights_state",
                "document_id",
                "conversion_id",
            }
            if type(reference) is not dict or not required.issubset(reference):
                raise ReconciliationIntegrityError("Perioden-Storage hat keine aktuelle Provenienz")
            relative_path = reference["relative_path"]
            if type(relative_path) is not str:
                raise ReconciliationIntegrityError("Perioden-Storage-Pfad ist ungültig")
            prepared[relative_path] = PreparedObject(
                artifact_id=reference["artifact_id"],
                logical_key=relative_path,
                path=prepared_root / relative_path,
                temp_root=prepared_root,
                sha256=reference["sha256"],
                size=reference["bytes"],
                source_id=reference["source_id"],
                source_sha256=reference["source_sha256"],
                decision_sha256=reference["decision_sha256"],
                visibility=reference["visibility"],
                rights_state=reference["rights_state"],
                document_id=reference["document_id"],
                conversion_id=reference["conversion_id"],
            )
        affected = AffectedPeriods()
        refs: list[PeriodRef] = []
        for document in documents:
            publication_date = document["publication_date"]
            assert type(publication_date) is str
            affected.add(publication_date, None)
            refs.extend(_document_periods(document, publication_date))
        as_of = datetime.fromtimestamp(
            max(period.source_date_epoch for period in refs),
            tz=timezone.utc,
        )
        plan = plan_period_archives(
            as_of=as_of,
            due_tasks=(),
            affected_periods=affected,
            graph=planning_graph,
            prepared_by_logical_key=prepared,
        )
    except (AggregationError, StorageError, ValueError) as exc:
        raise ReconciliationIntegrityError("Erwartete Periodenplanung ist ungültig") from exc
    return {(item.period.kind, item.period.value): item for item in plan.periods}


def _period_plan_matches(
    manifest: dict[str, object],
    expected: PeriodPlan,
    expected_plans: Mapping[tuple[TaskKind, str], PeriodPlan],
) -> bool:
    expected_documents = sorted(
        (
            {
                "document_id": document.document_id,
                "bitstream_id": document.bitstream_id,
                "doi": document.doi,
                "version": document.version,
                "source_id": document.source_id,
                "publication_date": document.publication_date,
                "pdf_artifact_id": None if document.pdf is None else document.pdf.artifact_id,
                "pdf_sha256": None if document.pdf is None else document.pdf.sha256,
                "markdown_artifact_id": (
                    None if document.markdown is None else document.markdown.artifact_id
                ),
                "markdown_sha256": None if document.markdown is None else document.markdown.sha256,
            }
            for document in expected.documents
        ),
        key=lambda item: (item["publication_date"], item["document_id"], item["bitstream_id"]),
    )
    if manifest.get("documents") != expected_documents:
        return False
    actual_archives = manifest.get("archives")
    if type(actual_archives) is not list:
        return False
    expected_archives = {archive.spec.archive_id: archive for archive in expected.archives}
    if {archive.get("archive_id") for archive in actual_archives if type(archive) is dict} != set(
        expected_archives
    ) or len(actual_archives) != len(expected_archives):
        return False
    for archive in actual_archives:
        if type(archive) is not dict:
            return False
        planned = expected_archives[archive["archive_id"]]
        if (
            archive.get("kind") != planned.spec.kind
            or archive.get("relative_bundle") != planned.relative_bundle
            or archive.get("input_fingerprint") != archive_input_fingerprint(planned.spec)
        ):
            return False
    expected_months = []
    if expected.period.kind is TaskKind.YEAR:
        months = sorted({document.publication_date[:7] for document in expected.documents})
        expected_months = [
            f"rki/Bulletins/Manifeste/Archive/month/{month}.json" for month in months
        ]
        if any((TaskKind.MONTH, month) not in expected_plans for month in months):
            return False
    return manifest.get("month_manifests") == expected_months


def _period_membership_matches(
    manifest: dict[str, object],
    period: PeriodRef,
    expected: Mapping[
        tuple[str, str],
        tuple[str, str, str | None, str | None, str | None, str | None],
    ],
) -> bool:
    documents = manifest["documents"]
    archives = manifest["archives"]
    if type(documents) is not list or type(archives) is not list:
        return False
    actual: dict[
        tuple[str, str],
        tuple[str, str, str | None, str | None, str | None, str | None],
    ] = {}
    for row in documents:
        if type(row) is not dict:
            return False
        identity = (row.get("document_id"), row.get("bitstream_id"))
        if not all(type(value) is str for value in identity) or identity in actual:
            return False
        actual[identity] = (
            row.get("source_id"),
            row.get("document_id"),
            row.get("pdf_artifact_id"),
            row.get("pdf_sha256"),
            row.get("markdown_artifact_id"),
            row.get("markdown_sha256"),
        )
    required_formats = {
        format_name
        for values in expected.values()
        for format_name, artifact_id in (("pdf", values[2]), ("markdown", values[4]))
        if artifact_id is not None
    }
    actual_formats = {
        archive.get("kind", "").removeprefix(f"{period.kind.value}-")
        for archive in archives
        if type(archive) is dict
    }
    return actual == expected and actual_formats == required_formats


def _require_graph(graph: ManifestGraph) -> None:
    if type(graph) is not ManifestGraph:
        raise ReconciliationIntegrityError("Manifestgraph ist ungültig")


def _manifest_storage_references(graph: ManifestGraph) -> tuple[StorageReference, ...]:
    _require_graph(graph)
    references: list[StorageReference] = []
    artifact_ids: set[str] = set()
    try:
        manifests = sorted(graph.storage_references, key=lambda item: item["artifact_id"])
    except (KeyError, TypeError) as exc:
        raise ReconciliationIntegrityError("Storage-Manifest ist ungültig") from exc
    for manifest in manifests:
        try:
            reference = storage_reference_from_manifest(manifest)
        except (KeyError, TypeError, ValueError) as exc:
            raise ReconciliationIntegrityError("Storage-Manifest ist ungültig") from exc
        if reference.artifact_id in artifact_ids:
            raise ReconciliationIntegrityError("Storage-Artefaktidentität ist doppelt")
        artifact_ids.add(reference.artifact_id)
        references.append(reference)
    return tuple(references)


def _storage_adapters(
    adapters: Mapping[StorageBackend, StorageAdapter],
) -> dict[StorageBackend, StorageAdapter]:
    if not isinstance(adapters, Mapping):
        raise ReconciliationIntegrityError("Storage-Adapterzuordnung ist ungültig")
    result: dict[StorageBackend, StorageAdapter] = {}
    for backend, adapter in adapters.items():
        if type(backend) is not StorageBackend or getattr(adapter, "backend", None) is not backend:
            raise ReconciliationIntegrityError("Storage-Adapterzuordnung ist ungültig")
        result[backend] = adapter
    return dict(sorted(result.items(), key=lambda item: item[0].value))


def _storage_finding(
    code: FindingCode,
    reference: StorageReference,
    owner: str | None,
) -> ReconciliationFinding:
    messages = {
        FindingCode.CHANGED: "Storage-Artefaktintegrität weicht ab",
        FindingCode.MISSING_LOCAL: "Storage-Artefakt fehlt lokal",
        FindingCode.ORPHAN: "Storage-Artefakt ist nicht im Manifest",
        FindingCode.RIGHTS_CHANGED: "Storage-Rechteentscheidung weicht ab",
    }
    return ReconciliationFinding(
        code=code,
        subject_kind=SubjectKind.STORAGE,
        subject_id=reference.artifact_id if owner is None else owner,
        relative_path=reference.relative_path,
        message=messages[code],
    )


def _storage_reference_owner(
    graph: ManifestGraph,
    reference: StorageReference,
) -> str | None:
    if reference.source_id is None or reference.document_id is None:
        return None
    matches: list[dict[str, object]] = []
    for document in graph.documents:
        if (
            type(document) is dict
            and document.get("superseded_by") is None
            and document.get("source_id") == reference.source_id
            and document.get("document_id") == reference.document_id
        ):
            matches.append(document)
    path_matches = []
    for document in matches:
        paths = document.get("paths")
        if type(paths) is dict and reference.relative_path in paths.values():
            path_matches.append(document)
    selected = path_matches if path_matches else matches
    if not selected:
        return None
    if len(selected) != 1:
        raise ReconciliationIntegrityError("Storage-Owner ist mehrdeutig")
    bitstream_id = selected[0].get("bitstream_id")
    if type(bitstream_id) is not str:
        raise ReconciliationIntegrityError("Storage-Owner ist ungültig")
    return source_subject_id(reference.source_id, bitstream_id)


def _current_source_projection(
    catalog: LoadedManifestCatalog,
) -> dict[tuple[str, str], dict[str, object]]:
    return _current_source_projection_from_graph(catalog.graph)


def _current_source_projection_from_graph(
    graph: ManifestGraph,
) -> dict[tuple[str, str], dict[str, object]]:
    sources = {source["bitstream_id"]: source for source in graph.sources}
    current: dict[tuple[str, str], dict[str, object]] = {}
    for document in graph.documents:
        if document["superseded_by"] is not None:
            continue
        bitstream_id = document["bitstream_id"]
        source = sources[bitstream_id]
        key = (source["source_id"], bitstream_id)
        current[key] = source
    return current


def _remote_bitstream_id(record: ArtifactRecord) -> str | None:
    if record.pdf_url is None:
        if _claims_downloadable_content(record):
            raise RemoteSnapshotError("Remote-Record mit Downloadanspruch hat keine PDF-URL")
        return None
    try:
        return bitstream_identity(record.pdf_url).bitstream_id
    except DocumentIdentityError as exc:
        raise RemoteSnapshotError(
            "Remote-Record hat keine kanonische PDF-Bitstream-Identität"
        ) from exc


def _remote_source_index(
    remote_records: tuple[ArtifactRecord, ...],
) -> dict[tuple[str, str], ArtifactRecord]:
    if type(remote_records) is not tuple:
        raise ReconciliationIntegrityError("Remote-Snapshot ist kein Tupel")
    result: dict[tuple[str, str], ArtifactRecord] = {}
    for record in remote_records:
        if type(record) is not ArtifactRecord:
            raise ReconciliationIntegrityError("Remote-Snapshot enthält keinen ArtifactRecord")
        bitstream_id = _remote_bitstream_id(record)
        if bitstream_id is None:
            continue
        key = (record.source_id, bitstream_id)
        if key in result:
            raise RemoteSnapshotError("Remote-Source/Bitstream ist doppelt")
        result[key] = record
    return result


def _claims_downloadable_content(record: ArtifactRecord) -> bool:
    return record.state in _DOWNLOADABLE_STATES or any(
        value is not None for value in (record.sha256, record.bytes, record.relative_path)
    )


def _metadata_drifts(local: dict[str, object], remote: ArtifactRecord) -> bool:
    if any(local[local_field] != getattr(remote, remote_field) for local_field, remote_field in _METADATA_FIELDS):
        return True
    if local.get("rights_evidence") != {
        "label": remote.rights.label,
        "license_url": remote.rights.uri,
        "copyright_notice": remote.rights.copyright_notice,
        "open_access": remote.rights.open_access,
    }:
        return True
    return remote.sha256 is not None and local["sha256"] != remote.sha256


def _candidate_load_is_justified(local: dict[str, object], remote: ArtifactRecord) -> bool:
    return any(
        local[local_field] != getattr(remote, remote_field)
        for local_field, remote_field in _CANDIDATE_METADATA_FIELDS
    ) or (remote.sha256 is not None and local["sha256"] != remote.sha256)


def _load_candidate_if_available(
    candidate_loader: CandidateLoader | None,
    record: ArtifactRecord,
) -> None:
    if candidate_loader is None:
        return
    try:
        candidate = candidate_loader(record)
    except (GrabberHttpError, PdfDownloadError, OSError, StorageError):
        return
    if type(candidate) is not PreparedObject:
        raise ReconciliationIntegrityError("CandidateLoader lieferte kein PreparedObject")
    try:
        candidate.__post_init__()
    except (StorageError, ValueError) as exc:
        raise ReconciliationIntegrityError("CandidateLoader lieferte ungültige Evidenz") from exc
    if (
        candidate.source_id != record.source_id
        or candidate.document_id != record.document_id
        or candidate.source_sha256 != candidate.sha256
        or not candidate.path.is_file()
    ):
        raise ReconciliationIntegrityError("CandidateLoader lieferte unpassende Evidenz")


def _source_finding(
    code: FindingCode,
    subject_id: str,
    message: str,
) -> ReconciliationFinding:
    return ReconciliationFinding(
        code=code,
        subject_kind=SubjectKind.SOURCE,
        subject_id=subject_id,
        relative_path=None,
        message=message,
    )


def _validate_clock_and_scope(as_of: datetime, from_year: int, to_year: int) -> None:
    if (
        type(as_of) is not datetime
        or as_of.tzinfo is None
        or as_of.utcoffset() != timedelta(0)
    ):
        raise ValueError("as_of muss UTC-aware sein")
    if as_of.microsecond != 0:
        raise ValueError("as_of muss auf ganze Sekunden begrenzt sein")
    if (
        type(from_year) is not int
        or type(to_year) is not int
        or not 1990 <= from_year <= to_year <= 9999
    ):
        raise ValueError("Jahresbereich ist ungültig")


def _validate_plan_contract(
    *,
    as_of: datetime,
    from_year: int,
    to_year: int,
    catalog: LoadedManifestCatalog,
    remote_records: tuple[ArtifactRecord, ...],
    adapters: Mapping[StorageBackend, StorageAdapter],
    period_root: Path,
    authority: RightsAuthority,
    policy: RightsPolicy,
    candidate_loader: CandidateLoader | None,
) -> None:
    _validate_clock_and_scope(as_of, from_year, to_year)
    if (
        type(catalog) is not LoadedManifestCatalog
        or type(catalog.graph) is not ManifestGraph
        or type(catalog.rendered) is not RenderedManifestCatalog
    ):
        raise ReconciliationIntegrityError("Manifestkatalog ist ungültig")
    _remote_source_index(remote_records)
    for record in remote_records:
        _remote_record_year(record)
    _storage_adapters(adapters)
    if not isinstance(period_root, Path):
        raise ReconciliationIntegrityError("Periodenwurzel ist ungültig")
    try:
        metadata = period_root.lstat()
    except FileNotFoundError:
        metadata = None
    except OSError as exc:
        raise ReconciliationIntegrityError("Periodenwurzel ist nicht prüfbar") from exc
    if metadata is not None and (
        stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ReconciliationIntegrityError("Periodenwurzel ist kein sicheres Verzeichnis")
    if type(authority) is not RightsAuthority or type(policy) is not RightsPolicy:
        raise ReconciliationIntegrityError("Rechteautorität oder -policy ist ungültig")
    if candidate_loader is not None and not callable(candidate_loader):
        raise ReconciliationIntegrityError("Kandidatenloader ist nicht aufrufbar")


def _remote_record_year(record: ArtifactRecord) -> int | None:
    if record.publication_date is not None:
        if type(record.publication_date) is not str:
            raise RemoteSnapshotError("Remote-Publikationsdatum ist ungültig")
        try:
            published = date.fromisoformat(record.publication_date)
        except ValueError as exc:
            raise RemoteSnapshotError("Remote-Publikationsdatum ist ungültig") from exc
        if published.isoformat() != record.publication_date:
            raise RemoteSnapshotError("Remote-Publikationsdatum ist nicht kanonisch")
        if type(record.year) is not int or record.year != published.year:
            raise RemoteSnapshotError("Remote-Publikationsjahr widerspricht dem Datum")
        return published.year
    if record.year is not None and type(record.year) is not int:
        raise RemoteSnapshotError("Remote-Publikationsjahr ist ungültig")
    return record.year


def _document_year(document: dict[str, object]) -> int:
    publication_date = document.get("publication_date")
    if publication_date is not None:
        if type(publication_date) is not str:
            raise ReconciliationIntegrityError("Dokumentdatum ist ungültig")
        try:
            published = date.fromisoformat(publication_date)
        except ValueError as exc:
            raise ReconciliationIntegrityError("Dokumentdatum ist ungültig") from exc
        if published.isoformat() != publication_date:
            raise ReconciliationIntegrityError("Dokumentdatum ist nicht kanonisch")
        return published.year
    canonical = document.get("canonical_periods")
    year = canonical.get("year") if type(canonical) is dict else document.get("year")
    if type(year) is not int:
        raise ReconciliationIntegrityError("Dokumentjahr ist ungültig")
    return year


def _scoped_reconciliation_inputs(
    catalog: LoadedManifestCatalog,
    remote_records: tuple[ArtifactRecord, ...],
    *,
    from_year: int,
    to_year: int,
) -> tuple[LoadedManifestCatalog, tuple[ArtifactRecord, ...]]:
    selected_documents: list[dict[str, object]] = []
    selected_bitstreams: set[str] = set()
    selected_document_ids: set[str] = set()
    selected_paths: set[str] = set()
    for document in catalog.graph.documents:
        if type(document) is not dict:
            raise ReconciliationIntegrityError("Dokumentmanifest ist ungültig")
        if document.get("superseded_by") is not None:
            continue
        if not from_year <= _document_year(document) <= to_year:
            continue
        bitstream_id = document.get("bitstream_id")
        document_id = document.get("document_id")
        if type(bitstream_id) is not str or type(document_id) is not str:
            raise ReconciliationIntegrityError("Dokumentidentität ist ungültig")
        selected_documents.append(document)
        selected_bitstreams.add(bitstream_id)
        selected_document_ids.add(document_id)
        paths = document.get("paths")
        if type(paths) is dict:
            selected_paths.update(path for path in paths.values() if type(path) is str)

    selected_sources = tuple(
        source
        for source in catalog.graph.sources
        if source.get("bitstream_id") in selected_bitstreams
    )
    selected_source_ids = {
        source["source_id"] for source in selected_sources if type(source.get("source_id")) is str
    }
    selected_conversions = tuple(
        conversion
        for conversion in catalog.graph.conversions
        if conversion.get("document_id") in selected_document_ids
        and conversion.get("bitstream_id") in selected_bitstreams
    )
    selected_conversion_ids = {
        conversion["conversion_id"]
        for conversion in selected_conversions
        if type(conversion.get("conversion_id")) is str
    }
    selected_storage = tuple(
        reference
        for reference in catalog.graph.storage_references
        if reference.get("relative_path") in selected_paths
        and (
            reference.get("conversion_id") is None
            or reference.get("conversion_id") in selected_conversion_ids
        )
    )
    scoped_graph = ManifestGraph(
        sources=selected_sources,
        documents=tuple(selected_documents),
        conversions=selected_conversions,
        storage_references=selected_storage,
    )
    scoped_catalog = LoadedManifestCatalog(graph=scoped_graph, rendered=catalog.rendered)
    scoped_keys = set(_current_source_projection(scoped_catalog))
    scoped_remote: list[ArtifactRecord] = []
    for record in remote_records:
        bitstream_id = _remote_bitstream_id(record)
        key = None if bitstream_id is None else (record.source_id, bitstream_id)
        year = _remote_record_year(record)
        if (
            (year is not None and from_year <= year <= to_year)
            or key in scoped_keys
            or record.source_id in selected_source_ids
        ):
            scoped_remote.append(record)
    return scoped_catalog, tuple(scoped_remote)


def build_reconciliation_result(
    *,
    as_of: datetime,
    from_year: int,
    to_year: int,
    source_manifest_sha256: str,
    findings: Iterable[ReconciliationFinding],
) -> ReconciliationResult:
    _validate_clock_and_scope(as_of, from_year, to_year)
    if type(source_manifest_sha256) is not str or _SHA256.fullmatch(source_manifest_sha256) is None:
        raise ValueError("source_manifest_sha256 muss ein kleingeschriebener SHA-256 sein")

    ordered: list[ReconciliationFinding] = []
    keys: set[tuple[str, str, str, str]] = set()
    subject_states: dict[str, set[FindingCode]] = {}
    counts = {
        "ok": 0,
        "changed": 0,
        "missing_remote": 0,
        "missing_local": 0,
        "orphan": 0,
        "rights_changed": 0,
        "unresolved": 0,
    }
    for item in findings:
        if type(item) is not ReconciliationFinding:
            raise ValueError("findings müssen ReconciliationFinding sein")
        if item.key in keys:
            raise ValueError("Finding-Key ist doppelt")
        keys.add(item.key)
        ordered.append(item)
        subject_states.setdefault(item.subject_id, set()).add(item.code)
        if item.code is FindingCode.NEW:
            counts["missing_local"] += 1
        else:
            counts[item.code.value] += 1
        if item.code is not FindingCode.OK:
            counts["unresolved"] += 1

    if any(FindingCode.OK in codes and len(codes) > 1 for codes in subject_states.values()):
        raise ValueError("ok darf nicht mit offenem Finding derselben Quelle gemischt werden")

    result_counts = ReconciliationCounts(**counts)
    conclusion = "success" if result_counts.unresolved == 0 else "blocked"
    report: dict[str, object] = json.loads(
        stable_json_dumps(
            {
                "schema_version": "1.0.0",
                "scope": {"from_year": from_year, "to_year": to_year},
                "as_of": as_of.isoformat().replace("+00:00", "Z"),
                "counts": result_counts.to_dict(),
                "conclusion": conclusion,
                "source_manifest_sha256": source_manifest_sha256,
            }
        )
    )
    validate_document("reconciliation-report", report)
    return ReconciliationResult(
        findings=tuple(sorted(ordered, key=lambda item: item.key)),
        counts=result_counts,
        conclusion=conclusion,
        source_manifest_sha256=source_manifest_sha256,
        report=report,
        successful_at=as_of if conclusion == "success" else None,
    )


def plan_reconciliation(
    *,
    as_of: datetime,
    from_year: int,
    to_year: int,
    catalog: LoadedManifestCatalog,
    remote_records: tuple[ArtifactRecord, ...],
    adapters: Mapping[StorageBackend, StorageAdapter],
    period_root: Path,
    authority: RightsAuthority,
    policy: RightsPolicy,
    candidate_loader: CandidateLoader | None = None,
) -> ReconciliationResult:
    """Compose deterministic findings from every reconciliation boundary."""

    _validate_plan_contract(
        as_of=as_of,
        from_year=from_year,
        to_year=to_year,
        catalog=catalog,
        remote_records=remote_records,
        adapters=adapters,
        period_root=period_root,
        authority=authority,
        policy=policy,
        candidate_loader=candidate_loader,
    )
    scoped_catalog, scoped_remote_records = _scoped_reconciliation_inputs(
        catalog,
        remote_records,
        from_year=from_year,
        to_year=to_year,
    )
    checked_adapters = _storage_adapters(adapters)
    component_findings = (
        compare_remote_sources(
            scoped_catalog,
            scoped_remote_records,
            candidate_loader=candidate_loader,
        ),
        reconcile_storage(
            scoped_catalog.graph,
            checked_adapters,
            complete_graph=catalog.graph,
        ),
        reconcile_rights(scoped_catalog.graph, authority=authority, policy=policy),
        reconcile_periods(scoped_catalog.graph, period_root),
    )
    findings_by_key: dict[tuple[str, str, str, str], ReconciliationFinding] = {}
    for findings in component_findings:
        for item in findings:
            if item.key in findings_by_key:
                raise ReconciliationIntegrityError("Finding-Key ist doppelt")
            findings_by_key[item.key] = item

    open_subjects = {
        item.subject_id
        for item in findings_by_key.values()
        if item.code is not FindingCode.OK
    }
    for source_id, bitstream_id in _current_source_projection(scoped_catalog):
        subject_id = source_subject_id(source_id, bitstream_id)
        if subject_id not in open_subjects:
            item = _source_finding(FindingCode.OK, subject_id, "Quelle stimmt mit allen Prüfungen überein")
            if item.key in findings_by_key:
                raise ReconciliationIntegrityError("Finding-Key ist doppelt")
            findings_by_key[item.key] = item

    source_files = [
        payload
        for name, payload in catalog.rendered.files
        if name == "Quellen/manifest.jsonl"
    ]
    if len(source_files) != 1:
        raise ReconciliationIntegrityError("Quellenmanifest fehlt oder ist doppelt")
    return build_reconciliation_result(
        as_of=as_of,
        from_year=from_year,
        to_year=to_year,
        source_manifest_sha256=hashlib.sha256(source_files[0]).hexdigest(),
        findings=tuple(sorted(findings_by_key.values(), key=lambda item: item.key)),
    )


def materialize_reconciliation(
    result: ReconciliationResult,
    *,
    temp_root: Path,
    ledger: EffectLedger,
) -> ReconciliationMaterialization:
    """Atomically materialize one immutable successful reconciliation report."""

    if type(result) is not ReconciliationResult:
        raise ReconciliationIntegrityError("Reconciliation-Ergebnis ist ungültig")
    if not isinstance(temp_root, Path):
        raise ReconciliationIntegrityError("temp_root ist ungültig")
    if type(ledger) is not EffectLedger or ledger.mode is not RunMode.MATERIALIZE:
        raise ReconciliationIntegrityError("Reconciliation benötigt MATERIALIZE-Ledger")
    if ledger.temp_root != temp_root.resolve():
        raise ReconciliationIntegrityError("Ledger-temp_root stimmt nicht überein")
    if result.conclusion == "blocked":
        if result.successful_at is not None:
            raise ReconciliationIntegrityError("Blockiertes Ergebnis darf keinen Erfolgstermin haben")
        return ReconciliationMaterialization(result, None, False)
    if result.conclusion != "success" or result.successful_at is None:
        raise ReconciliationIntegrityError("Reconciliation-Ergebnis ist nicht materialisierbar")
    if (
        result.successful_at.tzinfo is None
        or result.successful_at.utcoffset() != timedelta(0)
    ):
        raise ReconciliationIntegrityError("Erfolgszeitpunkt muss UTC-aware sein")
    payload = _reconciliation_report_payload(result)
    root = temp_root.resolve()
    timestamp = result.successful_at.strftime("%Y%m%dT%H%M%SZ")
    target = root / (
        "rki/Bulletins/Manifeste/Reconciliation/"
        f"reconciliation-{timestamp}.json"
    )
    changed = _write_immutable_report(target, root=root, payload=payload, ledger=ledger)
    return ReconciliationMaterialization(result, target, changed)


def _reconciliation_report_payload(result: ReconciliationResult) -> bytes:
    validate_document("reconciliation-report", result.report)
    try:
        scope = result.report["scope"]
        from_year = scope["from_year"]
        to_year = scope["to_year"]
    except (KeyError, TypeError) as exc:
        raise ReconciliationIntegrityError("Reconciliation-Bericht ist ungültig") from exc
    expected = build_reconciliation_result(
        as_of=result.successful_at,
        from_year=from_year,
        to_year=to_year,
        source_manifest_sha256=result.source_manifest_sha256,
        findings=result.findings,
    )
    if (
        result.counts != expected.counts
        or result.conclusion != expected.conclusion
        or result.successful_at != expected.successful_at
    ):
        raise ReconciliationIntegrityError("Reconciliation-Ergebnis ist widersprüchlich")
    if result.report != expected.report:
        raise ReconciliationIntegrityError("Reconciliation-Bericht widerspricht dem Ergebnis")
    return stable_json_dumps(expected.report).encode("utf-8") + b"\n"


def _write_immutable_report(
    target: Path,
    *,
    root: Path,
    payload: bytes,
    ledger: EffectLedger,
) -> bool:
    relative = relative_path_beneath(target, root)
    with open_root_directory(root, create=True) as root_fd:
        parent_fd = open_directory_beneath(root_fd, relative.parts[:-1], create=True)
        try:
            existing = _read_report_bytes(parent_fd, relative.name)
            if existing is not None:
                _require_identical_report(existing, payload)
                return False
            temporary_name = f".{relative.name}.{uuid.uuid4().hex}.part"
            temporary_path = target.with_name(temporary_name)
            try:
                atomic_write_bytes(temporary_path, payload, allowed_root=root)
                try:
                    os.link(
                        temporary_name,
                        relative.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    existing = _read_report_bytes(parent_fd, relative.name)
                    if existing is None:
                        raise ReconciliationIntegrityError(
                            "Reconciliation-Berichtskollision ist unklar"
                        ) from exc
                    try:
                        _require_identical_report(existing, payload)
                    except ReconciliationIntegrityError as conflict:
                        raise conflict from exc
                    return False
                try:
                    fsync_directory_fd(parent_fd)
                    ledger.record(
                        EffectKind.TEMP_FILE,
                        target.absolute().as_posix(),
                        sha256=hashlib.sha256(payload).hexdigest(),
                        size=len(payload),
                    )
                except BaseException:
                    _remove_linked_report(parent_fd, temporary_name, relative.name)
                    raise
                return True
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)


def _read_report_bytes(parent_fd: int, name: str) -> bytes | None:
    try:
        initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(initial.st_mode):
        raise ReconciliationIntegrityError("Reconciliation-Bericht ist keine reguläre Datei")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
            raise ReconciliationIntegrityError("Reconciliation-Bericht änderte sich beim Lesen")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            payload = handle.read()
        final = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns)
            != (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns)
        ):
            raise ReconciliationIntegrityError("Reconciliation-Bericht änderte sich beim Lesen")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _require_identical_report(existing: bytes, payload: bytes) -> None:
    if existing != payload:
        raise ReconciliationIntegrityError("Reconciliation-Bericht ist unveränderlich")


def _remove_linked_report(parent_fd: int, temporary_name: str, target_name: str) -> None:
    temporary = os.stat(temporary_name, dir_fd=parent_fd, follow_symlinks=False)
    current = os.stat(target_name, dir_fd=parent_fd, follow_symlinks=False)
    if (temporary.st_dev, temporary.st_ino) != (current.st_dev, current.st_ino):
        raise ReconciliationIntegrityError("Reconciliation-Berichtskollision ist unklar")
    os.unlink(target_name, dir_fd=parent_fd)
    try:
        fsync_directory_fd(parent_fd)
    except OSError:
        pass


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


class FixtureValidationError(ValueError):
    """Offline reconciliation fixture is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class _FixtureAdapter:
    backend: StorageBackend
    reference: StorageReference

    def verify(self, reference: StorageReference) -> None:
        if reference != self.reference:
            raise StorageError("Fixture-Storage-Referenz stimmt nicht überein")

    def list_references(self) -> tuple[StorageReference, ...]:
        return (self.reference,)


def _fixture_root_read_hook(_root: Path, _root_fd: int) -> None:
    """Test seam for deterministic fixture replacement races."""


def _fixture_payload(root: Path) -> dict[str, object]:
    fixture = Path(root)
    try:
        with open_root_directory(fixture) as root_fd:
            root_initial = os.fstat(root_fd)
            if set(os.listdir(root_fd)) != {"fixture.json"}:
                raise FixtureValidationError("Fixture-Verzeichnis ist nicht strikt")
            initial = os.stat("fixture.json", dir_fd=root_fd, follow_symlinks=False)
            if stat.S_ISLNK(initial.st_mode) or not stat.S_ISREG(initial.st_mode):
                raise FixtureValidationError("Fixture-Datei ist ungültig")
            _fixture_root_read_hook(fixture, root_fd)
            descriptor = os.open(
                "fixture.json",
                os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_fd,
            )
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode):
                    raise FixtureValidationError("Fixture-Datei ist ungültig")
                if (opened.st_dev, opened.st_ino) != (initial.st_dev, initial.st_ino):
                    raise FixtureValidationError("Fixture-Datei änderte sich beim Lesen")
                if opened.st_size > _FIXTURE_MAX_BYTES:
                    raise FixtureValidationError("Fixture-Datei ist zu groß")
                chunks: list[bytes] = []
                size = 0
                while True:
                    chunk = os.read(descriptor, min(8192, _FIXTURE_MAX_BYTES + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > _FIXTURE_MAX_BYTES:
                        raise FixtureValidationError("Fixture-Datei ist zu groß")
                payload = b"".join(chunks)
                opened_final = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            final = os.stat("fixture.json", dir_fd=root_fd, follow_symlinks=False)
            compared_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
            if any(getattr(value, field) != getattr(initial, field) for value in (opened_final, final) for field in compared_fields):
                raise FixtureValidationError("Fixture-Datei änderte sich beim Lesen")
            if set(os.listdir(root_fd)) != {"fixture.json"}:
                raise FixtureValidationError("Fixture-Verzeichnis änderte sich beim Lesen")
            root_final = os.fstat(root_fd)
            live_root = os.stat(fixture, follow_symlinks=False)
            if (
                not stat.S_ISDIR(live_root.st_mode)
                or stat.S_ISLNK(live_root.st_mode)
                or (root_initial.st_dev, root_initial.st_ino)
                != (root_final.st_dev, root_final.st_ino)
                or (root_initial.st_dev, root_initial.st_ino)
                != (live_root.st_dev, live_root.st_ino)
            ):
                raise FixtureValidationError("Fixture-Verzeichnis änderte sich beim Lesen")
    except (OSError, ValueError) as exc:
        raise FixtureValidationError("Fixture-Datei ist nicht lesbar") from exc
    if len(payload) > _FIXTURE_MAX_BYTES:
        raise FixtureValidationError("Fixture-Datei ist zu groß")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_fixture_object)
    except (FixtureValidationError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FixtureValidationError("Fixture-JSON ist ungültig") from exc
    if type(value) is not dict or set(value) != {
        "schema_version", "as_of", "scope", "source", "payload"
    }:
        raise FixtureValidationError("Fixture-Felder sind ungültig")
    if value["schema_version"] != "1.0.0" or value["as_of"] != _FIXTURE_AS_OF:
        raise FixtureValidationError("Fixture-Version oder Zeitpunkt ist ungültig")
    scope = value["scope"]
    source = value["source"]
    if (
        type(scope) is not dict
        or set(scope) != {"from_year", "to_year"}
        or any(type(scope[name]) is not int for name in scope)
        or scope != {"from_year": 2025, "to_year": 2025}
        or type(source) is not dict
        or set(source) != {"handle", "publication_date", "title", "pdf_url"}
        or type(value["payload"]) is not str
        or not value["payload"]
        or any(type(source[name]) is not str for name in source)
    ):
        raise FixtureValidationError("Fixture-Form ist ungültig")
    try:
        document = bitstream_identity(source["pdf_url"])
        if document.canonical_url != source["pdf_url"]:
            raise ValueError("Bitstream-URL ist nicht kanonisch")
        if source["handle"] != "176904/900000001" or source["publication_date"] != "2025-12-12":
            raise ValueError("Fixture-Quelle weicht ab")
        date.fromisoformat(source["publication_date"])
    except (DocumentIdentityError, ValueError) as exc:
        raise FixtureValidationError("Fixture-Quelle ist nicht kanonisch") from exc
    return value


def _fixture_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise FixtureValidationError("Fixture enthält doppelte Felder")
        value[key] = item
    return value


def _fixture_authorizer(
    root: Path,
    source_id: str,
    source_sha256: str,
    canonical_url: str,
) -> tuple[
    RightsStorageAuthorizer, RightsAuthority, RightsPolicy, RightsDecision
]:
    register = root / "rights.yml"
    register.write_text(
        "\n".join(
            (
                "schema_version: 2",
                "decisions:",
                f"  - source_id: {source_id}",
                f"    canonical_url: {canonical_url}",
                f"    version_or_bitstream: {bitstream_identity(canonical_url).bitstream_id}",
                f"    source_sha256: {source_sha256}",
                "    state: approved",
                "    mode: materialized",
                "    allowed_actions: [cache, extract_text, fetch, hash, index_text, ocr, publish, thumbnail]",
                "    components_state: cleared",
                "    attribution:",
                "      creators: [Synthetic Creator]",
                "      attribution_parties: [Synthetic Rights Holder]",
                "      copyright_notice: Synthetic copyright notice",
                "      license_notice: CC BY 4.0",
                "      license_url: https://creativecommons.org/licenses/by/4.0/",
                "      disclaimer_notice: Synthetic fixture only",
                f"      origin_url: https://edoc.rki.de/handle/{source_id.removeprefix('rki:')}",
                "      prior_change_history: []",
                "      current_change_notice: Unchanged synthetic fixture",
                "    basis: Synthetic fixture; no external publication rights claim",
                "    reviewed_by: Test Fixture",
                '    reviewed_at: "2026-08-04T04:00:00Z"',
                "",
            )
        ),
        encoding="utf-8",
    )
    authority = load_fixture_rights_authority(register)
    policy = load_rights_policy()
    approval_key = ApprovalKey(
        source_id=source_id,
        canonical_url=canonical_url,
        version_or_bitstream=bitstream_identity(canonical_url).bitstream_id,
        source_sha256=source_sha256,
    )
    decision = resolve_action(
        approval_key,
        action=RightsAction.CACHE,
        register=load_authority_register(authority),
        policy=policy,
    )
    return RightsStorageAuthorizer(authority, policy), authority, policy, decision


def _fixture_record(value: dict[str, object], source_sha256: str) -> ArtifactRecord:
    source = value["source"]
    assert type(source) is dict
    handle = source["handle"]
    publication_date = source["publication_date"]
    pdf_url = source["pdf_url"]
    title = source["title"]
    assert all(type(item) is str for item in (handle, publication_date, pdf_url, title))
    return ArtifactRecord(
        scope=Scope.ISSUES,
        document_id="rki-176904-900000001-v1",
        source_id="rki:176904/900000001",
        version=1,
        item_handle=handle,
        item_url=f"https://edoc.rki.de/handle/{handle}",
        title=title,
        publication_date=publication_date,
        year=2025,
        doi=None,
        rights=RightsMetadata(open_access=True),
        pdf_url=pdf_url,
        source_filename="source.pdf",
        relative_path=None,
        state=RecordState.DOWNLOADED,
        bytes=len(value["payload"].encode("utf-8")),
        sha256=source_sha256,
        etag=None,
        last_modified=None,
    )


def _reconcile_fixture_once(value: dict[str, object], *, mode: str) -> dict[str, object]:
    source_bytes = value["payload"].encode("utf-8")
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    record = _fixture_record(value, source_sha256)
    as_of = datetime.fromisoformat(_FIXTURE_AS_OF.replace("Z", "+00:00"))
    with TemporaryDirectory(prefix="desinfect-reconcile-") as temporary:
        root = Path(temporary)
        authorizer, authority, policy, decision = _fixture_authorizer(
            root, record.source_id, source_sha256, record.pdf_url
        )
        sources = build_source_manifests(
            (record,), rights_decisions={decision.approval_key: decision}
        )
        documents = (build_document_manifest(record),)
        logical_key = documents[0]["paths"]["pdf"]
        assert type(logical_key) is str
        source_path = root / logical_key
        source_path.parent.mkdir(parents=True)
        source_path.write_bytes(source_bytes)
        prepared = PreparedObject(
            artifact_id="fixture-pdf",
            logical_key=logical_key,
            path=source_path,
            temp_root=root,
            sha256=source_sha256,
            size=len(source_bytes),
            source_id=record.source_id,
            source_sha256=source_sha256,
            decision_sha256=decision.decision_sha256,
            visibility="repository_authorized",
            rights_state="approved",
            document_id=record.document_id,
        )
        reference = StorageReference(
            artifact_id=prepared.artifact_id,
            relative_path=prepared.logical_key,
            storage_backend=StorageBackend.LFS,
            storage_object_id=f"sha256:{source_sha256}",
            sha256=source_sha256,
            size=len(source_bytes),
            source_id=record.source_id,
            source_sha256=source_sha256,
            document_id=record.document_id,
            conversion_id=None,
            decision_sha256=decision.decision_sha256,
            provenance_state="current",
            visibility="repository_authorized",
            rights_state="approved",
            public_reference=None,
        )
        graph = build_manifest_graph(
            sources=sources,
            documents=documents,
            conversions=(),
            storage_references=(reference.to_dict(),),
            authorizer=authorizer,
        )
        catalog = LoadedManifestCatalog(graph=graph, rendered=render_manifest_catalog(graph))
        due_tasks = tuple(
            DueTask(
                task_id=f"{kind.value}:{period}",
                kind=kind,
                period=period,
                reason="fixture",
                due_at=_FIXTURE_AS_OF,
            )
            for kind, period in (
                (TaskKind.WEEK, "2025-W50"),
                (TaskKind.MONTH, "2025-12"),
                (TaskKind.YEAR, "2025"),
            )
        )
        aggregation = plan_period_archives(
            as_of=as_of,
            due_tasks=due_tasks,
            affected_periods=AffectedPeriods(),
            graph=graph,
            prepared_by_logical_key={logical_key: prepared},
        )
        products = materialize_period_archives(
            aggregation,
            root / "period-products",
            temp_root=root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=root),
            authorizer=authorizer,
        )
        result = plan_reconciliation(
            as_of=as_of,
            from_year=2025,
            to_year=2025,
            catalog=catalog,
            remote_records=(record,),
            adapters={StorageBackend.LFS: _FixtureAdapter(StorageBackend.LFS, reference)},
            period_root=products.root,
            authority=authority,
            policy=policy,
        )
        report_path: str | None = None
        changed = False
        if mode == "materialize":
            materialized = materialize_reconciliation(
                result,
                temp_root=root,
                ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=root),
            )
            report_path = (
                None
                if materialized.path is None
                else materialized.path.relative_to(root).as_posix()
            )
            changed = materialized.changed
        return {
            "mode": mode,
            "conclusion": result.conclusion,
            "counts": result.counts.to_dict(),
            "source_manifest_sha256": result.source_manifest_sha256,
            "findings": [
                {
                    "code": finding.code.value,
                    "subject_kind": finding.subject_kind.value,
                    "subject_id": finding.subject_id,
                    "relative_path": finding.relative_path,
                    "message": finding.message,
                }
                for finding in result.findings
            ],
            "report_path": report_path,
            "changed": changed,
        }


def _reconcile_fixture(value: dict[str, object], *, mode: str) -> dict[str, object]:
    return _reconcile_fixture_once(value, mode=mode)


def _fixture_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reconcile")
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--mode", choices=("plan", "materialize"), required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        args = _fixture_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        value = _fixture_payload(Path(args.fixture))
    except FixtureValidationError:
        print("reconcile: fixture validation failed", file=sys.stderr)
        return 1
    try:
        print(stable_json_dumps(_reconcile_fixture(value, mode=args.mode)), end="")
        return 0
    except (
        ArchiveError,
        AggregationError,
        DocumentIdentityError,
        ManifestBuildError,
        ManifestGraphError,
        OSError,
        PeriodManifestError,
        ReconciliationIntegrityError,
        RemoteSnapshotError,
        RightsPolicyError,
        SchemaContractError,
        StorageAuthorizationError,
        StorageError,
    ):
        print("reconcile: reconciliation failed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
