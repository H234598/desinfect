#!/usr/bin/env python3
"""Schema-first reference graph for current RKI manifest collections."""
from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from typing import Iterable

from scripts.rki_pipeline.documents import bitstream_identity, document_identity
from scripts.rki_pipeline.io_utils import detect_path_collisions
from scripts.rki_pipeline.paths import DocumentType, repository_document_paths
from scripts.rki_pipeline.schema_registry import validate_document

Manifest = dict[str, object]


class ManifestGraphError(ValueError):
    """Manifest records are valid alone but inconsistent as one snapshot."""


@dataclass(frozen=True, slots=True)
class ManifestGraph:
    """Stable, validated collections ready for canonical rendering."""

    sources: tuple[Manifest, ...]
    documents: tuple[Manifest, ...]
    conversions: tuple[Manifest, ...]
    storage_references: tuple[Manifest, ...]


def _validated(
    values: Iterable[Manifest],
    *,
    contract: str,
) -> tuple[Manifest, ...]:
    result: list[Manifest] = []
    for value in values:
        if type(value) is not dict:
            raise ManifestGraphError(f"{contract}: Manifest muss ein exaktes Objekt sein")
        validate_document(contract, value)
        if value.get("provenance_state") != "current":
            raise ManifestGraphError(f"{contract}: nur current ist im Manifestgraph zulässig")
        result.append(deepcopy(value))
    return tuple(result)


def _index(
    values: Iterable[Manifest],
    *,
    key,
    label: str,
) -> dict[object, Manifest]:
    result: dict[object, Manifest] = {}
    for value in values:
        identity = key(value)
        if identity in result:
            raise ManifestGraphError(f"{label}-Identität ist doppelt: {identity}")
        result[identity] = value
    return result


def _reject_cycles(
    nodes: Iterable[str],
    edges: dict[str, tuple[str, ...]],
    *,
    label: str,
) -> None:
    node_set = set(nodes)
    indegree = dict.fromkeys(node_set, 0)
    outgoing: dict[str, list[str]] = defaultdict(list)
    for source, targets in edges.items():
        for target in targets:
            if target not in node_set:
                raise ManifestGraphError(f"{label}-Ziel fehlt: {target}")
            outgoing[source].append(target)
            indegree[target] += 1
    ready = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    seen = 0
    while ready:
        source = ready.popleft()
        seen += 1
        for target in outgoing[source]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if seen != len(node_set):
        raise ManifestGraphError(f"{label}-Graph enthält einen Zyklus")


def _validate_sources(sources: dict[str, Manifest]) -> None:
    by_sha256: dict[str, list[str]] = defaultdict(list)
    for bitstream_id, source in sources.items():
        identity = document_identity(source["handle"])
        if (
            source["source_id"] != identity.source_id
            or source["version"] != identity.version
        ):
            raise ManifestGraphError("Source-Identität widerspricht RKI-Handle")
        bitstream = bitstream_identity(source["bitstream_url"])
        if (
            bitstream_id != bitstream.bitstream_id
            or source["bitstream_version"] != bitstream.version
        ):
            raise ManifestGraphError("Source-Bitstream widerspricht kanonischer URL")
        by_sha256[source["sha256"]].append(bitstream_id)
    for ids in by_sha256.values():
        ordered = sorted(ids)
        canonical = ordered[0]
        for bitstream_id in ordered:
            expected = [] if bitstream_id == canonical else [canonical]
            if sources[bitstream_id]["same_content_as"] != expected:
                raise ManifestGraphError("Source-Alias ist nicht kanonisch oder enthält Zyklus")


def _validate_documents(
    documents: dict[tuple[str, str], Manifest],
    sources: dict[str, Manifest],
) -> None:
    by_document: dict[str, list[Manifest]] = defaultdict(list)
    for (document_id, bitstream_id), document in documents.items():
        source = sources.get(bitstream_id)
        if source is None:
            raise ManifestGraphError(f"Dokument-Bitstream fehlt: {bitstream_id}")
        identity = document_identity(source["handle"])
        if (
            document_id != identity.document_id
            or document["source_id"] != source["source_id"]
            or document["version"] != source["version"]
            or document["bitstream_version"] != source["bitstream_version"]
            or document["publication_date"] != source["publication_date"]
        ):
            raise ManifestGraphError("Dokument widerspricht verknüpfter Source")
        if document["supersedes"] != identity.supersedes:
            raise ManifestGraphError("Dokument-Supersession widerspricht RKI-Version")
        document_type = DocumentType(document["document_type"])
        expected_paths = repository_document_paths(
            document_id=document_id,
            bitstream_id=bitstream_id,
            document_type=document_type,
            publication_date=document["publication_date"],
        )
        paths = document["paths"]
        if paths["pdf"] != expected_paths.pdf or paths["markdown"] not in {
            None,
            expected_paths.markdown,
        }:
            raise ManifestGraphError("Dokumentpfade sind nicht kanonisch")
        published = date.fromisoformat(document["publication_date"])
        iso_year, iso_week, _ = published.isocalendar()
        if document["canonical_periods"] != {
            "week": f"{iso_year:04d}-W{iso_week:02d}",
            "month": f"{published.year:04d}-{published.month:02d}",
            "year": published.year,
        }:
            raise ManifestGraphError("Dokumentperioden sind nicht kanonisch")
        by_document[document_id].append(document)

    missing_documents = set(sources) - {bitstream_id for _, bitstream_id in documents}
    if missing_documents:
        raise ManifestGraphError(f"Source-Dokument fehlt: {sorted(missing_documents)}")

    relations: dict[str, tuple[str | None, str | None]] = {}
    for document_id, variants in by_document.items():
        values = {(item["supersedes"], item["superseded_by"]) for item in variants}
        if len(values) != 1:
            raise ManifestGraphError(f"Dokument-Supersession ist widersprüchlich: {document_id}")
        relations[document_id] = values.pop()
    for document_id, (supersedes, superseded_by) in relations.items():
        if supersedes is not None:
            previous = relations.get(supersedes)
            if previous is None:
                raise ManifestGraphError(f"Supersession-Ziel fehlt: {supersedes}")
            if previous[1] != document_id:
                raise ManifestGraphError("Dokument-Supersession ist nicht reziprok")
        if superseded_by is not None:
            following = relations.get(superseded_by)
            if following is None:
                raise ManifestGraphError(f"Superseded-by-Ziel fehlt: {superseded_by}")
            if following[0] != document_id:
                raise ManifestGraphError("Dokument-Supersession ist nicht reziprok")
    _reject_cycles(
        relations,
        {
            document_id: (() if supersedes is None else (supersedes,))
            for document_id, (supersedes, _superseded_by) in relations.items()
        },
        label="Dokument-Supersession",
    )


def _validate_conversions(
    conversions: dict[str, Manifest],
    documents: dict[tuple[str, str], Manifest],
    sources: dict[str, Manifest],
) -> None:
    persisted_owners: set[tuple[str, str]] = set()
    for conversion in conversions.values():
        owner = (conversion["document_id"], conversion["bitstream_id"])
        if owner not in documents:
            raise ManifestGraphError(f"Conversion-Dokument/Bitstream fehlt: {owner}")
        if conversion["storage_reference"] is not None:
            if owner in persisted_owners:
                raise ManifestGraphError(f"Persistierte aktive Conversion ist doppelt: {owner}")
            persisted_owners.add(owner)
        source = sources[conversion["bitstream_id"]]
        if conversion["source_sha256"] != source["sha256"]:
            raise ManifestGraphError("Conversion-Source-SHA widerspricht Source-SHA")


def _validate_storage(
    storage: dict[str, Manifest],
    sources: dict[str, Manifest],
    documents: dict[tuple[str, str], Manifest],
    conversions: dict[str, Manifest],
) -> None:
    detect_path_collisions(item["relative_path"] for item in storage.values())
    paths: set[str] = set()
    persisted_owners: set[tuple[str, str]] = set()
    for artifact_id, reference in storage.items():
        relative_path = reference["relative_path"]
        if relative_path in paths:
            raise ManifestGraphError(f"Storage-Pfad ist doppelt: {relative_path}")
        paths.add(relative_path)
        conversion_id_value = reference["conversion_id"]
        if conversion_id_value is None:
            matching_documents = [
                (owner, document)
                for owner, document in documents.items()
                if owner[0] == reference["document_id"]
                and document["paths"]["pdf"] == relative_path
            ]
            if len(matching_documents) != 1:
                raise ManifestGraphError("Storage-Dokument für Source fehlt")
            owner, _document = matching_documents[0]
            source = sources[owner[1]]
            expected_sha256 = source["sha256"]
            expected_path = documents[owner]["paths"]["pdf"]
        else:
            conversion = conversions.get(conversion_id_value)
            if conversion is None:
                raise ManifestGraphError(f"Storage-Conversion fehlt: {conversion_id_value}")
            if artifact_id != conversion["storage_reference"]:
                raise ManifestGraphError("Storage-Artefakt-ID widerspricht Conversion-Referenz")
            source = sources[conversion["bitstream_id"]]
            owner = (conversion["document_id"], conversion["bitstream_id"])
            expected_sha256 = conversion["output_sha256"]
            expected_path = documents[owner]["paths"]["markdown"]
            if reference["document_id"] != conversion["document_id"]:
                raise ManifestGraphError("Storage-Dokument widerspricht Conversion")
            persisted_owners.add(owner)
        if reference["relative_path"] != expected_path:
            raise ManifestGraphError("Storage-Pfad widerspricht Dokumentpfad")
        if reference["sha256"] != expected_sha256:
            raise ManifestGraphError("Storage-Artefakt-SHA widerspricht Manifest")
        if (
            reference["source_id"] != source["source_id"]
            or reference["source_sha256"] != source["sha256"]
        ):
            raise ManifestGraphError("Storage-Source-SHA oder Source-ID driftet")
        if (
            reference["decision_sha256"] != source["decision_sha256"]
            or reference["rights_state"] != source["rights"]["state"]
        ):
            raise ManifestGraphError("Storage-Rechteentscheidung driftet")
        if reference["storage_backend"] == "lfs" and reference["storage_object_id"] != (
            f"sha256:{reference['sha256']}"
        ):
            raise ManifestGraphError("LFS-Objekt-SHA entspricht nicht Artefakt-SHA")
        if reference["public_reference"] is not None and reference["visibility"] != "public":
            raise ManifestGraphError("Öffentliche Storage-Referenz benötigt public visibility")
    for conversion_id, conversion in conversions.items():
        storage_reference = conversion["storage_reference"]
        if storage_reference is not None:
            reference = storage.get(storage_reference)
            if reference is None or reference["conversion_id"] != conversion_id:
                raise ManifestGraphError("Conversion-Storage-Referenz löst nicht exakt auf")
    for owner, document in documents.items():
        materialized = owner in persisted_owners
        if (document["paths"]["markdown"] is not None) != materialized:
            raise ManifestGraphError("Document-Markdown-Pfad widerspricht Storagekante")


def build_manifest_graph(
    *,
    sources: Iterable[Manifest],
    documents: Iterable[Manifest],
    conversions: Iterable[Manifest],
    storage_references: Iterable[Manifest],
) -> ManifestGraph:
    """Validate and stably sort one complete current manifest snapshot."""

    source_values = _validated(sources, contract="source-manifest")
    document_values = _validated(documents, contract="document-manifest")
    conversion_values = _validated(conversions, contract="conversion-manifest")
    storage_values = _validated(storage_references, contract="storage-reference")
    source_index = _index(
        source_values,
        key=lambda value: value["bitstream_id"],
        label="Source",
    )
    document_index = _index(
        document_values,
        key=lambda value: (value["document_id"], value["bitstream_id"]),
        label="Dokument",
    )
    conversion_index = _index(
        conversion_values,
        key=lambda value: value["conversion_id"],
        label="Conversion",
    )
    storage_index = _index(
        storage_values,
        key=lambda value: value["artifact_id"],
        label="Storage",
    )
    _validate_sources(source_index)
    _validate_documents(document_index, source_index)
    _validate_conversions(conversion_index, document_index, source_index)
    _validate_storage(storage_index, source_index, document_index, conversion_index)
    return ManifestGraph(
        sources=tuple(source_index[key] for key in sorted(source_index)),
        documents=tuple(document_index[key] for key in sorted(document_index)),
        conversions=tuple(conversion_index[key] for key in sorted(conversion_index)),
        storage_references=tuple(storage_index[key] for key in sorted(storage_index)),
    )
