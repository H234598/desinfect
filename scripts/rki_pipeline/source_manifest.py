"""Fail-closed builders for RKI source and document manifests."""
from __future__ import annotations

from datetime import date
from pathlib import Path
import re
from typing import Iterable, Mapping
from urllib.parse import urlsplit

from scripts.rki_grabber.models import ArtifactRecord, RecordState, Scope
from scripts.rki_pipeline.documents import DocumentIdentityError, bitstream_identity, document_identity
from scripts.rki_pipeline.io_utils import atomic_write_text, stable_json_dumps
from scripts.rki_pipeline.paths import DocumentPathError, DocumentType, repository_document_paths
from scripts.rki_pipeline.rights import RightsDecision, RightsState
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document


_COMPLETE_STATES = frozenset({RecordState.EXISTING, RecordState.DOWNLOADED, RecordState.RESUMED})
_BITSTREAM_ID = re.compile(r"^rki-bitstream-[0-9a-f]{64}$")
_DOCUMENT_ID = re.compile(r"^(rki-[a-z0-9-]+)-v([1-9][0-9]*)$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ManifestBuildError(ValueError):
    """An ArtifactRecord cannot safely produce a manifest."""


def _document_type(scope: Scope) -> DocumentType:
    if scope is Scope.ISSUES:
        return DocumentType.ISSUE
    if scope is Scope.ARTICLES:
        return DocumentType.ARTICLE
    raise ManifestBuildError("Scope besitzt keinen konkreten Dokumenttyp")


def _record_identities(record: ArtifactRecord) -> tuple[object, object]:
    _document_type(record.scope)
    if record.state not in _COMPLETE_STATES:
        raise ManifestBuildError("Nur materialisierte PDF-Records erhalten Manifeste")
    if not record.pdf_url or not record.sha256 or not record.publication_date:
        raise ManifestBuildError("PDF-URL, SHA-256 und Publikationsdatum sind erforderlich")
    if not record.item_url or not record.title:
        raise ManifestBuildError("Quell-URL und Titel sind erforderlich")
    try:
        document = document_identity(record.item_handle)
        bitstream = bitstream_identity(record.pdf_url)
        date.fromisoformat(record.publication_date)
    except (DocumentIdentityError, ValueError) as exc:
        raise ManifestBuildError("Record enthält keine kanonische Dokumentidentität") from exc
    if (
        record.document_id != document.document_id
        or record.source_id != document.source_id
        or record.version != document.version
    ):
        raise ManifestBuildError("Record-Identität stimmt nicht mit dem RKI-Handle überein")
    item_url = urlsplit(record.item_url)
    bitstream_path = urlsplit(bitstream.canonical_url).path
    if (
        item_url.scheme != "https"
        or item_url.netloc != "edoc.rki.de"
        or item_url.path != f"/handle/{document.handle}"
        or item_url.query
        or item_url.fragment
        or not bitstream_path.startswith(f"/bitstream/handle/{document.handle}/")
    ):
        raise ManifestBuildError("Record-URLs stimmen nicht mit dem RKI-Handle überein")
    return document, bitstream


def _validated(name: str, payload: dict[str, object]) -> dict[str, object]:
    try:
        validate_document(name, payload)
    except SchemaContractError as exc:
        raise ManifestBuildError(str(exc)) from exc
    return payload


def _aliases(bitstream_id: str, same_content_as: tuple[str, ...]) -> list[str]:
    aliases = list(same_content_as)
    if (
        aliases != sorted(aliases)
        or len(aliases) != len(set(aliases))
        or bitstream_id in aliases
        or any(_BITSTREAM_ID.fullmatch(alias) is None for alias in aliases)
    ):
        raise ManifestBuildError("Content-Aliase müssen sortiert, eindeutig und extern sein")
    return aliases


def build_source_manifest(
    record: ArtifactRecord,
    *,
    rights_decision: RightsDecision,
    same_content_as: tuple[str, ...] = (),
) -> dict[str, object]:
    """Build one validated, rights-fail-closed source manifest."""

    document, bitstream = _record_identities(record)
    if (
        rights_decision.source_id != record.source_id
        or rights_decision.source_sha256 != record.sha256
    ):
        raise ManifestBuildError("Rechteentscheidung gehört zu anderer Quelle oder anderen Bytes")
    if (
        rights_decision.state is RightsState.UNKNOWN
        or not rights_decision.basis
        or (
            rights_decision.state
            in {RightsState.APPROVED, RightsState.INTERNAL_ONLY, RightsState.TAKEDOWN}
            and (
                not rights_decision.reviewed_by
                or not rights_decision.reviewed_at
                or rights_decision.decision_sha256 is None
            )
        )
        or (
            rights_decision.decision_sha256 is not None
            and _SHA256.fullmatch(rights_decision.decision_sha256) is None
        )
    ):
        raise ManifestBuildError("Rechteentscheidung ist nicht kanonisch")
    payload: dict[str, object] = {
        "schema_version": "1.1.0",
        "source_id": document.source_id,
        "handle": document.handle,
        "version": document.version,
        "source_url": record.item_url,
        "title": record.title,
        "publication_date": record.publication_date,
        "etag": record.etag,
        "last_modified": record.last_modified,
        "sha256": record.sha256,
        "rights": {
            "state": rights_decision.state.value,
            "basis": rights_decision.basis,
            "reviewed_at": rights_decision.reviewed_at,
            "reviewed_by": rights_decision.reviewed_by,
        },
        "provenance_state": "current",
        "bitstream_id": bitstream.bitstream_id,
        "bitstream_url": bitstream.canonical_url,
        "bitstream_version": bitstream.version,
        "rights_evidence": {
            "label": record.rights.label,
            "license_url": record.rights.uri,
            "copyright_notice": record.rights.copyright_notice,
            "open_access": record.rights.open_access,
        },
        "decision_sha256": rights_decision.decision_sha256,
        "same_content_as": _aliases(bitstream.bitstream_id, same_content_as),
    }
    return _validated("source-manifest", payload)


def _validated_superseded_by(document_id: str, version: int, value: str | None) -> str | None:
    if value is None:
        return None
    match = _DOCUMENT_ID.fullmatch(value)
    if match is None or match.group(1) != document_id.rsplit("-v", 1)[0] or int(match.group(2)) <= version:
        raise ManifestBuildError("superseded_by muss auf spätere Version desselben Dokuments zeigen")
    return value


def build_document_manifest(
    record: ArtifactRecord,
    *,
    markdown_materialized: bool = False,
    superseded_by: str | None = None,
) -> dict[str, object]:
    """Build one validated document manifest from a materialized PDF record."""

    if type(markdown_materialized) is not bool:
        raise ManifestBuildError("markdown_materialized muss Boolean sein")
    document, bitstream = _record_identities(record)
    try:
        paths = repository_document_paths(
            document_id=document.document_id,
            bitstream_id=bitstream.bitstream_id,
            document_type=_document_type(record.scope),
            publication_date=record.publication_date,
        )
        published = date.fromisoformat(record.publication_date)
    except (DocumentPathError, ValueError) as exc:
        raise ManifestBuildError("Dokumentpfad oder Publikationsdatum ist ungültig") from exc
    iso_year, iso_week, _ = published.isocalendar()
    payload: dict[str, object] = {
        "schema_version": "1.1.0",
        "document_id": document.document_id,
        "version": document.version,
        "source_id": document.source_id,
        "document_type": _document_type(record.scope).value,
        "publication_date": published.isoformat(),
        "paths": {"pdf": paths.pdf, "markdown": paths.markdown if markdown_materialized else None},
        "supersedes": document.supersedes,
        "provenance_state": "current",
        "bitstream_id": bitstream.bitstream_id,
        "bitstream_version": bitstream.version,
        "canonical_periods": {
            "week": f"{iso_year:04d}-W{iso_week:02d}",
            "month": f"{published.year:04d}-{published.month:02d}",
            "year": published.year,
        },
        "superseded_by": _validated_superseded_by(
            document.document_id, document.version, superseded_by
        ),
    }
    return _validated("document-manifest", payload)


def build_source_manifests(
    records: Iterable[ArtifactRecord],
    *,
    rights_decisions: Mapping[tuple[str, str], RightsDecision],
) -> tuple[dict[str, object], ...]:
    """Build sorted source manifests with explicit same-content aliases."""

    by_id: dict[str, tuple[ArtifactRecord, dict[str, object]]] = {}
    hashes: dict[str, list[str]] = {}
    for record in records:
        _, bitstream = _record_identities(record)
        decision = rights_decisions.get((record.source_id, record.sha256))
        if decision is None:
            raise ManifestBuildError("Rechteentscheidung für Source-ID und SHA-256 fehlt")
        payload = build_source_manifest(record, rights_decision=decision)
        previous = by_id.get(bitstream.bitstream_id)
        if previous is not None and previous[1] != payload:
            raise ManifestBuildError("Gleiche Bitstream-Identität hat widersprüchliche Record-Daten")
        by_id[bitstream.bitstream_id] = (record, payload)
        hashes.setdefault(record.sha256, []).append(bitstream.bitstream_id)

    aliases: dict[str, tuple[str, ...]] = {bitstream_id: () for bitstream_id in by_id}
    for ids in hashes.values():
        unique_ids = sorted(set(ids))
        if len(unique_ids) > 1:
            canonical = unique_ids[0]
            for bitstream_id in unique_ids[1:]:
                aliases[bitstream_id] = (canonical,)
    return tuple(
        build_source_manifest(
            by_id[bitstream_id][0],
            rights_decision=rights_decisions[
                (by_id[bitstream_id][0].source_id, by_id[bitstream_id][0].sha256)
            ],
            same_content_as=aliases[bitstream_id],
        )
        for bitstream_id in sorted(by_id)
    )


def write_manifest(
    path: Path, payload: dict[str, object], *, contract_name: str, allowed_root: Path
) -> None:
    """Validate then atomically write one manifest beneath its allowed root."""

    _validated(contract_name, payload)
    atomic_write_text(path, stable_json_dumps(payload), allowed_root=allowed_root)
