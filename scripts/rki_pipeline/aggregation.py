#!/usr/bin/env python3
"""Strict period selection for deterministic archive aggregation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
import hashlib
import json
import posixpath
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from scripts.rki_grabber.models import AffectedPeriods
from scripts.rki_pipeline.archive import ArchiveBuild, ArchiveEntry, ArchiveSpec, archive_input_fingerprint
from scripts.rki_pipeline.due_tasks import DueTask, TaskKind
from scripts.rki_pipeline.io_utils import normalize_posix_path, sha256_file, stable_json_dumps
from scripts.rki_pipeline.manifests import ManifestGraph
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document
from scripts.rki_pipeline.storage.base import PreparedObject

_BERLIN = ZoneInfo("Europe/Berlin")
_WEEK = re.compile(r"^(?P<year>[0-9]{4})-W(?P<week>0[1-9]|[1-4][0-9]|5[0-3])$")
_MONTH = re.compile(r"^(?P<year>[0-9]{4})-(?P<month>0[1-9]|1[0-2])$")
_YEAR = re.compile(r"^[0-9]{4}$")
_KIND_ORDER = {TaskKind.WEEK: 0, TaskKind.MONTH: 1, TaskKind.YEAR: 2}


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
    return PeriodRef(kind, value, start, end, int(close.timestamp()))


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
                version=version,
                source_id=_string(document, "source_id", label="Dokument"),
                publication_date=_string(document, "publication_date", label="Dokument"),
                title=_string(source, "title", label="Source"),
                handle=_string(source, "handle", label="Source"),
                doi=None,
                conversion_state=state,
                pdf=pdf,
                markdown=markdown,
            )
        )
    return tuple(sorted(selected, key=lambda item: (item.publication_date, item.document_id, item.source_id)))


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
    return posixpath.relpath(target, PurePosixPath(source).parent.as_posix())


def _table_cell(value: str) -> str:
    """Escape untrusted metadata for one stable Markdown table cell."""

    if type(value) is not str:
        raise AggregationError("Tabellenwert muss eine Zeichenkette sein")
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
    )


def _week_archive_links(period: PeriodRef, index_path: str) -> str:
    monday = date.fromordinal(period.start.toordinal() - period.start.weekday())
    links: list[str] = []
    while monday <= period.end:
        iso = monday.isocalendar()
        week = period_ref(TaskKind.WEEK, f"{iso.year:04d}-W{iso.week:02d}")
        for format_name, label in (("pdf", "PDF"), ("markdown", "Markdown")):
            target = f"{_bundle_path(week, format_name)}/archive.zip"
            links.append(f"[{label}]({_relative_link(index_path, target)})")
        monday = date.fromordinal(monday.toordinal() + 7)
    return " ".join(links)


def render_month_index(period_plan: PeriodPlan) -> bytes:
    """Render one deterministic Markdown index for a monthly period."""

    if type(period_plan) is not PeriodPlan:
        raise AggregationError("period_plan muss ein exakter PeriodPlan sein")
    if period_plan.period.kind is not TaskKind.MONTH or period_plan.index_path is None:
        raise AggregationError("Monatsindex benötigt eine Monatsperiode mit Indexpfad")
    index_path = _canonical_path(period_plan.index_path, label="Indexpfad")
    documents = tuple(
        sorted(
            period_plan.documents,
            key=lambda item: (item.publication_date, item.document_id, item.source_id),
        )
    )
    lines = [
        f"# RKI-Einzelartikel {period_plan.period.value}",
        "",
        f"Artikel: {len(documents)}",
        "",
        "| Datum | Titel | RKI-Handle | DOI | PDF | Markdown | Konvertierung | PDF SHA-256 | Markdown SHA-256 | Wochenarchive |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    weekly_links = _week_archive_links(period_plan.period, index_path)
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
    return ("\n".join(lines) + "\n").encode("utf-8")


def _document_manifest(document: PeriodDocument) -> dict[str, object]:
    if type(document) is not PeriodDocument:
        raise PeriodManifestError("PeriodDocument ist ungültig")
    return {
        "document_id": document.document_id,
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
    if build.path.is_symlink() or not build.path.is_file():
        raise PeriodManifestError("Archiv-Build-Pfad ist keine reguläre Datei")
    before = build.path.stat()
    if before.st_size != build.size:
        raise PeriodManifestError("size stimmt nicht mit Archivdatei überein")
    if sha256_file(build.path) != build.output_sha256:
        raise PeriodManifestError("output_sha256 stimmt nicht mit Archivdatei überein")
    after = build.path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise PeriodManifestError("Archivdatei änderte sich während der Prüfung")
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
        key=lambda item: (item["publication_date"], item["document_id"], item["source_id"]),
    ):
        raise PeriodManifestError("Dokumente sind nicht kanonisch sortiert")
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
    if len(expected) != len(period_plan.archives) or set(builds) != set(expected):
        raise PeriodManifestError("Archiv-ID-Mapping stimmt nicht exakt überein")
    archive_rows = [
        _archive_manifest(period_plan.period, expected[archive_id], build)
        for archive_id, build in builds.items()
        if archive_id in expected
    ]
    documents = sorted(
        (_document_manifest(document) for document in period_plan.documents),
        key=lambda item: (item["publication_date"], item["document_id"], item["source_id"]),
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
