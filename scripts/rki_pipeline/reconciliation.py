"""Deterministic reconciliation findings and report contract."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import json
import re
from typing import Callable, Iterable, Mapping, TypeAlias
import unicodedata

from scripts.rki_grabber.models import ArtifactRecord, RecordState
from scripts.rki_pipeline.documents import DocumentIdentityError, bitstream_identity
from scripts.rki_pipeline.io_utils import normalize_posix_path, stable_json_dumps
from scripts.rki_pipeline.manifests import (
    LoadedManifestCatalog,
    ManifestGraph,
    storage_reference_from_manifest,
)
from scripts.rki_pipeline.rights import (
    RightsAuthority,
    RightsDecision,
    RightsPolicy,
    RightsPolicyError,
    RightsState,
    resolve_rights,
)
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    StorageAdapter,
    StorageAuthorizationError,
    StorageBackend,
    StorageError,
    StorageReference,
)


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_METADATA_FIELDS = (
    ("version", "version"),
    ("source_url", "item_url"),
    ("bitstream_url", "pdf_url"),
    ("etag", "etag"),
    ("last_modified", "last_modified"),
    ("publication_date", "publication_date"),
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


def source_subject_id(source_id: str, bitstream_id: str) -> str:
    return f"{source_id}#{bitstream_id}"


def compare_remote_sources(
    catalog: LoadedManifestCatalog,
    remote_records: tuple[ArtifactRecord, ...],
    *,
    candidate_loader: CandidateLoader | None = None,
) -> tuple[ReconciliationFinding, ...]:
    """Compare current manifest sources with a remote metadata snapshot."""

    local_sources = _current_source_projection(catalog)
    remote_sources: dict[tuple[str, str], ArtifactRecord] = {}
    findings: list[ReconciliationFinding] = []

    for record in remote_records:
        bitstream_id = _remote_bitstream_id(record)
        if bitstream_id is None:
            continue
        key = (record.source_id, bitstream_id)
        if key in remote_sources:
            raise ValueError("Remote-Source/Bitstream ist doppelt")
        remote_sources[key] = record

    for key, record in remote_sources.items():
        local = local_sources.get(key)
        subject_id = source_subject_id(*key)
        if local is None:
            findings.append(_source_finding(FindingCode.NEW, subject_id, "Remote-Quelle ist neu"))
            continue
        if _metadata_drifts(local, record):
            _load_candidate_if_available(candidate_loader, record, local)
            findings.append(
                _source_finding(FindingCode.CHANGED, subject_id, "Remote-Metadaten driften")
            )

    for key in local_sources.keys() - remote_sources.keys():
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
) -> tuple[ReconciliationFinding, ...]:
    """Verify persisted storage references and detect adapter inventory orphans."""

    references = _manifest_storage_references(graph)
    checked_adapters = _storage_adapters(adapters)
    findings: list[ReconciliationFinding] = []
    manifest_ids = {reference.artifact_id for reference in references}

    for reference in references:
        adapter = checked_adapters.get(reference.storage_backend)
        if adapter is None:
            findings.append(_storage_finding(FindingCode.MISSING_LOCAL, reference))
            continue
        try:
            adapter.verify(reference)
        except FileNotFoundError:
            findings.append(_storage_finding(FindingCode.MISSING_LOCAL, reference))
        except StorageAuthorizationError:
            findings.append(_storage_finding(FindingCode.RIGHTS_CHANGED, reference))
        except StorageError:
            findings.append(_storage_finding(FindingCode.CHANGED, reference))
        except Exception as exc:
            raise ReconciliationIntegrityError("Storage-Adapter-Vertrag ist ungültig") from exc

    inventory_ids: set[str] = set()
    by_artifact_id = {reference.artifact_id: reference for reference in references}
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
            if reference.artifact_id not in manifest_ids:
                findings.append(_storage_finding(FindingCode.ORPHAN, reference))

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
    decisions: dict[tuple[str, str], RightsDecision] = {}
    source_changed: dict[tuple[str, str], bool] = {}

    for source in sorted(graph.sources, key=lambda item: (item["source_id"], item["bitstream_id"])):
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
        key = (source_id, source_sha256)
        decision = decisions.get(key)
        if decision is None:
            try:
                decision = resolve_rights(
                    source_id,
                    source_sha256,
                    authority=authority,
                    policy=policy,
                )
            except RightsPolicyError as exc:
                raise ReconciliationIntegrityError("Rechteentscheidung ist nicht prüfbar") from exc
            if type(decision) is not RightsDecision:
                raise ReconciliationIntegrityError("Rechteentscheidung ist ungültig")
            decisions[key] = decision
        changed = (
            decision.decision_sha256 != persisted_hash
            or decision.state.value != persisted_state
            or decision.state is not RightsState.APPROVED
        )
        source_changed[key] = source_changed.get(key, False) or changed
        if changed:
            findings.append(
                _source_finding(
                    FindingCode.RIGHTS_CHANGED,
                    source_subject_id(source_id, bitstream_id),
                    "Aktuelle Rechteentscheidung weicht ab",
                )
            )

    for reference in references:
        if reference.source_id is None or reference.source_sha256 is None:
            raise ReconciliationIntegrityError("Storage-Rechteverknüpfung ist ungültig")
        key = (reference.source_id, reference.source_sha256)
        decision = decisions.get(key)
        if decision is None:
            raise ReconciliationIntegrityError("Storage-Source-Rechteverknüpfung fehlt")
        if (
            source_changed[key]
            or decision.decision_sha256 != reference.decision_sha256
            or decision.state.value != reference.rights_state
        ):
            findings.append(_storage_finding(FindingCode.RIGHTS_CHANGED, reference))

    return tuple(sorted(findings, key=lambda item: item.key))


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
        subject_id=reference.artifact_id,
        relative_path=reference.relative_path,
        message=messages[code],
    )


def _current_source_projection(
    catalog: LoadedManifestCatalog,
) -> dict[tuple[str, str], dict[str, object]]:
    sources = {source["bitstream_id"]: source for source in catalog.graph.sources}
    current: dict[tuple[str, str], dict[str, object]] = {}
    for document in catalog.graph.documents:
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


def _claims_downloadable_content(record: ArtifactRecord) -> bool:
    return record.state in _DOWNLOADABLE_STATES or any(
        value is not None for value in (record.sha256, record.bytes, record.relative_path)
    )


def _metadata_drifts(local: dict[str, object], remote: ArtifactRecord) -> bool:
    if any(local[local_field] != getattr(remote, remote_field) for local_field, remote_field in _METADATA_FIELDS):
        return True
    return remote.sha256 is not None and local["sha256"] != remote.sha256


def _load_candidate_if_available(
    candidate_loader: CandidateLoader | None,
    record: ArtifactRecord,
    local: dict[str, object],
) -> None:
    if candidate_loader is None:
        return
    try:
        candidate = candidate_loader(record)
        candidate.__post_init__()
        if (
            candidate.source_id != record.source_id
            or candidate.document_id != record.document_id
            or candidate.source_sha256 != candidate.sha256
            or not candidate.path.is_file()
            or candidate.sha256 != local["sha256"]
        ):
            return
    except Exception:
        return


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


def build_reconciliation_result(
    *,
    as_of: datetime,
    from_year: int,
    to_year: int,
    source_manifest_sha256: str,
    findings: Iterable[ReconciliationFinding],
) -> ReconciliationResult:
    if (
        type(as_of) is not datetime
        or as_of.tzinfo is None
        or as_of.utcoffset() != timedelta(0)
    ):
        raise ValueError("as_of muss UTC-aware sein")
    if (
        type(from_year) is not int
        or type(to_year) is not int
        or not 1990 <= from_year <= to_year <= 9999
    ):
        raise ValueError("Jahresbereich ist ungültig")
    if type(source_manifest_sha256) is not str or _SHA256.fullmatch(source_manifest_sha256) is None:
        raise ValueError("source_manifest_sha256 muss ein kleingeschriebener SHA-256 sein")

    ordered: list[ReconciliationFinding] = []
    keys: set[tuple[str, str, str, str]] = set()
    source_states: dict[str, set[FindingCode]] = {}
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
        if item.subject_kind is SubjectKind.SOURCE:
            source_states.setdefault(item.subject_id.partition("#")[0], set()).add(item.code)
        if item.code is FindingCode.NEW:
            counts["missing_local"] += 1
        else:
            counts[item.code.value] += 1
        if item.code is not FindingCode.OK:
            counts["unresolved"] += 1

    if any(FindingCode.OK in codes and len(codes) > 1 for codes in source_states.values()):
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


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)
