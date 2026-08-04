#!/usr/bin/env python3
"""Strict period selection for deterministic archive aggregation."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import json
import os
import posixpath
import re
from pathlib import Path, PurePosixPath
import stat
import sys
from tempfile import TemporaryDirectory
from typing import Any, Iterable, Mapping
from urllib.parse import quote
from zoneinfo import ZoneInfo

from scripts.rki_grabber.models import AffectedPeriods
from scripts.rki_pipeline.archive import (
    ArchiveBuild,
    ArchiveEntry,
    ArchiveError,
    ArchiveSpec,
    archive_input_fingerprint,
    materialize_archive,
    validate_archive,
    validate_archive_bundle_fd,
)
from scripts.rki_pipeline.due_tasks import DueTask, DueTaskError, TaskKind, parse_utc
from scripts.rki_pipeline.io_utils import (
    GENERATED_ROOT_SENTINEL,
    UnsafePathError,
    assert_generated_root_fd,
    atomic_write_bytes,
    entry_exists,
    normalize_posix_path,
    open_directory_beneath,
    open_root_directory,
    relative_path_beneath,
    remove_tree_at,
    stable_json_dumps,
)
from scripts.rki_pipeline.manifests import ManifestGraph
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document
from scripts.rki_pipeline.staging import StagingError, StagingState, staged_directory
from scripts.rki_pipeline.storage.base import (
    PreparedObject,
    RightsStorageAuthorizer,
    StorageAuthorizationError,
    authorize_storage_operation,
)
from scripts.rki_pipeline.rights import load_rights_authority, load_rights_policy, resolve_rights

_BERLIN = ZoneInfo("Europe/Berlin")
_WEEK = re.compile(r"^(?P<year>[0-9]{4})-W(?P<week>0[1-9]|[1-4][0-9]|5[0-3])$")
_MONTH = re.compile(r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])$")
_YEAR = re.compile(r"^[0-9]{4}$")
_WEEK_MANIFEST_NAME = re.compile(r"^(?P<period>[0-9]{4}-W(?:0[1-9]|[1-4][0-9]|5[0-3]))\.json$")
_KIND_ORDER = {TaskKind.WEEK: 0, TaskKind.MONTH: 1, TaskKind.YEAR: 2}
_MAX_PERIOD_MANIFEST_BYTES = 4 * 1024 * 1024


class AggregationError(ValueError):
    """Base aggregation contract failure."""


class PeriodSelectionError(AggregationError):
    """A due or affected period is malformed, future, or not closed."""


class PeriodManifestError(AggregationError):
    """Period-manifest bytes or archive references violate the contract."""


@dataclass(frozen=True, slots=True)
class PeriodRef:
    """One immutable, closed calendar period in Berlin time."""

    kind: TaskKind
    value: str
    start: date
    end: date
    source_date_epoch: int


@dataclass(frozen=True, slots=True)
class PeriodDocument:
    """One current document version selected for a closed period."""

    document_id: str
    bitstream_id: str
    version: int
    source_id: str
    publication_date: str
    title: str
    handle: str
    doi: str | None
    conversion_state: str
    pdf: PreparedObject | None
    markdown: PreparedObject | None


@dataclass(frozen=True, slots=True)
class PlannedArchive:
    """One P07.1 bundle below the period archive layout."""

    relative_bundle: str
    spec: ArchiveSpec


@dataclass(frozen=True, slots=True)
class PeriodPlan:
    """All selected documents and nonempty products for one period."""

    period: PeriodRef
    documents: tuple[PeriodDocument, ...]
    archives: tuple[PlannedArchive, ...]
    index_path: str | None
    manifest_path: str


@dataclass(frozen=True, slots=True)
class AggregationPlan:
    """Deterministic aggregate planning result."""

    periods: tuple[PeriodPlan, ...]
    input_fingerprint: str


@dataclass(frozen=True, slots=True)
class WeeklyArchiveReference:
    """One validated current-plan or previously published weekly archive."""

    period: PeriodRef
    archive_id: str
    kind: str
    relative_bundle: str
    input_fingerprint: str
    output_sha256: str | None
    size: int | None


@dataclass(frozen=True, slots=True)
class MonthIndexRendererInput:
    """Immutable weekly references consumed by monthly index rendering."""

    weekly_archives: tuple[WeeklyArchiveReference, ...]
    input_fingerprint: str


@dataclass(frozen=True, slots=True)
class MaterializedPeriodArchive:
    """One archive identity translated from staging to final output."""

    archive_id: str
    relative_bundle: str
    build: ArchiveBuild


@dataclass(frozen=True, slots=True)
class PeriodArchiveMaterialization:
    """Immutable result of one atomic aggregate product publication."""

    root: Path
    archives: tuple[MaterializedPeriodArchive, ...]
    index_paths: tuple[Path, ...]
    manifest_paths: tuple[Path, ...]
    input_fingerprint: str
    changed: bool


def _period_dates(kind: TaskKind, value: str) -> tuple[date, date]:
    if type(kind) is not TaskKind or kind is TaskKind.RECONCILIATION:
        raise PeriodSelectionError("Period kind ist ungültig")
    if type(value) is not str:
        raise PeriodSelectionError("Period muss eine Zeichenkette sein")
    if kind is TaskKind.WEEK:
        match = _WEEK.fullmatch(value)
        if match is None:
            raise PeriodSelectionError("ISO-Woche ist ungültig")
        try:
            start = date.fromisocalendar(int(match["year"]), int(match["week"]), 1)
        except ValueError as exc:
            raise PeriodSelectionError("ISO-Woche existiert nicht") from exc
        return start, date.fromordinal(start.toordinal() + 6)
    if kind is TaskKind.MONTH:
        match = _MONTH.fullmatch(value)
        if match is None:
            raise PeriodSelectionError("Monat ist ungültig")
        year, month = int(match["year"]), int(match["month"])
        try:
            start = date(year, month, 1)
            next_start = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
        except ValueError as exc:
            raise PeriodSelectionError("Monat existiert nicht") from exc
        return start, date.fromordinal(next_start.toordinal() - 1)
    if _YEAR.fullmatch(value) is None:
        raise PeriodSelectionError("Jahr ist ungültig")
    try:
        year = int(value)
        return date(year, 1, 1), date(year, 12, 31)
    except ValueError as exc:
        raise PeriodSelectionError("Jahr existiert nicht") from exc


def period_ref(kind: TaskKind, value: str) -> PeriodRef:
    """Parse one exact week/month/year and derive its Berlin close instant."""

    start, end = _period_dates(kind, value)
    close = datetime.combine(date.fromordinal(end.toordinal() + 1), time.min, _BERLIN)
    source_date_epoch = int(close.timestamp())
    if source_date_epoch < 0:
        raise PeriodSelectionError("Periode liegt vor RKI-Korpus und unterstütztem Unix-Epoch")
    return PeriodRef(kind, value, start, end, source_date_epoch)


def _affected_values(affected_periods: AffectedPeriods) -> Iterable[tuple[TaskKind, str]]:
    if type(affected_periods) is not AffectedPeriods:
        raise PeriodSelectionError("affected_periods muss ein AffectedPeriods-Wert sein")
    groups = (
        (TaskKind.WEEK, affected_periods.weeks),
        (TaskKind.MONTH, affected_periods.months),
        (TaskKind.YEAR, affected_periods.years),
    )
    for kind, values in groups:
        if type(values) is not set:
            raise PeriodSelectionError("AffectedPeriods-Feld muss eine Menge sein")
        for value in values:
            if kind is TaskKind.YEAR:
                if type(value) is not int:
                    raise PeriodSelectionError("Betroffenes Jahr muss eine Ganzzahl sein")
                yield kind, f"{value:04d}"
            else:
                if type(value) is not str:
                    raise PeriodSelectionError("Betroffene Periode muss eine Zeichenkette sein")
                yield kind, value


def select_periods(
    as_of: datetime,
    due_tasks: Iterable[DueTask],
    affected_periods: AffectedPeriods,
) -> tuple[PeriodRef, ...]:
    """Return closed due/affected periods in stable chronological kind order."""

    if type(as_of) is not datetime or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise PeriodSelectionError("as_of muss ein bewusster datetime-Wert sein")
    pairs: set[tuple[TaskKind, str]] = set(_affected_values(affected_periods))
    try:
        iterator = iter(due_tasks)
    except TypeError as exc:
        raise PeriodSelectionError("due_tasks muss iterierbar sein") from exc
    for task in iterator:
        if type(task) is not DueTask:
            raise PeriodSelectionError("due_tasks muss DueTask-Werte enthalten")
        if task.kind is TaskKind.RECONCILIATION:
            raise PeriodSelectionError("Reconciliation ist keine Archivperiode")
        pairs.add((task.kind, task.period))
    selected: list[PeriodRef] = []
    for kind, value in pairs:
        period = period_ref(kind, value)
        if as_of.timestamp() < period.source_date_epoch:
            raise PeriodSelectionError(f"Periode ist noch nicht abgeschlossen: {kind.value}:{value}")
        selected.append(period)
    return tuple(sorted(selected, key=lambda item: (_KIND_ORDER[item.kind], item.start)))


_CONVERSION_STATES = frozenset(
    {"converted", "skipped_unchanged", "needs_review", "failed", "not_materialized"}
)
_MATERIALIZED_CONVERSION_STATES = frozenset(
    {"converted", "skipped_unchanged", "needs_review"}
)


def _mapping_value(record: Mapping[str, object], field: str, *, label: str) -> object:
    if field not in record:
        raise AggregationError(f"{label} fehlt: {field}")
    return record[field]


def _string(record: Mapping[str, object], field: str, *, label: str) -> str:
    value = _mapping_value(record, field, label=label)
    if type(value) is not str:
        raise AggregationError(f"{label} ist ungültig: {field}")
    return value


def _nullable_string(record: Mapping[str, object], field: str, *, label: str) -> str | None:
    value = _mapping_value(record, field, label=label)
    if value is not None and type(value) is not str:
        raise AggregationError(f"{label} ist ungültig: {field}")
    return value


def _index_exact(
    values: tuple[dict[str, object], ...],
    *,
    field: str,
    label: str,
) -> dict[str, dict[str, object]]:
    index: dict[str, dict[str, object]] = {}
    for value in values:
        if type(value) is not dict:
            raise AggregationError(f"{label} ist kein exaktes Manifestobjekt")
        key = _string(value, field, label=label)
        if key in index:
            raise AggregationError(f"{label}-Identität ist mehrdeutig: {key}")
        index[key] = value
    return index


def _prepared_for(
    reference: Mapping[str, object],
    prepared_by_logical_key: Mapping[str, PreparedObject],
    *,
    source: Mapping[str, object],
    document: Mapping[str, object],
    conversion_id: str | None,
) -> PreparedObject:
    """Resolve one storage record to precisely identical authorized bytes."""

    logical_key = _string(reference, "relative_path", label="Storage")
    prepared = prepared_by_logical_key.get(logical_key)
    if type(prepared) is not PreparedObject:
        raise AggregationError(f"PreparedObject fehlt oder ist ungültig: {logical_key}")
    checks = (
        ("artifact_id", prepared.artifact_id, "Storage-Artefakt"),
        ("sha256", prepared.sha256, "SHA-256"),
        ("bytes", prepared.size, "Größe"),
        ("source_id", prepared.source_id, "Source"),
        ("source_sha256", prepared.source_sha256, "Source-SHA-256"),
        ("document_id", prepared.document_id, "Dokument"),
        ("conversion_id", prepared.conversion_id, "Conversion"),
        ("decision_sha256", prepared.decision_sha256, "Rechteentscheidung"),
        ("visibility", prepared.visibility, "Sichtbarkeit"),
        ("rights_state", prepared.rights_state, "Rechtestatus"),
    )
    for field, actual, label in checks:
        if _mapping_value(reference, field, label="Storage") != actual:
            raise AggregationError(f"{label} stimmt nicht mit PreparedObject überein")
    if prepared.logical_key != logical_key:
        raise AggregationError("PreparedObject logical_key stimmt nicht mit Storage überein")
    source_checks = (
        ("source_id", "source_id", "Source"),
        ("source_sha256", "sha256", "Source-SHA-256"),
        ("decision_sha256", "decision_sha256", "Rechteentscheidung"),
    )
    for field, source_field, label in source_checks:
        if _mapping_value(reference, field, label="Storage") != _mapping_value(
            source, source_field, label="Source"
        ):
            raise AggregationError(f"Storage-{label} stimmt nicht mit Source überein")
    rights = _mapping_value(source, "rights", label="Source")
    if type(rights) is not dict or _mapping_value(
        reference, "rights_state", label="Storage"
    ) != _mapping_value(rights, "state", label="Source-Rechte"):
        raise AggregationError("Storage-Rechtestatus stimmt nicht mit Source überein")
    if _mapping_value(reference, "document_id", label="Storage") != _mapping_value(
        document, "document_id", label="Dokument"
    ):
        raise AggregationError("Storage-Dokument stimmt nicht mit Dokument überein")
    if _mapping_value(reference, "conversion_id", label="Storage") != conversion_id:
        raise AggregationError("Storage-Conversion stimmt nicht mit Conversion überein")
    return prepared


def _document_source(
    document: Mapping[str, object], sources: Mapping[str, dict[str, object]]
) -> dict[str, object]:
    bitstream_id = _string(document, "bitstream_id", label="Dokument")
    source = sources.get(bitstream_id)
    if source is None:
        raise AggregationError(f"Source für Dokument fehlt: {bitstream_id}")
    if _mapping_value(document, "source_id", label="Dokument") != _mapping_value(
        source, "source_id", label="Source"
    ):
        raise AggregationError("Dokument-Source stimmt nicht exakt überein")
    if _mapping_value(document, "publication_date", label="Dokument") != _mapping_value(
        source, "publication_date", label="Source"
    ) or _mapping_value(document, "version", label="Dokument") != _mapping_value(
        source, "version", label="Source"
    ):
        raise AggregationError("Dokument-Identität stimmt nicht mit Source überein")
    return source


def _period_documents(
    period: PeriodRef,
    *,
    graph: ManifestGraph,
    prepared_by_logical_key: Mapping[str, PreparedObject],
) -> tuple[PeriodDocument, ...]:
    sources = _index_exact(graph.sources, field="bitstream_id", label="Source")
    storage_by_path = _index_exact(
        graph.storage_references, field="relative_path", label="Storage"
    )
    conversions = _index_exact(graph.conversions, field="conversion_id", label="Conversion")
    by_owner: dict[tuple[str, str], list[dict[str, object]]] = {}
    for conversion in conversions.values():
        owner = (
            _string(conversion, "document_id", label="Conversion"),
            _string(conversion, "bitstream_id", label="Conversion"),
        )
        by_owner.setdefault(owner, []).append(conversion)
    selected: list[PeriodDocument] = []
    for document in graph.documents:
        if type(document) is not dict:
            raise AggregationError("Dokument ist kein exaktes Manifestobjekt")
        if _mapping_value(document, "superseded_by", label="Dokument") is not None:
            continue
        canonical_periods = _mapping_value(document, "canonical_periods", label="Dokument")
        if type(canonical_periods) is not dict or canonical_periods.get(period.kind.value) != (
            int(period.value) if period.kind is TaskKind.YEAR else period.value
        ):
            continue
        source = _document_source(document, sources)
        document_id = _string(document, "document_id", label="Dokument")
        bitstream_id = _string(document, "bitstream_id", label="Dokument")
        paths = _mapping_value(document, "paths", label="Dokument")
        if type(paths) is not dict:
            raise AggregationError("Dokumentpfade sind ungültig")
        pdf_path = _string(paths, "pdf", label="Dokumentpfade")
        markdown_path = _nullable_string(paths, "markdown", label="Dokumentpfade")
        all_candidates = by_owner.get((document_id, bitstream_id), [])
        persisted_candidates = [
            candidate
            for candidate in all_candidates
            if _mapping_value(candidate, "storage_reference", label="Conversion") is not None
        ]
        if len(persisted_candidates) > 1 or (
            not persisted_candidates and len(all_candidates) > 1
        ):
            raise AggregationError(f"Conversion für Dokument ist mehrdeutig: {document_id}")
        conversion = (
            persisted_candidates[0]
            if persisted_candidates
            else all_candidates[0]
            if all_candidates
            else None
        )
        conversion_id = None
        state = "not_materialized"
        if conversion is not None:
            conversion_id = _string(conversion, "conversion_id", label="Conversion")
            state = _string(conversion, "state", label="Conversion")
            if state not in _CONVERSION_STATES:
                raise AggregationError(f"Konvertierungsstatus ist unbekannt: {state}")
            if _mapping_value(conversion, "source_sha256", label="Conversion") != _mapping_value(
                source, "sha256", label="Source"
            ):
                raise AggregationError("Conversion-Source-SHA stimmt nicht mit Source überein")
        pdf_reference = storage_by_path.get(pdf_path)
        if pdf_reference is None:
            raise AggregationError(f"Storage für PDF fehlt: {pdf_path}")
        pdf = _prepared_for(
            pdf_reference,
            prepared_by_logical_key,
            source=source,
            document=document,
            conversion_id=None,
        )
        markdown: PreparedObject | None = None
        if state in _MATERIALIZED_CONVERSION_STATES:
            assert conversion is not None and conversion_id is not None
            if markdown_path is None:
                raise AggregationError("Dokument-Markdown fehlt trotz materialisierter Conversion")
            if _mapping_value(conversion, "output_sha256", label="Conversion") is None:
                raise AggregationError("Conversion-Ausgabe-SHA fehlt")
            markdown_reference = storage_by_path.get(markdown_path)
            if markdown_reference is None:
                raise AggregationError(f"Storage für Markdown fehlt: {markdown_path}")
            if _mapping_value(conversion, "storage_reference", label="Conversion") != _mapping_value(
                markdown_reference, "artifact_id", label="Storage"
            ):
                raise AggregationError("Conversion-Storage-Referenz stimmt nicht exakt überein")
            if _mapping_value(markdown_reference, "sha256", label="Storage") != _mapping_value(
                conversion, "output_sha256", label="Conversion"
            ):
                raise AggregationError("Storage-SHA stimmt nicht mit Conversion überein")
            markdown = _prepared_for(
                markdown_reference,
                prepared_by_logical_key,
                source=source,
                document=document,
                conversion_id=conversion_id,
            )
        elif markdown_path is not None:
            raise AggregationError("Nicht materialisierte Conversion besitzt Markdown-Storage")
        version = _mapping_value(document, "version", label="Dokument")
        if type(version) is not int:
            raise AggregationError("Dokumentversion ist ungültig")
        selected.append(
            PeriodDocument(
                document_id=document_id,
                bitstream_id=bitstream_id,
                version=version,
                source_id=_string(document, "source_id", label="Dokument"),
                publication_date=_string(document, "publication_date", label="Dokument"),
                title=_string(source, "title", label="Source"),
                handle=_string(source, "handle", label="Source"),
                doi=_nullable_string(source, "doi", label="Source"),
                conversion_state=state,
                pdf=pdf,
                markdown=markdown,
            )
        )
    return tuple(
        sorted(
            selected,
            key=lambda item: (item.publication_date, item.document_id, item.bitstream_id),
        )
    )


def _bundle_path(period: PeriodRef, format_name: str) -> str:
    suffix = "Markdown" if format_name == "markdown" else "PDF"
    if period.kind is TaskKind.WEEK:
        label = f"{period.start.isoformat()}_bis_{period.end.isoformat()}"
        return (
            f"rki/Bulletins/Monate/{period.start.year:04d}/{period.start.month:02d}/"
            f"ZIP/Wochen/RKI-Einzelartikel-{label}-{suffix}"
        )
    root = "Monate" if period.kind is TaskKind.MONTH else "Jahre"
    directory = (
        f"rki/Bulletins/{root}/{period.start.year:04d}/{period.start.month:02d}/ZIP"
        if period.kind is TaskKind.MONTH
        else f"rki/Bulletins/{root}/{period.start.year:04d}/ZIP"
    )
    return f"{directory}/RKI-Einzelartikel-{period.value}-{suffix}"


def _planned_archives(period: PeriodRef, documents: tuple[PeriodDocument, ...]) -> tuple[PlannedArchive, ...]:
    archives: list[PlannedArchive] = []
    for format_name in ("markdown", "pdf"):
        payloads = tuple(
            document.markdown if format_name == "markdown" else document.pdf
            for document in documents
        )
        entries_payloads = tuple(payload for payload in payloads if payload is not None)
        if not entries_payloads:
            continue
        try:
            basenames = tuple(PurePosixPath(payload.logical_key).name for payload in entries_payloads)
            if len(set(basename.casefold() for basename in basenames)) != len(basenames):
                raise ValueError("Portable Kollision")
            entries = tuple(
                ArchiveEntry(path=f"{format_name.title()}/{basename}", prepared=payload)
                for basename, payload in sorted(
                    zip(basenames, entries_payloads, strict=True), key=lambda item: item[0]
                )
            )
            visibility = entries_payloads[0].visibility
            if any(payload.visibility != visibility for payload in entries_payloads):
                raise AggregationError("Gemischte Sichtbarkeit pro Periode und Format")
            spec = ArchiveSpec(
                archive_id=f"rki-{period.kind.value}-{period.value.lower()}-{format_name}",
                period=period.value,
                kind=f"{period.kind.value}-{format_name}",
                visibility=visibility,
                source_date_epoch=period.source_date_epoch,
                entries=entries,
            )
        except AggregationError:
            raise
        except ValueError as exc:
            raise AggregationError(f"Archiv-Kollision oder ungültiger Eintrag: {exc}") from exc
        archives.append(PlannedArchive(relative_bundle=_bundle_path(period, format_name), spec=spec))
    return tuple(archives)


def _plan_fingerprint(periods: tuple[PeriodPlan, ...]) -> str:
    payload: list[dict[str, Any]] = []
    for item in periods:
        payload.append(
            {
                "period": {
                    "kind": item.period.kind.value,
                    "value": item.period.value,
                    "start": item.period.start.isoformat(),
                    "end": item.period.end.isoformat(),
                    "source_date_epoch": item.period.source_date_epoch,
                },
                "documents": [
                    {
                        "document_id": document.document_id,
                        "bitstream_id": document.bitstream_id,
                        "version": document.version,
                        "source_id": document.source_id,
                        "publication_date": document.publication_date,
                        "title": document.title,
                        "handle": document.handle,
                        "doi": document.doi,
                        "conversion_state": document.conversion_state,
                        "pdf": None if document.pdf is None else document.pdf.artifact_id,
                        "markdown": None
                        if document.markdown is None
                        else document.markdown.artifact_id,
                    }
                    for document in item.documents
                ],
                "archives": [
                    {
                        "relative_bundle": archive.relative_bundle,
                        "archive_id": archive.spec.archive_id,
                        "kind": archive.spec.kind,
                        "visibility": archive.spec.visibility,
                        "entries": [
                            {
                                "path": entry.path,
                                "artifact_id": entry.prepared.artifact_id,
                                "logical_key": entry.prepared.logical_key,
                                "sha256": entry.prepared.sha256,
                                "bytes": entry.prepared.size,
                                "source_id": entry.prepared.source_id,
                                "source_sha256": entry.prepared.source_sha256,
                                "decision_sha256": entry.prepared.decision_sha256,
                                "rights_state": entry.prepared.rights_state,
                                "visibility": entry.prepared.visibility,
                                "document_id": entry.prepared.document_id,
                                "conversion_id": entry.prepared.conversion_id,
                            }
                            for entry in archive.spec.entries
                        ],
                    }
                    for archive in item.archives
                ],
                "index_path": item.index_path,
                "manifest_path": item.manifest_path,
            }
        )
    return hashlib.sha256(stable_json_dumps(payload).encode("utf-8")).hexdigest()


def plan_period_archives(
    *,
    as_of: datetime,
    due_tasks: Iterable[DueTask],
    affected_periods: AffectedPeriods,
    graph: ManifestGraph,
    prepared_by_logical_key: Mapping[str, PreparedObject],
) -> AggregationPlan:
    """Plan deterministic nonempty P07.1 products for every closed selected period."""

    if type(graph) is not ManifestGraph:
        raise AggregationError("graph muss ein exakter ManifestGraph sein")
    if not isinstance(prepared_by_logical_key, Mapping):
        raise AggregationError("prepared_by_logical_key muss ein Mapping sein")
    storage_by_path = _index_exact(
        graph.storage_references, field="relative_path", label="Storage"
    )
    expected_keys = set(storage_by_path)
    actual_keys = set(prepared_by_logical_key)
    if expected_keys != actual_keys:
        raise AggregationError("PreparedObject-Mapping enthält fehlende oder zusätzliche Schlüssel")
    for logical_key, prepared in prepared_by_logical_key.items():
        if type(logical_key) is not str or type(prepared) is not PreparedObject:
            raise AggregationError("prepared_by_logical_key enthält ungültige PreparedObject-Werte")
        if logical_key != prepared.logical_key:
            raise AggregationError("PreparedObject-Schlüssel stimmt nicht mit logical_key überein")
    plans: list[PeriodPlan] = []
    for period in select_periods(as_of, due_tasks, affected_periods):
        documents = _period_documents(
            period,
            graph=graph,
            prepared_by_logical_key=prepared_by_logical_key,
        )
        plans.append(
            PeriodPlan(
                period=period,
                documents=documents,
                archives=_planned_archives(period, documents),
                index_path=(
                    f"rki/Bulletins/Monate/{period.start.year:04d}/{period.start.month:02d}/"
                    "Markdown/index.md"
                    if period.kind is TaskKind.MONTH
                    else None
                ),
                manifest_path=(
                    f"rki/Bulletins/Manifeste/Archive/{period.kind.value}/{period.value}.json"
                ),
            )
        )
    period_plans = tuple(plans)
    return AggregationPlan(periods=period_plans, input_fingerprint=_plan_fingerprint(period_plans))


def _canonical_path(value: str, *, label: str) -> str:
    if type(value) is not str:
        raise AggregationError(f"{label} muss ein kanonischer relativer Pfad sein")
    try:
        normalized = normalize_posix_path(value)
    except ValueError as exc:
        raise AggregationError(f"{label} ist kein kanonischer relativer Pfad") from exc
    if normalized != value:
        raise AggregationError(f"{label} ist nicht kanonisch")
    return value


def _relative_link(source_path: str, target_path: str) -> str:
    source = _canonical_path(source_path, label="Indexpfad")
    target = _canonical_path(target_path, label="Linkziel")
    return quote(posixpath.relpath(target, PurePosixPath(source).parent.as_posix()), safe="/")


def _table_cell(value: str) -> str:
    """Escape untrusted metadata for one stable Markdown table cell."""

    if type(value) is not str:
        raise AggregationError("Tabellenwert muss eine Zeichenkette sein")
    return (
        value.replace("&", "&amp;")
        .replace("\\", "&#92;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "&#124;")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _weekly_reference_fingerprint(
    references: tuple[WeeklyArchiveReference, ...],
) -> str:
    return hashlib.sha256(
        stable_json_dumps(
            [
                {
                    "period": reference.period.value,
                    "archive_id": reference.archive_id,
                    "kind": reference.kind,
                    "relative_bundle": reference.relative_bundle,
                    "input_fingerprint": reference.input_fingerprint,
                    "output_sha256": reference.output_sha256,
                    "bytes": reference.size,
                }
                for reference in references
            ]
        ).encode("utf-8")
    ).hexdigest()


def _weekly_reference_key(reference: WeeklyArchiveReference) -> tuple[date, int]:
    return (reference.period.start, 0 if reference.kind == "week-pdf" else 1)


def _month_index_renderer_input(
    aggregation_plan: AggregationPlan,
    historical: tuple[WeeklyArchiveReference, ...] = (),
) -> MonthIndexRendererInput:
    references = list(historical)
    for candidate in aggregation_plan.periods:
        if candidate.period.kind is not TaskKind.WEEK:
            continue
        for archive in candidate.archives:
            references.append(
                WeeklyArchiveReference(
                    period=candidate.period,
                    archive_id=archive.spec.archive_id,
                    kind=archive.spec.kind,
                    relative_bundle=archive.relative_bundle,
                    input_fingerprint=archive_input_fingerprint(archive.spec),
                    output_sha256=None,
                    size=None,
                )
            )
    ordered = tuple(sorted(references, key=_weekly_reference_key))
    seen: set[tuple[str, str]] = set()
    for reference in ordered:
        if type(reference) is not WeeklyArchiveReference:
            raise AggregationError("Wochenreferenz ist nicht unveränderlich")
        format_name = reference.kind.removeprefix("week-")
        identity = (reference.period.value, format_name)
        if (
            reference.period.kind is not TaskKind.WEEK
            or format_name not in {"pdf", "markdown"}
            or reference.archive_id
            != f"rki-week-{reference.period.value.lower()}-{format_name}"
            or reference.relative_bundle != _bundle_path(reference.period, format_name)
            or identity in seen
        ):
            raise AggregationError("Wochenreferenz ist nicht kanonisch oder eindeutig")
        seen.add(identity)
    return MonthIndexRendererInput(
        weekly_archives=ordered,
        input_fingerprint=_weekly_reference_fingerprint(ordered),
    )


def _week_archive_links(
    period: PeriodRef, index_path: str, renderer_input: MonthIndexRendererInput
) -> str:
    if type(renderer_input) is not MonthIndexRendererInput or (
        renderer_input.input_fingerprint
        != _weekly_reference_fingerprint(renderer_input.weekly_archives)
    ):
        raise AggregationError("Monatsindex-Renderer-Input ist nicht kanonisch")
    planned: list[tuple[date, int, str, str]] = []
    for reference in renderer_input.weekly_archives:
        if reference.period.start > period.end or reference.period.end < period.start:
            continue
        format_name = reference.kind.removeprefix("week-")
        planned.append(
            (
                reference.period.start,
                0 if format_name == "pdf" else 1,
                "PDF" if format_name == "pdf" else "Markdown",
                reference.relative_bundle,
            )
        )
    return " ".join(
        f"[{label}]({_relative_link(index_path, f'{bundle}/archive.zip')})"
        for _start, _format_order, label, bundle in sorted(planned)
    )


def render_month_index(
    period_plan: PeriodPlan,
    aggregation_plan: AggregationPlan,
    *,
    renderer_input: MonthIndexRendererInput | None = None,
) -> bytes:
    """Render one deterministic Markdown index for a monthly period."""

    if type(period_plan) is not PeriodPlan:
        raise AggregationError("period_plan muss ein exakter PeriodPlan sein")
    if type(aggregation_plan) is not AggregationPlan:
        raise AggregationError("aggregation_plan muss ein exakter AggregationPlan sein")
    if aggregation_plan.input_fingerprint != _plan_fingerprint(aggregation_plan.periods):
        raise AggregationError("AggregationPlan-Fingerprint ist nicht kanonisch")
    if not any(plan is period_plan for plan in aggregation_plan.periods):
        raise AggregationError("period_plan gehört nicht zum AggregationPlan")
    if period_plan.period.kind is not TaskKind.MONTH or period_plan.index_path is None:
        raise AggregationError("Monatsindex benötigt eine Monatsperiode mit Indexpfad")
    index_path = _canonical_path(period_plan.index_path, label="Indexpfad")
    documents = tuple(
        sorted(
            period_plan.documents,
            key=lambda item: (item.publication_date, item.document_id, item.bitstream_id),
        )
    )
    lines = [
        f"# RKI-Einzelartikel {period_plan.period.value}",
        "",
        f"Artikel: {len(documents)}",
        "",
        "| Datum | Titel | RKI-Handle | Bitstream-ID | DOI | PDF | Markdown | Konvertierung | PDF SHA-256 | Markdown SHA-256 | Wochenarchive |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    frozen_input = (
        _month_index_renderer_input(aggregation_plan)
        if renderer_input is None
        else renderer_input
    )
    weekly_links = _week_archive_links(period_plan.period, index_path, frozen_input)
    for document in documents:
        if type(document) is not PeriodDocument:
            raise AggregationError("Monatsindex enthält kein exaktes PeriodDocument")
        pdf_link = "—"
        if document.pdf is not None:
            pdf_link = f"[PDF]({_relative_link(index_path, document.pdf.logical_key)})"
        markdown_link = "—"
        if document.markdown is not None:
            markdown_link = f"[Markdown]({_relative_link(index_path, document.markdown.logical_key)})"
        lines.append(
            "| "
            + " | ".join(
                (
                    _table_cell(document.publication_date),
                    _table_cell(document.title),
                    _table_cell(document.handle),
                    _table_cell(document.bitstream_id),
                    _table_cell(document.doi) if document.doi is not None else "—",
                    pdf_link,
                    markdown_link,
                    _table_cell(document.conversion_state),
                    document.pdf.sha256 if document.pdf is not None else "—",
                    document.markdown.sha256 if document.markdown is not None else "—",
                    weekly_links,
                )
            )
            + " |"
        )
    lines.extend(("", "## Wochenarchive", "", weekly_links or "—"))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _document_manifest(document: PeriodDocument) -> dict[str, object]:
    if type(document) is not PeriodDocument:
        raise PeriodManifestError("PeriodDocument ist ungültig")
    return {
        "document_id": document.document_id,
        "bitstream_id": document.bitstream_id,
        "version": document.version,
        "source_id": document.source_id,
        "publication_date": document.publication_date,
        "pdf_artifact_id": None if document.pdf is None else document.pdf.artifact_id,
        "pdf_sha256": None if document.pdf is None else document.pdf.sha256,
        "markdown_artifact_id": None if document.markdown is None else document.markdown.artifact_id,
        "markdown_sha256": None if document.markdown is None else document.markdown.sha256,
    }


def _archive_manifest(period: PeriodRef, archive: PlannedArchive, build: ArchiveBuild) -> dict[str, object]:
    if type(archive) is not PlannedArchive or type(build) is not ArchiveBuild:
        raise PeriodManifestError("Archiv-Build ist ungültig")
    expected_entries = tuple(sorted(entry.path for entry in archive.spec.entries))
    if build.entries != expected_entries:
        raise PeriodManifestError("entries stimmen nicht mit Archiv-Spezifikation überein")
    expected_fingerprint = archive_input_fingerprint(archive.spec)
    if build.input_fingerprint != expected_fingerprint:
        raise PeriodManifestError("input_fingerprint stimmt nicht mit Archiv-Spezifikation überein")
    try:
        inspection = validate_archive(
            build.path,
            expected_fingerprint=build.input_fingerprint,
            expected_output_sha256=build.output_sha256,
        )
    except ArchiveError as exc:
        message = str(exc)
        if "SHA-256" in message:
            raise PeriodManifestError("output_sha256 stimmt nicht mit Archivdatei überein") from exc
        if "größe" in message:
            raise PeriodManifestError("size stimmt nicht mit Archivdatei überein") from exc
        raise PeriodManifestError("Archiv-Build ist ungültig") from exc
    if inspection.entries != expected_entries:
        raise PeriodManifestError("entries stimmen nicht mit Archiv-Spezifikation überein")
    if inspection.input_fingerprint != build.input_fingerprint:
        raise PeriodManifestError("input_fingerprint stimmt nicht mit Archiv-Build überein")
    if inspection.output_sha256 != build.output_sha256:
        raise PeriodManifestError("output_sha256 stimmt nicht mit Archiv-Build überein")
    if inspection.size != build.size:
        raise PeriodManifestError("size stimmt nicht mit Archiv-Build überein")
    return {
        "archive_id": archive.spec.archive_id,
        "kind": archive.spec.kind,
        "relative_bundle": archive.relative_bundle,
        "input_fingerprint": build.input_fingerprint,
        "output_sha256": build.output_sha256,
        "bytes": build.size,
        "storage_reference": None,
    }


def _month_manifest_paths(period: PeriodRef, documents: list[dict[str, object]]) -> list[str]:
    if period.kind is not TaskKind.YEAR:
        return []
    months: set[str] = set()
    for document in documents:
        publication_date = document["publication_date"]
        if type(publication_date) is not str:
            raise PeriodManifestError("Dokumentdatum ist ungültig")
        try:
            published = date.fromisoformat(publication_date)
        except ValueError as exc:
            raise PeriodManifestError("Dokumentdatum ist ungültig") from exc
        if published.isoformat() != publication_date or not period.start <= published <= period.end:
            raise PeriodManifestError("Dokumentdatum liegt außerhalb der Jahresperiode")
        months.add(publication_date[:7])
    return [f"rki/Bulletins/Manifeste/Archive/month/{month}.json" for month in sorted(months)]


def _manifest_fingerprint(value: dict[str, object]) -> str:
    normalized: dict[str, object] = {}
    for key, content in value.items():
        if key == "input_fingerprint":
            continue
        if key == "archives" and type(content) is list:
            normalized[key] = [
                {**archive, "storage_reference": None} for archive in content
            ]
        else:
            normalized[key] = content
    return hashlib.sha256(stable_json_dumps(normalized).encode("utf-8")).hexdigest()


def _period_from_manifest(value: dict[str, object]) -> PeriodRef:
    kind_value = value["kind"]
    period_value = value["period"]
    if type(kind_value) is not str or type(period_value) is not str:
        raise PeriodManifestError("Periode ist ungültig")
    try:
        kind = TaskKind(kind_value)
        period = period_ref(kind, period_value)
    except (ValueError, PeriodSelectionError) as exc:
        raise PeriodManifestError("Periode ist ungültig") from exc
    if (
        value["timezone"] != "Europe/Berlin"
        or value["start_date"] != period.start.isoformat()
        or value["end_date"] != period.end.isoformat()
        or value["source_date_epoch"] != period.source_date_epoch
    ):
        raise PeriodManifestError("Periodenmetadaten sind nicht kanonisch")
    return period


def _validate_manifest_order(value: dict[str, object], period: PeriodRef) -> None:
    documents = value["documents"]
    archives = value["archives"]
    month_manifests = value["month_manifests"]
    if type(documents) is not list or type(archives) is not list or type(month_manifests) is not list:
        raise PeriodManifestError("Manifest-Listen sind ungültig")
    if documents != sorted(
        documents,
        key=lambda item: (item["publication_date"], item["document_id"], item["bitstream_id"]),
    ):
        raise PeriodManifestError("Dokumente sind nicht kanonisch sortiert")
    document_identities: set[tuple[str, str]] = set()
    for document in documents:
        document_id = document["document_id"]
        bitstream_id = document["bitstream_id"]
        identity = (document_id, bitstream_id)
        if identity in document_identities:
            raise PeriodManifestError("Dokument-/Bitstream-Identität ist mehrfach vorhanden")
        document_identities.add(identity)
    if archives != sorted(archives, key=lambda item: item["archive_id"]):
        raise PeriodManifestError("Archive sind nicht kanonisch sortiert")
    archive_ids: set[str] = set()
    for archive in archives:
        archive_id = archive["archive_id"]
        kind = archive["kind"]
        if type(archive_id) is not str or type(kind) is not str:
            raise PeriodManifestError("Archivreferenz ist ungültig")
        prefix = f"{period.kind.value}-"
        if not kind.startswith(prefix) or kind.removeprefix(prefix) not in {"pdf", "markdown"}:
            raise PeriodManifestError("Archiv-Art passt nicht zur Periode")
        format_name = kind.removeprefix(prefix)
        if archive_id != f"rki-{period.kind.value}-{period.value.lower()}-{format_name}":
            raise PeriodManifestError("Archiv-ID ist nicht kanonisch")
        if archive["relative_bundle"] != _bundle_path(period, format_name):
            raise PeriodManifestError("Archivpfad ist nicht kanonisch")
        if archive_id in archive_ids:
            raise PeriodManifestError("Archiv-ID ist mehrfach vorhanden")
        archive_ids.add(archive_id)
    expected_months = _month_manifest_paths(period, documents)
    if month_manifests != expected_months:
        raise PeriodManifestError("Monatsmanifeste sind nicht kanonisch")


def _json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PeriodManifestError("Manifest enthält doppelte Felder")
        result[key] = value
    return result


def validate_period_manifest(payload: bytes) -> dict[str, object]:
    """Validate canonical, backend-neutral period-manifest bytes."""

    if type(payload) is not bytes:
        raise PeriodManifestError("Manifest muss bytes sein")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, PeriodManifestError) as exc:
        raise PeriodManifestError("Manifest ist kein gültiges JSON") from exc
    if type(value) is not dict:
        raise PeriodManifestError("Manifest muss ein Objekt sein")
    try:
        validate_document("period-archive-manifest", value)
    except SchemaContractError as exc:
        raise PeriodManifestError("Manifest verletzt Schema-Vertrag") from exc
    period = _period_from_manifest(value)
    _validate_manifest_order(value, period)
    if value["input_fingerprint"] != _manifest_fingerprint(value):
        raise PeriodManifestError("input_fingerprint ist nicht kanonisch")
    canonical = stable_json_dumps(value).encode("utf-8")
    if payload != canonical:
        raise PeriodManifestError("Manifestbytes sind nicht kanonisch")
    return value


def render_period_manifest(period_plan: PeriodPlan, builds: Mapping[str, ArchiveBuild]) -> bytes:
    """Render one canonical period manifest from exact P07.1 archive builds."""

    if type(period_plan) is not PeriodPlan:
        raise PeriodManifestError("period_plan muss ein exakter PeriodPlan sein")
    if not isinstance(builds, Mapping):
        raise PeriodManifestError("builds muss ein Mapping sein")
    expected = {archive.spec.archive_id: archive for archive in period_plan.archives}
    try:
        build_items = tuple(builds.items())
        actual = {archive_id: build for archive_id, build in build_items}
    except (TypeError, ValueError) as exc:
        raise PeriodManifestError("Archiv-ID-Mapping ist ungültig") from exc
    if (
        len(expected) != len(period_plan.archives)
        or len(build_items) != len(actual)
        or set(actual) != set(expected)
    ):
        raise PeriodManifestError("Archiv-ID-Mapping stimmt nicht exakt überein")
    archive_rows = [
        _archive_manifest(period_plan.period, expected[archive_id], build)
        for archive_id, build in build_items
    ]
    documents = sorted(
        (_document_manifest(document) for document in period_plan.documents),
        key=lambda item: (item["publication_date"], item["document_id"], item["bitstream_id"]),
    )
    value: dict[str, object] = {
        "schema_version": "1.0.0",
        "kind": period_plan.period.kind.value,
        "period": period_plan.period.value,
        "timezone": "Europe/Berlin",
        "start_date": period_plan.period.start.isoformat(),
        "end_date": period_plan.period.end.isoformat(),
        "source_date_epoch": period_plan.period.source_date_epoch,
        "input_fingerprint": "",
        "documents": documents,
        "archives": sorted(archive_rows, key=lambda item: item["archive_id"]),
        "month_manifests": _month_manifest_paths(period_plan.period, documents),
    }
    value["input_fingerprint"] = _manifest_fingerprint(value)
    payload = stable_json_dumps(value).encode("utf-8")
    validate_period_manifest(payload)
    return payload


def _same_file_metadata(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare identity and all file-shape fields relevant to stable reads."""

    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )


def _hash_regular_file(parent_fd: int, name: str, expected: os.stat_result) -> tuple[int, str]:
    """Hash one unchanged regular file through a no-follow descriptor."""

    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not _same_file_metadata(expected, before):
            raise AggregationError("Aggregationsbaum enthält keine reguläre Datei")
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(descriptor)
        if not _same_file_metadata(before, after):
            raise AggregationError("Aggregationsdatei änderte sich während der Hash-Prüfung")
        return size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _tree_signature_fd(directory_fd: int, prefix: str = "") -> tuple[tuple[str, int, int, str], ...]:
    """Return file path/mode/size/SHA tuples without resolving any child path."""

    root_before = os.fstat(directory_fd)
    if not stat.S_ISDIR(root_before.st_mode):
        raise AggregationError("Aggregationsbaum enthält kein Verzeichnis")
    rows: list[tuple[str, int, int, str]] = []
    names = tuple(sorted(os.listdir(directory_fd)))
    metadata_by_name: dict[str, os.stat_result] = {}
    for name in names:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        metadata_by_name[name] = metadata
        relative = f"{prefix}/{name}" if prefix else name
        if stat.S_ISLNK(metadata.st_mode):
            raise AggregationError("Symlink im Aggregationsbaum ist unzulässig")
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = open_directory_beneath(directory_fd, (name,))
            try:
                if not _same_file_metadata(metadata, os.fstat(child_fd)):
                    raise AggregationError("Aggregationsverzeichnis änderte sich während der Prüfung")
                rows.extend(_tree_signature_fd(child_fd, relative))
                if not _same_file_metadata(metadata, os.fstat(child_fd)):
                    raise AggregationError("Aggregationsverzeichnis änderte sich während der Prüfung")
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise AggregationError("Aggregationsbaum enthält keine reguläre Datei")
        size, digest = _hash_regular_file(directory_fd, name, metadata)
        if size != metadata.st_size:
            raise AggregationError("Aggregationsdatei änderte sich während der Hash-Prüfung")
        rows.append((relative, stat.S_IMODE(metadata.st_mode), size, digest))
    if tuple(sorted(os.listdir(directory_fd))) != names or not _same_file_metadata(
        root_before, os.fstat(directory_fd)
    ):
        raise AggregationError("Aggregationsverzeichnis änderte sich während der Prüfung")
    for name, before in metadata_by_name.items():
        if not _same_file_metadata(before, os.stat(name, dir_fd=directory_fd, follow_symlinks=False)):
            raise AggregationError("Aggregationsbaum änderte sich während der Prüfung")
    return tuple(rows)


def _existing_tree_signature(root: Path, relative: PurePosixPath) -> tuple[tuple[str, int, int, str], ...]:
    """Inspect final target through held root and target descriptors only."""

    with open_root_directory(root) as root_fd:
        try:
            target_fd = open_directory_beneath(root_fd, relative.parts)
        except FileNotFoundError:
            return ()
        try:
            assert_generated_root_fd(target_fd)
            return _tree_signature_fd(target_fd)
        finally:
            os.close(target_fd)


def _authorize_plan(plan: AggregationPlan, authorizer: RightsStorageAuthorizer) -> None:
    """Reauthorize every planned input before inspecting prior output."""

    for period_plan in plan.periods:
        if type(period_plan) is not PeriodPlan:
            raise AggregationError("AggregationPlan enthält keinen exakten PeriodPlan")
        for document in period_plan.documents:
            if type(document) is not PeriodDocument:
                raise AggregationError("AggregationPlan enthält kein exaktes PeriodDocument")
            for payload in (document.pdf, document.markdown):
                if payload is None:
                    continue
                try:
                    authorize_storage_operation(
                        authorizer,
                        payload,
                        operation="period-archive-materialize",
                    )
                except StorageAuthorizationError as exc:
                    raise AggregationError("Rechteentscheidung autorisiert Archivaggregation nicht") from exc
        for archive in period_plan.archives:
            if type(archive) is not PlannedArchive:
                raise AggregationError("AggregationPlan enthält keinen exakten PlannedArchive")
            for entry in archive.spec.entries:
                try:
                    authorize_storage_operation(
                        authorizer,
                        entry.prepared,
                        operation="period-archive-materialize",
                    )
                except StorageAuthorizationError as exc:
                    raise AggregationError("Rechteentscheidung autorisiert Archivaggregation nicht") from exc


def _snapshot_plan(aggregation_plan: AggregationPlan) -> AggregationPlan:
    """Freeze exact immutable plan records before validation or side effects."""

    if type(aggregation_plan.periods) is not tuple:
        raise AggregationError("AggregationPlan.periods muss ein exaktes tuple sein")
    if type(aggregation_plan.input_fingerprint) is not str:
        raise AggregationError("AggregationPlan.input_fingerprint muss eine Zeichenkette sein")
    periods = tuple(aggregation_plan.periods)
    if any(type(period_plan) is not PeriodPlan for period_plan in periods):
        raise AggregationError("AggregationPlan enthält keinen exakten PeriodPlan")
    if any(type(period_plan.period) is not PeriodRef for period_plan in periods):
        raise AggregationError("PeriodPlan enthält keine exakte PeriodRef")
    for period_plan in periods:
        try:
            if period_plan.period != period_ref(period_plan.period.kind, period_plan.period.value):
                raise AggregationError("PeriodPlan enthält keine kanonische PeriodRef")
        except PeriodSelectionError as exc:
            raise AggregationError("PeriodPlan enthält keine kanonische PeriodRef") from exc
    if periods != tuple(sorted(periods, key=lambda item: (_KIND_ORDER[item.period.kind], item.period.start))):
        raise AggregationError("AggregationPlan-Perioden sind nicht kanonisch sortiert")
    seen_periods: set[tuple[TaskKind, str]] = set()
    for period_plan in periods:
        period_key = (period_plan.period.kind, period_plan.period.value)
        if period_key in seen_periods:
            raise AggregationError("AggregationPlan enthält doppelte Perioden")
        seen_periods.add(period_key)
        if type(period_plan.documents) is not tuple or type(period_plan.archives) is not tuple:
            raise AggregationError("PeriodPlan-Container müssen exakte tuple sein")
        for document in period_plan.documents:
            if type(document) is not PeriodDocument or any(
                payload is not None and type(payload) is not PreparedObject
                for payload in (document.pdf, document.markdown)
            ):
                raise AggregationError("PeriodPlan enthält kein exaktes PeriodDocument")
        if period_plan.documents != tuple(
            sorted(
                period_plan.documents,
                key=lambda item: (item.publication_date, item.document_id, item.bitstream_id),
            )
        ):
            raise AggregationError("PeriodPlan-Dokumente sind nicht kanonisch sortiert")
        expected_index = (
            f"rki/Bulletins/Monate/{period_plan.period.start.year:04d}/"
            f"{period_plan.period.start.month:02d}/Markdown/index.md"
            if period_plan.period.kind is TaskKind.MONTH
            else None
        )
        expected_manifest = (
            f"rki/Bulletins/Manifeste/Archive/{period_plan.period.kind.value}/"
            f"{period_plan.period.value}.json"
        )
        if period_plan.index_path != expected_index or period_plan.manifest_path != expected_manifest:
            raise AggregationError("PeriodPlan-Ausgabepfade sind nicht kanonisch")
        for archive in period_plan.archives:
            if type(archive) is not PlannedArchive or type(archive.spec) is not ArchiveSpec:
                raise AggregationError("PeriodPlan enthält kein exaktes PlannedArchive")
            if type(archive.spec.entries) is not tuple or any(
                type(entry) is not ArchiveEntry or type(entry.prepared) is not PreparedObject
                for entry in archive.spec.entries
            ):
                raise AggregationError("ArchiveSpec.entries muss ein exaktes ArchiveEntry-tuple sein")
        try:
            expected_archives = _planned_archives(period_plan.period, period_plan.documents)
        except (AggregationError, ValueError) as exc:
            raise AggregationError("PeriodPlan-Archive sind nicht kanonisch") from exc
        if period_plan.archives != expected_archives:
            raise AggregationError("PeriodPlan-Archive sind nicht kanonisch")
    snapshot = AggregationPlan(periods=periods, input_fingerprint=aggregation_plan.input_fingerprint)
    if snapshot.input_fingerprint != _plan_fingerprint(snapshot.periods):
        raise AggregationError("AggregationPlan-Fingerprint ist nicht kanonisch")
    _validate_archive_payloads(snapshot)
    return snapshot


def _validate_archive_payloads(plan: AggregationPlan) -> None:
    """Bind every selected payload to exactly one matching period archive."""

    for period_plan in plan.periods:
        by_kind = {archive.spec.kind: archive for archive in period_plan.archives}
        if len(by_kind) != len(period_plan.archives):
            raise AggregationError("Periode enthält mehrdeutige Archive")
        for format_name in ("pdf", "markdown"):
            expected = tuple(
                sorted(
                    (
                        document.pdf if format_name == "pdf" else document.markdown
                        for document in period_plan.documents
                    ),
                    key=lambda payload: "" if payload is None else payload.artifact_id,
                )
            )
            expected = tuple(payload for payload in expected if payload is not None)
            archive = by_kind.get(f"{period_plan.period.kind.value}-{format_name}")
            if not expected:
                if archive is not None:
                    raise AggregationError("Leeres oder unerwartetes Periodenarchiv")
                continue
            if archive is None:
                raise AggregationError("Dokumentpayload besitzt kein Periodenarchiv")
            actual = tuple(sorted((entry.prepared for entry in archive.spec.entries), key=lambda item: item.artifact_id))
            if actual != expected:
                raise AggregationError("Archivpayload stimmt nicht exakt mit Periodendokumenten überein")
        if any(
            archive.spec.kind not in {
                f"{period_plan.period.kind.value}-pdf",
                f"{period_plan.period.kind.value}-markdown",
            }
            for archive in period_plan.archives
        ):
            raise AggregationError("Archiv-Art passt nicht zur Periode")


def _copy_regular_file(
    source_fd: int, destination_fd: int, name: str, expected: os.stat_result
) -> None:
    """Copy one stable regular file between held directory descriptors."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    source = os.open(name, flags, dir_fd=source_fd)
    destination: int | None = None
    try:
        before = os.fstat(source)
        if not stat.S_ISREG(before.st_mode) or not _same_file_metadata(expected, before):
            raise AggregationError("Quellbaum enthält keine reguläre Datei")
        destination = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            stat.S_IMODE(expected.st_mode),
            dir_fd=destination_fd,
        )
        os.fchmod(destination, stat.S_IMODE(expected.st_mode))
        while chunk := os.read(source, 1024 * 1024):
            view = memoryview(chunk)
            while view:
                written = os.write(destination, view)
                if written <= 0:
                    raise AggregationError("FD-Kopie konnte nicht vollständig geschrieben werden")
                view = view[written:]
        os.fsync(destination)
        after = os.fstat(source)
        if not _same_file_metadata(before, after):
            raise AggregationError("Quellbaum änderte sich während FD-Kopie")
    finally:
        if destination is not None:
            os.close(destination)
        os.close(source)


def _copy_tree(source_fd: int, destination_fd: int, *, skip_root_sentinel: bool = False) -> None:
    """Copy a generated tree FD-relativ, no-follow, preserving regular modes."""

    root_before = os.fstat(source_fd)
    if not stat.S_ISDIR(root_before.st_mode):
        raise AggregationError("Quellbaum enthält kein Verzeichnis")
    names = tuple(sorted(os.listdir(source_fd)))
    metadata_by_name: dict[str, os.stat_result] = {}
    for name in names:
        metadata = os.stat(name, dir_fd=source_fd, follow_symlinks=False)
        metadata_by_name[name] = metadata
        if skip_root_sentinel and name == GENERATED_ROOT_SENTINEL:
            continue
        if stat.S_ISLNK(metadata.st_mode):
            raise AggregationError("Symlink im bestehenden Aggregationsbaum ist unzulässig")
        if stat.S_ISDIR(metadata.st_mode):
            os.mkdir(name, stat.S_IMODE(metadata.st_mode), dir_fd=destination_fd)
            source_child = open_directory_beneath(source_fd, (name,))
            destination_child = open_directory_beneath(destination_fd, (name,))
            try:
                if not _same_file_metadata(metadata, os.fstat(source_child)):
                    raise AggregationError("Quellverzeichnis änderte sich während FD-Kopie")
                _copy_tree(source_child, destination_child)
                if not _same_file_metadata(metadata, os.fstat(source_child)):
                    raise AggregationError("Quellverzeichnis änderte sich während FD-Kopie")
                os.fchmod(destination_child, stat.S_IMODE(metadata.st_mode))
                os.fsync(destination_child)
            finally:
                os.close(source_child)
                os.close(destination_child)
        elif stat.S_ISREG(metadata.st_mode):
            _copy_regular_file(source_fd, destination_fd, name, metadata)
        else:
            raise AggregationError("Bestehender Aggregationsbaum enthält keinen regulären Eintrag")
    if tuple(sorted(os.listdir(source_fd))) != names or not _same_file_metadata(
        root_before, os.fstat(source_fd)
    ):
        raise AggregationError("Quellverzeichnis änderte sich während FD-Kopie")
    for name, before in metadata_by_name.items():
        if not _same_file_metadata(before, os.stat(name, dir_fd=source_fd, follow_symlinks=False)):
            raise AggregationError("Quellbaum änderte sich während FD-Kopie")


def _copy_existing_tree(root: Path, relative: PurePosixPath, stage_fd: int) -> None:
    """Clone existing marked output into owned staging through held descriptors."""

    with open_root_directory(root) as root_fd:
        try:
            source_fd = open_directory_beneath(root_fd, relative.parts)
        except FileNotFoundError:
            return
        try:
            assert_generated_root_fd(source_fd)
            _copy_tree(source_fd, stage_fd, skip_root_sentinel=True)
        finally:
            os.close(source_fd)


def _remove_stage_path(stage_fd: int, relative_path: str, *, directory: bool) -> None:
    """Remove one explicitly planned stale product from an owned staging tree."""

    relative = PurePosixPath(_canonical_path(relative_path, label="Stagingpfad"))
    try:
        parent_fd = open_directory_beneath(stage_fd, relative.parts[:-1])
    except FileNotFoundError:
        return
    try:
        if not entry_exists(parent_fd, relative.name):
            return
        metadata = os.stat(relative.name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise AggregationError("Symlink im bestehenden Aggregationsbaum ist unzulässig")
        if directory:
            if not stat.S_ISDIR(metadata.st_mode):
                raise AggregationError("Archivbundle ist kein Verzeichnis")
            remove_tree_at(parent_fd, relative.name, require_sentinel=True)
        else:
            if not stat.S_ISREG(metadata.st_mode):
                raise AggregationError("Periodendatei ist keine reguläre Datei")
            os.unlink(relative.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _clear_planned_outputs(stage_fd: int, plan: AggregationPlan) -> None:
    """Remove only bundle/index/manifest products owned by planned periods."""

    for period_plan in plan.periods:
        for format_name in ("pdf", "markdown"):
            _remove_stage_path(stage_fd, _bundle_path(period_plan.period, format_name), directory=True)
        if period_plan.index_path is not None:
            _remove_stage_path(stage_fd, period_plan.index_path, directory=False)
        _remove_stage_path(stage_fd, period_plan.manifest_path, directory=False)


def _read_stable_regular_at(parent_fd: int, name: str, *, maximum: int) -> bytes:
    """Read one bounded regular file while holding and rechecking its identity."""

    metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        initial = os.fstat(descriptor)
        if (
            not stat.S_ISREG(initial.st_mode)
            or not _same_file_metadata(metadata, initial)
            or initial.st_size > maximum
        ):
            raise AggregationError("Bestehendes Periodenmanifest ist keine sichere reguläre Datei")
        chunks: list[bytes] = []
        remaining = initial.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise AggregationError("Bestehendes Periodenmanifest ist unvollständig")
            chunks.append(chunk)
            remaining -= len(chunk)
        final = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_file_metadata(initial, final) or not _same_file_metadata(final, current):
            raise AggregationError("Bestehendes Periodenmanifest änderte seine Identität")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _load_existing_weekly_references(
    stage_fd: int,
    stage_root: Path,
    months: tuple[PeriodRef, ...],
) -> tuple[WeeklyArchiveReference, ...]:
    """Load only overlapping, schema- and ZIP-valid weekly references from staging."""

    if not months:
        return ()
    try:
        manifest_fd = open_directory_beneath(
            stage_fd,
            ("rki", "Bulletins", "Manifeste", "Archive", "week"),
        )
    except FileNotFoundError:
        return ()
    references: list[WeeklyArchiveReference] = []
    try:
        root_before = os.fstat(manifest_fd)
        names = tuple(sorted(os.listdir(manifest_fd)))
        for name in names:
            match = _WEEK_MANIFEST_NAME.fullmatch(name)
            if match is None:
                raise AggregationError("Wochenmanifest-Namensraum enthält unbekannten Eintrag")
            period = period_ref(TaskKind.WEEK, match["period"])
            if not any(
                period.start <= month.end and period.end >= month.start for month in months
            ):
                continue
            value = validate_period_manifest(
                _read_stable_regular_at(
                    manifest_fd,
                    name,
                    maximum=_MAX_PERIOD_MANIFEST_BYTES,
                )
            )
            if value["kind"] != "week" or value["period"] != period.value:
                raise AggregationError("Wochenmanifest stimmt nicht mit seinem Dateinamen überein")
            archives = value["archives"]
            if type(archives) is not list:
                raise AggregationError("Wochenmanifest-Archive sind ungültig")
            for archive in archives:
                if type(archive) is not dict:
                    raise AggregationError("Wochenmanifest-Archivreferenz ist ungültig")
                kind = _string(archive, "kind", label="Wochenarchiv")
                format_name = kind.removeprefix("week-")
                relative_bundle = _string(
                    archive, "relative_bundle", label="Wochenarchiv"
                )
                relative = PurePosixPath(
                    _canonical_path(relative_bundle, label="Wochenarchivpfad")
                )
                try:
                    bundle_fd = open_directory_beneath(stage_fd, relative.parts)
                except FileNotFoundError as exc:
                    raise AggregationError("Wochenmanifest verweist auf fehlendes Archiv") from exc
                try:
                    build = validate_archive_bundle_fd(
                        bundle_fd,
                        display_root=stage_root / relative_bundle,
                        archive_id=_string(archive, "archive_id", label="Wochenarchiv"),
                        period=period.value,
                        kind=kind,
                        input_fingerprint=_string(
                            archive, "input_fingerprint", label="Wochenarchiv"
                        ),
                        output_sha256=_string(
                            archive, "output_sha256", label="Wochenarchiv"
                        ),
                        size=archive["bytes"],
                    )
                finally:
                    os.close(bundle_fd)
                references.append(
                    WeeklyArchiveReference(
                        period=period,
                        archive_id=_string(archive, "archive_id", label="Wochenarchiv"),
                        kind=kind,
                        relative_bundle=relative_bundle,
                        input_fingerprint=build.input_fingerprint,
                        output_sha256=build.output_sha256,
                        size=build.size,
                    )
                )
        if tuple(sorted(os.listdir(manifest_fd))) != names or not _same_file_metadata(
            root_before, os.fstat(manifest_fd)
        ):
            raise AggregationError("Wochenmanifest-Namensraum änderte sich während des Ladens")
    except (ArchiveError, OSError, PeriodManifestError, UnsafePathError, ValueError) as exc:
        raise AggregationError("Ungültige bestehende Wochenreferenz") from exc
    finally:
        os.close(manifest_fd)
    return tuple(sorted(references, key=_weekly_reference_key))


def _final_build(build: ArchiveBuild, root: Path, relative_bundle: str) -> ArchiveBuild:
    """Translate a stage-local archive identity to its final immutable path."""

    return ArchiveBuild(
        path=root / relative_bundle / "archive.zip",
        input_fingerprint=build.input_fingerprint,
        output_sha256=build.output_sha256,
        size=build.size,
        entries=build.entries,
    )


def _product_files(
    signature: tuple[tuple[str, int, int, str], ...],
) -> tuple[tuple[str, str, int], ...]:
    """List final regular product files without exposing staging sentinel as output."""

    files: list[tuple[str, str, int]] = []
    for relative, _mode, size, digest in signature:
        if PurePosixPath(relative).name == GENERATED_ROOT_SENTINEL:
            continue
        files.append((relative, digest, size))
    return tuple(files)


def materialize_period_archives(
    aggregation_plan: AggregationPlan,
    target: Path,
    *,
    temp_root: Path,
    ledger: EffectLedger,
    authorizer: RightsStorageAuthorizer,
) -> PeriodArchiveMaterialization:
    """Atomically materialize one full aggregation plan below ``temp_root``."""

    if type(aggregation_plan) is not AggregationPlan:
        raise AggregationError("aggregation_plan muss ein exakter AggregationPlan sein")
    if not isinstance(target, Path) or not isinstance(temp_root, Path):
        raise AggregationError("target und temp_root müssen Path-Werte sein")
    if type(ledger) is not EffectLedger:
        raise AggregationError("ledger muss ein exaktes EffectLedger sein")
    if type(authorizer) is not RightsStorageAuthorizer:
        raise AggregationError("authorizer muss ein exakter RightsStorageAuthorizer sein")
    root = temp_root.resolve()
    if ledger.mode is not RunMode.MATERIALIZE or ledger.temp_root != root:
        raise AggregationError("Materialize-Ledger und temp_root müssen exakt passen")
    try:
        relative = relative_path_beneath(target, root)
    except (OSError, UnsafePathError) as exc:
        raise AggregationError("Aggregationsziel liegt außerhalb temp_root") from exc
    final_root = root / Path(relative.as_posix())
    if final_root.is_symlink():
        raise AggregationError("Symlink-Ziel ist unzulässig")
    plan = _snapshot_plan(aggregation_plan)

    _authorize_plan(plan, authorizer)
    event_count = len(ledger.events)
    stage_archives: list[MaterializedPeriodArchive] = []
    index_relatives: list[str] = []
    manifest_relatives: list[str] = []
    pending_events: tuple[tuple[str, str, int], ...] = ()
    staging_state = StagingState()
    stage_signature: tuple[tuple[str, int, int, str], ...] | None = None
    historical_weekly_archives: tuple[WeeklyArchiveReference, ...] = ()

    def validate_published(target_fd: int) -> None:
        if stage_signature is None or _tree_signature_fd(target_fd) != stage_signature:
            raise AggregationError("Veröffentlichte Aggregationsgeneration stimmt nicht mit Staging überein")

    try:
        with staged_directory(
            final_root,
            allowed_root=root,
            replace_existing=True,
            state=staging_state,
            publication_validator=validate_published,
        ) as stage:
            stage_fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                _copy_existing_tree(root, relative, stage_fd)
                _clear_planned_outputs(stage_fd, plan)
                historical_weekly_archives = _load_existing_weekly_references(
                    stage_fd,
                    stage.resolve(),
                    tuple(
                        period_plan.period
                        for period_plan in plan.periods
                        if period_plan.period.kind is TaskKind.MONTH
                    ),
                )
            finally:
                os.close(stage_fd)
            stage_root = stage.resolve()
            inner_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=stage_root)
            builds_by_period: dict[int, dict[str, ArchiveBuild]] = {}
            for period_index, period_plan in enumerate(plan.periods):
                builds: dict[str, ArchiveBuild] = {}
                for archive in period_plan.archives:
                    result = materialize_archive(
                        archive.spec,
                        stage_root / archive.relative_bundle,
                        temp_root=stage_root,
                        ledger=inner_ledger,
                        authorizer=authorizer,
                    )
                    builds[archive.spec.archive_id] = result.build
                    stage_archives.append(
                        MaterializedPeriodArchive(
                            archive_id=archive.spec.archive_id,
                            relative_bundle=archive.relative_bundle,
                            build=result.build,
                        )
                    )
                builds_by_period[period_index] = builds
            for period_index, period_plan in enumerate(plan.periods):
                if period_plan.index_path is not None:
                    index_path = _canonical_path(period_plan.index_path, label="Indexpfad")
                    renderer_input = _month_index_renderer_input(
                        plan, historical_weekly_archives
                    )
                    atomic_write_bytes(
                        stage_root / index_path,
                        render_month_index(
                            period_plan,
                            plan,
                            renderer_input=renderer_input,
                        ),
                        allowed_root=stage_root,
                    )
                    index_relatives.append(index_path)
                manifest_path = _canonical_path(period_plan.manifest_path, label="Manifestpfad")
                manifest_payload = render_period_manifest(period_plan, builds_by_period[period_index])
                validate_period_manifest(manifest_payload)
                atomic_write_bytes(
                    stage_root / manifest_path,
                    manifest_payload,
                    allowed_root=stage_root,
                )
                manifest_relatives.append(manifest_path)
            stage_fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                stage_signature = _tree_signature_fd(stage_fd)
            finally:
                os.close(stage_fd)
            if stage_signature == _existing_tree_signature(root, relative):
                staging_state.no_change = True
            else:
                pending_events = tuple(
                    ((final_root / product_relative).as_posix(), digest, size)
                    for product_relative, digest, size in _product_files(stage_signature)
                )
    except AggregationError:
        if not staging_state.published:
            del ledger.events[event_count:]
        raise
    except (ArchiveError, UnsafePathError, OSError, ValueError) as exc:
        if not staging_state.published:
            del ledger.events[event_count:]
        raise AggregationError("Archivaggregation konnte nicht materialisiert werden") from exc
    except StagingError as exc:
        if not staging_state.published:
            del ledger.events[event_count:]
            raise AggregationError("Archivaggregation konnte nicht materialisiert werden") from exc
        try:
            for event_target, digest, size in pending_events:
                ledger.record(EffectKind.TEMP_FILE, event_target, sha256=digest, size=size)
        except Exception as record_error:
            raise AggregationError("Archivaggregation wurde veröffentlicht, aber konnte nicht vollständig protokolliert werden") from record_error
        raise AggregationError("Archivaggregation wurde veröffentlicht, Cleanup fehlgeschlagen") from exc
    except Exception as exc:
        if not staging_state.published:
            del ledger.events[event_count:]
        raise AggregationError("Archivaggregation konnte nicht materialisiert werden") from exc

    if staging_state.no_change_validated:
        return PeriodArchiveMaterialization(
            root=final_root,
            archives=tuple(
                MaterializedPeriodArchive(
                    archive_id=item.archive_id,
                    relative_bundle=item.relative_bundle,
                    build=_final_build(item.build, final_root, item.relative_bundle),
                )
                for item in stage_archives
            ),
            index_paths=tuple(final_root / item for item in index_relatives),
            manifest_paths=tuple(final_root / item for item in manifest_relatives),
            input_fingerprint=plan.input_fingerprint,
            changed=False,
        )

    try:
        for event_target, digest, size in pending_events:
            ledger.record(EffectKind.TEMP_FILE, event_target, sha256=digest, size=size)
    except Exception as exc:
        raise AggregationError("Archivaggregation wurde veröffentlicht, aber konnte nicht vollständig protokolliert werden") from exc
    final_archives = tuple(
        MaterializedPeriodArchive(
            archive_id=item.archive_id,
            relative_bundle=item.relative_bundle,
            build=_final_build(item.build, final_root, item.relative_bundle),
        )
        for item in stage_archives
    )
    return PeriodArchiveMaterialization(
        root=final_root,
        archives=final_archives,
        index_paths=tuple(final_root / item for item in index_relatives),
        manifest_paths=tuple(final_root / item for item in manifest_relatives),
        input_fingerprint=plan.input_fingerprint,
        changed=True,
    )


_CLI_SOURCE_ID = "rki:176904/900000001"
_CLI_SOURCE_SHA256 = "4665c3b8cfa6de8d9792a8defb977bfd200465b513575419e0a88541000f5b2a"
_CLI_PAYLOAD = b"# Synthetic deterministic archive fixture\n"
_CLI_PAYLOAD_SHA256 = "7c86816c4af09e887f9d46fd7441089955578a82e0287d5d234c07b18b3c658c"
_CLI_UTC = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


def _cli_fixture(temp_root: Path, as_of: datetime) -> tuple[AggregationPlan, RightsStorageAuthorizer]:
    """Build one fixed, local-only fixture through normal aggregation contracts."""

    authority = load_rights_authority()
    policy = load_rights_policy()
    decision = resolve_rights(_CLI_SOURCE_ID, _CLI_SOURCE_SHA256, authority=authority, policy=policy)
    if decision.decision_sha256 is None or decision.state.value != "approved":
        raise AggregationError("Synthetische Aggregationsfixture besitzt keine Freigabe")
    prepared_root = temp_root / "prepared"
    prepared_root.mkdir()
    logical_key = "rki/Bulletins/Jahre/2025/PDF/2025-12-12_fixture.pdf"
    payload_path = prepared_root / "fixture.pdf"
    payload_path.write_bytes(_CLI_PAYLOAD)
    prepared = PreparedObject(
        artifact_id="period-fixture-pdf",
        logical_key=logical_key,
        path=payload_path,
        temp_root=prepared_root,
        sha256=_CLI_PAYLOAD_SHA256,
        size=len(_CLI_PAYLOAD),
        source_id=_CLI_SOURCE_ID,
        source_sha256=_CLI_SOURCE_SHA256,
        decision_sha256=decision.decision_sha256,
        visibility="repository_authorized",
        rights_state=decision.state.value,
        document_id="rki-176904-900000001-v1",
    )
    bitstream_id = "rki-bitstream-" + "a" * 64
    source = {
        "bitstream_id": bitstream_id,
        "source_id": _CLI_SOURCE_ID,
        "title": "Synthetic aggregation bulletin",
        "handle": "176904/900000001",
        "version": 1,
        "publication_date": "2025-12-12",
        "sha256": _CLI_SOURCE_SHA256,
        "decision_sha256": decision.decision_sha256,
        "doi": None,
        "rights": {"state": decision.state.value},
    }
    document = {
        "document_id": "rki-176904-900000001-v1",
        "version": 1,
        "source_id": _CLI_SOURCE_ID,
        "bitstream_id": bitstream_id,
        "publication_date": "2025-12-12",
        "canonical_periods": {"week": "2025-W50", "month": "2025-12", "year": 2025},
        "paths": {"pdf": logical_key, "markdown": None},
        "superseded_by": None,
    }
    storage = {
        "artifact_id": "period-fixture-pdf",
        "relative_path": logical_key,
        "sha256": _CLI_PAYLOAD_SHA256,
        "bytes": len(_CLI_PAYLOAD),
        "source_id": _CLI_SOURCE_ID,
        "source_sha256": _CLI_SOURCE_SHA256,
        "document_id": document["document_id"],
        "conversion_id": None,
        "decision_sha256": decision.decision_sha256,
        "visibility": "repository_authorized",
        "rights_state": decision.state.value,
    }
    due_tasks = tuple(
        DueTask(
            task_id=f"{kind.value}:{period}",
            kind=kind,
            period=period,
            reason="offline fixture",
            due_at="2026-01-01T05:00:00Z",
        )
        for kind, period in (
            (TaskKind.WEEK, "2025-W50"),
            (TaskKind.MONTH, "2025-12"),
            (TaskKind.YEAR, "2025"),
        )
    )
    return (
        plan_period_archives(
            as_of=as_of,
            due_tasks=due_tasks,
            affected_periods=AffectedPeriods(),
            graph=ManifestGraph(
                sources=(source,),
                documents=(document,),
                conversions=(),
                storage_references=(storage,),
            ),
            prepared_by_logical_key={logical_key: prepared},
        ),
        RightsStorageAuthorizer(authority=authority, policy=policy),
    )


def _plan_evidence(plan: AggregationPlan) -> dict[str, object]:
    return {
        "input_fingerprint": plan.input_fingerprint,
        "periods": [
            {
                "archives": [archive.relative_bundle for archive in period.archives],
                "index": period.index_path,
                "kind": period.period.kind.value,
                "manifest": period.manifest_path,
                "period": period.period.value,
            }
            for period in plan.periods
        ],
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aggregate", allow_abbrev=False)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--mode", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run fixed offline aggregate plan or temporary materialization smoke."""

    try:
        args = _build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.mode not in {RunMode.PLAN.value, RunMode.MATERIALIZE.value}:
        print("aggregate: mode muss plan oder materialize sein", file=sys.stderr)
        return 2
    try:
        if _CLI_UTC.fullmatch(args.as_of) is None:
            raise DueTaskError("as_of muss RFC3339 UTC mit T und Z sein")
        as_of = parse_utc(args.as_of)
        with TemporaryDirectory(prefix="desinfect-p07-aggregate-") as directory:
            temp_root = Path(directory).resolve()
            plan, authorizer = _cli_fixture(temp_root, as_of)
            if args.mode == RunMode.PLAN.value:
                evidence = _plan_evidence(plan)
            else:
                result = materialize_period_archives(
                    plan,
                    temp_root / "products",
                    temp_root=temp_root,
                    ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
                    authorizer=authorizer,
                )
                evidence = {
                    "archives": [
                        {
                            "archive_id": archive.archive_id,
                            "bytes": archive.build.size,
                            "input_fingerprint": archive.build.input_fingerprint,
                            "output_sha256": archive.build.output_sha256,
                            "relative_bundle": archive.relative_bundle,
                        }
                        for archive in result.archives
                    ],
                    "changed": result.changed,
                    "indexes": [
                        path.relative_to(result.root).as_posix() for path in result.index_paths
                    ],
                    "input_fingerprint": result.input_fingerprint,
                    "manifests": [
                        path.relative_to(result.root).as_posix() for path in result.manifest_paths
                    ],
                }
            print(stable_json_dumps(evidence), end="")
        return 0
    except OSError:
        print("aggregate: lokaler Ein-/Ausgabefehler", file=sys.stderr)
        return 2
    except (AggregationError, DueTaskError, ValueError) as exc:
        print(f"aggregate: {exc}", file=sys.stderr)
        return 2
