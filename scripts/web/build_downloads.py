"""Fail-closed, backend-neutral public download projection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import html
import re
from typing import Mapping
from urllib.parse import urlsplit

from scripts.rki_pipeline.manifests import ManifestGraph
from scripts.rki_pipeline.rights import (
    ApprovalKey,
    ComponentsState,
    PublicationMode,
    RightsAction,
    RightsAttribution,
    RightsPolicy,
    RightsPolicyError,
    RightsRegister,
    RightsState,
    resolve_action,
    validate_license_url,
)
from scripts.rki_pipeline.storage.base import StorageBackend


DOWNLOAD_MARKER = "<!-- DESINFECT_DOWNLOADS -->"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ID = re.compile(r"^rki:176904/[0-9]+(?:\.(?:[2-9]|[1-9][0-9]+))?$")
_STATUS_LABELS = {
    "not_initialized": "Noch nicht initialisiert",
    "operational": "Betriebsbereit",
    "degraded": "Eingeschränkt verfügbar",
    "blocked": "Blockiert",
    "unknown": "Unbekannt",
}
_RIGHTS_LABELS = {
    RightsState.APPROVED: "Freigegeben",
    RightsState.METADATA_ONLY: "Nur Metadaten",
    RightsState.INTERNAL_ONLY: "Nur intern",
    RightsState.UNKNOWN: "Ungeklärt",
    RightsState.TAKEDOWN: "Entfernt",
}


class DownloadProjectionError(ValueError):
    """A public projection cannot be derived safely."""


@dataclass(frozen=True, slots=True)
class DownloadRow:
    """Public row without backend path or object identity."""

    source_id: str
    title: str
    publication_date: str
    approval_key: ApprovalKey
    rights_state: RightsState
    mode: PublicationMode
    origin_url: str
    artifact_kind: str | None
    artifact_id: str | None
    bitstream_id: str | None
    artifact_url: str | None
    artifact_sha256: str | None
    artifact_bytes: int | None
    storage_backend: StorageBackend | None
    attribution: RightsAttribution | None


@dataclass(frozen=True, slots=True, repr=False)
class DownloadTombstone:
    """Internal only removal record; never rendered publicly."""

    source_id: str
    artifact_id: str


@dataclass(frozen=True, slots=True)
class DownloadInputs:
    rows: tuple[DownloadRow, ...]
    tombstones: tuple[DownloadTombstone, ...]
    public_status: Mapping[str, object]
    input_hashes: Mapping[str, str]


def _require_string(value: object, name: str) -> str:
    if (
        type(value) is not str
        or not value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise DownloadProjectionError(f"{name} muss nichtleerer Text ohne Steuerzeichen sein")
    return value


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DownloadProjectionError(f"{name} muss ein Objekt sein")
    return value


def _publication_date(value: object) -> str:
    raw = _require_string(value, "publication_date")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise DownloadProjectionError("publication_date ist ungültig") from exc
    if parsed.isoformat() != raw:
        raise DownloadProjectionError("publication_date ist nicht kanonisch")
    return raw


def _safe_https_url(value: object, name: str) -> str:
    url = _require_string(value, name)
    if (
        not url.isascii()
        or "\\" in url
        or re.search(r"%(?:0[0-9a-f]|1[0-9a-f]|7f|2f|5c)", url, re.I)
    ):
        raise DownloadProjectionError(f"{name} enthält verschleierte oder unsichere Zeichen")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise DownloadProjectionError(f"{name} besitzt einen ungültigen Port") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.netloc != parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or parsed.query
        or parsed.fragment
    ):
        raise DownloadProjectionError(f"{name} muss absolute kanonische HTTPS-URL sein")
    return url


def validate_origin_url(value: str, *, source_id: str) -> str:
    """Accept only exact canonical RKI handles for the given source."""

    if type(source_id) is not str or _SOURCE_ID.fullmatch(source_id) is None:
        raise DownloadProjectionError("source_id ist nicht kanonisch")
    url = _safe_https_url(value, "origin_url")
    expected = "https://edoc.rki.de/handle/" + source_id.removeprefix("rki:")
    if url != expected:
        raise DownloadProjectionError("origin_url passt nicht zur source_id")
    return url


def _backend(value: object) -> StorageBackend:
    if type(value) is not str:
        raise DownloadProjectionError("storage_backend fehlt")
    try:
        return StorageBackend(value)
    except ValueError as exc:
        raise DownloadProjectionError("storage_backend ist unbekannt") from exc


def _source_key(source: Mapping[str, object]) -> ApprovalKey:
    try:
        return ApprovalKey(
            source_id=_require_string(source.get("source_id"), "source_id"),
            canonical_url=_require_string(source.get("bitstream_url"), "bitstream_url"),
            version_or_bitstream=_require_string(source.get("bitstream_id"), "bitstream_id"),
            source_sha256=_require_string(source.get("sha256"), "source.sha256"),
        )
    except RightsPolicyError as exc:
        raise DownloadProjectionError("Source-ApprovalKey ist ungültig") from exc


def _decision_for(source: Mapping[str, object], *, register: RightsRegister, policy: RightsPolicy):
    key = _source_key(source)
    if type(register) is not RightsRegister or type(policy) is not RightsPolicy:
        raise DownloadProjectionError("Register oder Policy ist nicht kanonisch")
    decision = next((entry for entry in register.entries if entry.approval_key == key), None)
    if decision is None or decision.decision_sha256 is None:
        raise DownloadProjectionError("Source besitzt keine aktuelle Rechteentscheidung")
    if source.get("decision_sha256") != decision.decision_sha256:
        raise DownloadProjectionError("Source-Rechteentscheidung ist veraltet")
    if decision.mode is PublicationMode.MATERIALIZED:
        try:
            decision = resolve_action(
                key, action=RightsAction.PUBLISH, register=register, policy=policy
            )
        except RightsPolicyError as exc:
            raise DownloadProjectionError(
                "Source ist nicht für öffentliche Publikation freigegeben"
            ) from exc
    elif decision.mode is PublicationMode.REMOVE_ALL:
        if decision.state is not RightsState.TAKEDOWN:
            raise DownloadProjectionError("remove_all erfordert takedown")
    elif decision.mode not in {PublicationMode.SOURCE_ONLY, PublicationMode.ORIGIN_LINK}:
        raise DownloadProjectionError("Publikationsmodus ist unbekannt")
    if decision.mode is PublicationMode.MATERIALIZED and (
        decision.state is not RightsState.APPROVED
        or decision.components_state is not ComponentsState.CLEARED
        or decision.attribution is None
    ):
        raise DownloadProjectionError("Source-Rechteentscheidung ist nicht veröffentlichbar")
    return key, decision


def _artifact_rows(
    source: Mapping[str, object],
    document: Mapping[str, object],
    storage: tuple[Mapping[str, object], ...],
    *,
    key: ApprovalKey,
    attribution: RightsAttribution,
    decision_sha256: str,
    rights_state: RightsState,
) -> tuple[DownloadRow, ...]:
    paths = _mapping(document.get("paths"), "document.paths")
    pdf_path = _require_string(paths.get("pdf"), "document.paths.pdf")
    markdown_value = paths.get("markdown")
    if markdown_value is not None and type(markdown_value) is not str:
        raise DownloadProjectionError("document.paths.markdown muss Text oder None sein")
    markdown_path = (
        None
        if markdown_value is None
        else _require_string(markdown_value, "document.paths.markdown")
    )
    if markdown_path is not None and pdf_path == markdown_path:
        raise DownloadProjectionError("PDF- und Markdown-Pfad kollidieren")
    rows: list[DownloadRow] = []
    seen_references: set[str] = set()
    seen_artifacts: set[tuple[str, str]] = set()
    seen_kinds: set[str] = set()
    for reference in storage:
        relative_path = _require_string(reference.get("relative_path"), "relative_path")
        if relative_path == pdf_path:
            kind = "pdf"
        elif relative_path == markdown_path:
            kind = "markdown"
        else:
            continue
        if (
            reference.get("source_id") != key.source_id
            or reference.get("source_sha256") != key.source_sha256
        ):
            raise DownloadProjectionError("Storage-Referenz gehört nicht zur Source")
        if reference.get("document_id") != document.get("document_id"):
            raise DownloadProjectionError("Storage-Referenz gehört nicht zum Dokument")
        if (
            reference.get("decision_sha256") != decision_sha256
            or reference.get("provenance_state") != "current"
            or reference.get("visibility") != "public"
            or reference.get("rights_state") != RightsState.APPROVED.value
        ):
            raise DownloadProjectionError(
                "Storage-Referenz ist nicht aktuell öffentlich freigegeben"
            )
        if kind in seen_kinds:
            raise DownloadProjectionError("Storage-Referenz ist für Artefaktart mehrdeutig")
        seen_kinds.add(kind)
        artifact_id = _require_string(reference.get("artifact_id"), "artifact_id")
        identity = (kind, artifact_id)
        if identity in seen_artifacts:
            raise DownloadProjectionError("Artefaktidentität ist doppelt")
        seen_artifacts.add(identity)
        reference_url = _safe_https_url(reference.get("public_reference"), "public_reference")
        if reference_url in seen_references:
            raise DownloadProjectionError("public_reference ist doppelt")
        seen_references.add(reference_url)
        artifact_sha256 = _require_string(reference.get("sha256"), "artifact.sha256")
        if _SHA256.fullmatch(artifact_sha256) is None:
            raise DownloadProjectionError("artifact.sha256 ist ungültig")
        artifact_bytes = reference.get("bytes")
        if type(artifact_bytes) is not int or artifact_bytes < 0:
            raise DownloadProjectionError("artifact.bytes ist ungültig")
        rows.append(
            DownloadRow(
                source_id=key.source_id,
                title=_require_string(source.get("title"), "title"),
                publication_date=_publication_date(source.get("publication_date")),
                approval_key=key,
                rights_state=rights_state,
                mode=PublicationMode.MATERIALIZED,
                origin_url=validate_origin_url(
                    _require_string(source.get("source_url"), "source_url"), source_id=key.source_id
                ),
                artifact_kind=kind,
                artifact_id=artifact_id,
                bitstream_id=key.version_or_bitstream,
                artifact_url=reference_url,
                artifact_sha256=artifact_sha256,
                artifact_bytes=artifact_bytes,
                storage_backend=_backend(reference.get("storage_backend")),
                attribution=attribution,
            )
        )
    if not rows:
        raise DownloadProjectionError("Materialized Source besitzt keine Storage-Referenz")
    return tuple(rows)


def project_downloads(
    graph: ManifestGraph,
    status: Mapping[str, object] | None = None,
    *,
    register: RightsRegister,
    policy: RightsPolicy,
) -> DownloadInputs:
    """Derive safe public rows from IDs and public references only."""

    if type(graph) is not ManifestGraph:
        raise DownloadProjectionError("graph muss exakter ManifestGraph sein")
    public_status: Mapping[str, object] = (
        {"status": "unknown"} if status is None else _mapping(status, "status")
    )
    documents: dict[tuple[str, str], Mapping[str, object]] = {}
    for raw_document in graph.documents:
        document = _mapping(raw_document, "document")
        source_id = _require_string(document.get("source_id"), "document.source_id")
        bitstream_id = _require_string(document.get("bitstream_id"), "document.bitstream_id")
        identity = (source_id, bitstream_id)
        if identity in documents:
            raise DownloadProjectionError("Dokumentzuordnung ist mehrdeutig")
        documents[identity] = document
    references: dict[tuple[str, str], list[Mapping[str, object]]] = {}
    references_by_source: dict[str, list[Mapping[str, object]]] = {}
    for raw_reference in graph.storage_references:
        reference = _mapping(raw_reference, "storage_reference")
        source_id = _require_string(reference.get("source_id"), "storage.source_id")
        document_id = _require_string(reference.get("document_id"), "storage.document_id")
        references.setdefault((source_id, document_id), []).append(reference)
        references_by_source.setdefault(source_id, []).append(reference)
    rows: list[DownloadRow] = []
    tombstones: list[DownloadTombstone] = []
    for raw_source in graph.sources:
        source = _mapping(raw_source, "source")
        key, decision = _decision_for(source, register=register, policy=policy)
        if decision.mode is PublicationMode.REMOVE_ALL:
            tombstones.extend(
                DownloadTombstone(
                    key.source_id,
                    _require_string(reference.get("artifact_id"), "artifact_id"),
                )
                for reference in references_by_source.get(key.source_id, ())
            )
            continue
        origin_url = validate_origin_url(
            _require_string(source.get("source_url"), "source_url"), source_id=key.source_id
        )
        if decision.mode in {PublicationMode.SOURCE_ONLY, PublicationMode.ORIGIN_LINK}:
            rows.append(
                DownloadRow(
                    source_id=key.source_id,
                    title=_require_string(source.get("title"), "title"),
                    publication_date=_publication_date(source.get("publication_date")),
                    approval_key=key,
                    rights_state=decision.state,
                    mode=decision.mode,
                    origin_url=origin_url,
                    artifact_kind=None,
                    artifact_id=None,
                    bitstream_id=key.version_or_bitstream,
                    artifact_url=None,
                    artifact_sha256=None,
                    artifact_bytes=None,
                    storage_backend=None,
                    attribution=None,
                )
            )
            continue
        document = documents.get((key.source_id, key.version_or_bitstream))
        if document is None:
            raise DownloadProjectionError("Source besitzt kein eindeutiges Dokument")
        source_references = tuple(
            references.get(
                (key.source_id, _require_string(document.get("document_id"), "document_id")), ()
            )
        )
        rows.extend(
            _artifact_rows(
                source,
                document,
                source_references,
                key=key,
                attribution=decision.attribution,
                decision_sha256=decision.decision_sha256,
                rights_state=decision.state,
            )
        )
    rows.sort(
        key=lambda row: (
            -date.fromisoformat(row.publication_date).toordinal(),
            row.source_id,
            row.bitstream_id or "",
            {"pdf": 0, "markdown": 1}.get(row.artifact_kind, 2),
            row.artifact_id or "",
        )
    )
    materialized_references = [
        row.artifact_url for row in rows if row.mode is PublicationMode.MATERIALIZED
    ]
    if len(set(materialized_references)) != len(materialized_references):
        raise DownloadProjectionError("public_reference ist global doppelt")
    return DownloadInputs(tuple(rows), tuple(tombstones), public_status, {})


def _status_fragment(status: Mapping[str, object]) -> str:
    state = status.get("status")
    if state not in _STATUS_LABELS:
        raise DownloadProjectionError("öffentlicher Status ist unbekannt")
    parts = [
        '<section class="publication-status" role="status" aria-live="polite" aria-labelledby="publication-status-title">',
        '<h2 id="publication-status-title">Datenstatus</h2>',
        f"<p>{html.escape(_STATUS_LABELS[state], quote=True)}</p>",
    ]
    pipeline = status.get("pipeline")
    has_success = (
        isinstance(pipeline, Mapping) and pipeline.get("last_successful_run_at") is not None
    )
    if has_success:
        parts.append(
            f"<p>Letzter erfolgreicher Lauf: {html.escape(_require_string(pipeline['last_successful_run_at'], 'last_successful_run_at'), quote=True)}</p>"
        )
    if state in {"degraded", "blocked"}:
        if not has_success:
            parts.append("<p>Kein erfolgreicher Lauf dokumentiert.</p>")
        parts.append('<p><a href="/Wartung/Wiederherstellung/">Wiederherstellungshinweise</a></p>')
    return "".join(parts) + "</section>"


def _row_fragment(row: DownloadRow, *, include_attribution: bool) -> str:
    if type(row) is not DownloadRow:
        raise DownloadProjectionError("row muss exakter DownloadRow sein")
    if row.mode not in {
        PublicationMode.MATERIALIZED,
        PublicationMode.SOURCE_ONLY,
        PublicationMode.ORIGIN_LINK,
    }:
        raise DownloadProjectionError("row besitzt keinen öffentlichen Publikationsmodus")
    if type(row.rights_state) is not RightsState:
        raise DownloadProjectionError("row.rights_state ist nicht kanonisch")
    if row.bitstream_id != row.approval_key.version_or_bitstream:
        raise DownloadProjectionError("row.bitstream_id passt nicht zum ApprovalKey")
    title = html.escape(row.title, quote=True)
    origin = validate_origin_url(row.origin_url, source_id=row.source_id)
    publication_date = html.escape(_publication_date(row.publication_date), quote=True)
    parts = [
        '<article class="download">',
        f"<h3>{title}</h3>",
        f"<p>Erscheinungsdatum: {publication_date}</p>",
    ]
    if row.mode is PublicationMode.MATERIALIZED:
        if (
            row.artifact_url is None
            or row.artifact_kind not in {"pdf", "markdown"}
            or type(row.artifact_id) is not str
            or _SHA256.fullmatch(row.artifact_sha256 or "") is None
            or type(row.artifact_bytes) is not int
            or row.artifact_bytes < 0
            or type(row.storage_backend) is not StorageBackend
            or row.attribution is None
        ):
            raise DownloadProjectionError("materialized Row ist unvollständig")
        artifact_url = _safe_https_url(row.artifact_url, "artifact_url")
        text = "PDF herunterladen" if row.artifact_kind == "pdf" else "Quell-Markdown herunterladen"
        parts.append(
            f'<p><a href="{html.escape(artifact_url, quote=True)}" rel="noopener noreferrer">{text}</a></p>'
        )
        license_url = validate_license_url(
            row.attribution.license_url, license_notice=row.attribution.license_notice
        )
        if include_attribution:
            parts.append(
                f'<p>{html.escape(row.attribution.copyright_notice, quote=True)} · <a href="{html.escape(license_url, quote=True)}" rel="noopener noreferrer">{html.escape(row.attribution.license_notice, quote=True)}</a></p>'
            )
        if row.rights_state is not RightsState.APPROVED:
            raise DownloadProjectionError(
                "materialized Row besitzt keinen freigegebenen Rechtezustand"
            )
        parts.append(
            "<p>Rechtestatus: Freigegeben. Veröffentlichungsmodus: Materialisiert. Verfügbarkeit: Download verfügbar.</p>"
        )
    else:
        if any(
            value is not None
            for value in (
                row.artifact_kind,
                row.artifact_id,
                row.artifact_url,
                row.artifact_sha256,
                row.artifact_bytes,
                row.storage_backend,
                row.attribution,
            )
        ):
            raise DownloadProjectionError("nicht-materialized Row enthält Artefaktdaten")
        mode = "Nur Quelle" if row.mode is PublicationMode.SOURCE_ONLY else "Origin-Link"
        parts.append(
            f"<p>Rechtestatus: {_RIGHTS_LABELS[row.rights_state]}. "
            f"Veröffentlichungsmodus: {mode}. Verfügbarkeit: Beim RKI verfügbar.</p>"
        )
    parts.append(
        f'<p><a href="{html.escape(origin, quote=True)}" rel="noopener noreferrer">Beim RKI öffnen</a></p>'
    )
    return "".join(parts) + "</article>"


def render_download_page(source: str, inputs: DownloadInputs) -> str:
    """Replace exactly one marker with escaped public status and downloads."""

    if type(source) is not str or source.count(DOWNLOAD_MARKER) != 1:
        raise DownloadProjectionError("download marker muss genau einmal vorkommen")
    if type(inputs) is not DownloadInputs:
        raise DownloadProjectionError("inputs muss exakter DownloadInputs sein")
    fragment = _status_fragment(inputs.public_status)
    if inputs.rows:
        attributed: set[ApprovalKey] = set()
        rows: list[str] = []
        for row in inputs.rows:
            include_attribution = (
                row.mode is PublicationMode.MATERIALIZED and row.approval_key not in attributed
            )
            rows.append(_row_fragment(row, include_attribution=include_attribution))
            if row.mode is PublicationMode.MATERIALIZED:
                attributed.add(row.approval_key)
        fragment += '<section class="downloads"><h2>Downloads</h2>' + "".join(rows) + "</section>"
    else:
        fragment += '<section class="downloads"><h2>Downloads</h2><p>Keine öffentlichen Downloads verfügbar.</p></section>'
    return source.replace(DOWNLOAD_MARKER, fragment)
