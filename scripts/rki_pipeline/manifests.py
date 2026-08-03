#!/usr/bin/env python3
"""Schema-first reference graph for current RKI manifest collections."""

from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import stat
from typing import Iterable

from scripts.rki_pipeline.documents import bitstream_identity, document_identity
from scripts.rki_pipeline.io_utils import (
    GENERATED_ROOT_SENTINEL,
    assert_generated_root_fd,
    detect_path_collisions,
    open_directory_beneath,
    open_root_directory,
    stable_json_dumps,
)
from scripts.rki_pipeline.paths import DocumentType, repository_document_paths
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.staging import StagingState, staged_directory

Manifest = dict[str, object]
MAX_MANIFEST_LINE_BYTES = 1_048_576
MAX_MANIFEST_FILE_BYTES = 16_777_216
MAX_CATALOG_BYTES = 65_536

_COLLECTIONS = (
    ("source-manifest", "Quellen/manifest.jsonl", "sources"),
    ("document-manifest", "Dokumente/manifest.jsonl", "documents"),
    ("conversion-manifest", "Konvertierungen/manifest.jsonl", "conversions"),
    ("storage-reference", "Storage/manifest.jsonl", "storage_references"),
)


class ManifestGraphError(ValueError):
    """Manifest records are valid alone but inconsistent as one snapshot."""


class ManifestCatalogError(ValueError):
    """A persisted manifest collection is malformed or no longer canonical."""


@dataclass(frozen=True, slots=True)
class ManifestGraph:
    """Stable, validated collections ready for canonical rendering."""

    sources: tuple[Manifest, ...]
    documents: tuple[Manifest, ...]
    conversions: tuple[Manifest, ...]
    storage_references: tuple[Manifest, ...]


@dataclass(frozen=True, slots=True)
class RenderedManifestCatalog:
    """Canonical bytes for one complete manifest snapshot."""

    files: tuple[tuple[str, bytes], ...]
    catalog_sha256: str


@dataclass(frozen=True, slots=True)
class LoadedManifestCatalog:
    """Strictly loaded graph and its canonical bytes."""

    graph: ManifestGraph
    rendered: RenderedManifestCatalog


@dataclass(frozen=True, slots=True)
class ManifestCatalogResult:
    """One materialized manifest root."""

    root: Path
    rendered: RenderedManifestCatalog
    changed: bool


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
        if source["source_id"] != identity.source_id or source["version"] != identity.version:
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


def _render_jsonl(values: tuple[Manifest, ...]) -> bytes:
    return b"".join(stable_json_dumps(value, indent=None).encode("utf-8") for value in values)


def render_manifest_catalog(graph: ManifestGraph) -> RenderedManifestCatalog:
    """Render stable JSONL collections and their canonical catalog."""

    files: list[tuple[str, bytes]] = []
    descriptors: list[dict[str, object]] = []
    for kind, relative, attribute in _COLLECTIONS:
        values = getattr(graph, attribute)
        payload = _render_jsonl(values)
        files.append((relative, payload))
        descriptors.append(
            {
                "bytes": len(payload),
                "count": len(values),
                "kind": kind,
                "path": relative,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    catalog_bytes = stable_json_dumps(
        {
            "collections": descriptors,
            "schema_version": "1.0.0",
        }
    ).encode("utf-8")
    files.append(("catalog.json", catalog_bytes))
    return RenderedManifestCatalog(
        files=tuple(files),
        catalog_sha256=hashlib.sha256(catalog_bytes).hexdigest(),
    )


def _read_regular_at(
    parent_fd: int,
    name: str,
    *,
    maximum: int,
    label: str,
) -> bytes:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ManifestCatalogError(f"Manifestdatei fehlt: {label}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ManifestCatalogError(f"Symlink im Manifestkatalog: {label}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ManifestCatalogError(f"Manifestpfad ist keine reguläre Datei: {label}")
    if metadata.st_size > maximum:
        raise ManifestCatalogError(f"Manifestdatei überschreitet Größenlimit: {label}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
        ):
            raise ManifestCatalogError(f"Manifestdatei änderte sich beim Lesen: {label}")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(1_048_576, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) > maximum:
            raise ManifestCatalogError(f"Manifestdatei überschreitet Größenlimit: {label}")
        completed = os.fstat(descriptor)
        if (
            len(payload) != opened.st_size
            or completed.st_size != opened.st_size
            or completed.st_mtime_ns != opened.st_mtime_ns
            or completed.st_ctime_ns != opened.st_ctime_ns
        ):
            raise ManifestCatalogError(f"Manifestdatei änderte sich beim Lesen: {label}")
        return payload
    finally:
        os.close(descriptor)


def _reject_symlinks(parent_fd: int, names: set[str], *, label: str) -> None:
    for name in names:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ManifestCatalogError(f"Symlink im Manifestkatalog: {label}/{name}")


def _read_catalog_files_fd(root_fd: int) -> dict[str, bytes]:
    expected_directories = {relative.split("/", 1)[0] for _, relative, _ in _COLLECTIONS}
    expected_root = expected_directories | {"catalog.json"}
    allowed_root = expected_root | {GENERATED_ROOT_SENTINEL}
    root_names = set(os.listdir(root_fd))
    _reject_symlinks(root_fd, root_names, label=".")
    if GENERATED_ROOT_SENTINEL in root_names:
        assert_generated_root_fd(root_fd)
    extras = sorted(root_names - allowed_root)
    missing = sorted(expected_root - root_names)
    if extras or missing:
        raise ManifestCatalogError(
            f"Manifestkatalog enthält unregistrierte oder fehlende Pfade: "
            f"extras={extras}; missing={missing}"
        )
    result = {
        "catalog.json": _read_regular_at(
            root_fd,
            "catalog.json",
            maximum=MAX_CATALOG_BYTES,
            label="catalog.json",
        )
    }
    for _kind, relative, _attribute in _COLLECTIONS:
        directory_name, file_name = relative.split("/", 1)
        directory_fd = open_directory_beneath(root_fd, (directory_name,))
        try:
            names = set(os.listdir(directory_fd))
            _reject_symlinks(directory_fd, names, label=directory_name)
            if names != {file_name}:
                raise ManifestCatalogError(
                    f"Manifestkatalog enthält unregistrierte Pfade in "
                    f"{directory_name}: {sorted(names - {file_name})}"
                )
            result[relative] = _read_regular_at(
                directory_fd,
                file_name,
                maximum=MAX_MANIFEST_FILE_BYTES,
                label=relative,
            )
        finally:
            os.close(directory_fd)
    return result


def _read_catalog_files(root: Path) -> dict[str, bytes]:
    try:
        with open_root_directory(root) as root_fd:
            return _read_catalog_files_fd(root_fd)
    except ManifestCatalogError:
        raise
    except (OSError, ValueError) as exc:
        raise ManifestCatalogError(
            f"Manifestkatalog kann nicht sicher gelesen werden: {exc}"
        ) from exc


def _strict_json(payload: bytes, *, label: str) -> object:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise ManifestCatalogError(f"JSON-BOM ist unzulässig: {label}")

    def object_pairs(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ManifestCatalogError(f"Doppelter JSON-Schlüssel in {label}: {key}")
            value[key] = item
        return value

    def reject_constant(value):
        raise ManifestCatalogError(f"Nichtendlicher JSON-Wert in {label}: {value}")

    try:
        return json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except ManifestCatalogError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestCatalogError(f"Ungültiges JSON in {label}: {exc}") from exc


def _load_jsonl(payload: bytes, *, label: str) -> tuple[Manifest, ...]:
    if not payload:
        return ()
    if not payload.endswith(b"\n"):
        raise ManifestCatalogError(f"JSONL endet nicht mit Newline: {label}")
    lines = payload.split(b"\n")[:-1]
    if any(not line for line in lines):
        raise ManifestCatalogError(f"Leerzeile im JSONL-Manifest: {label}")
    values: list[Manifest] = []
    for number, line in enumerate(lines, 1):
        if len(line) > MAX_MANIFEST_LINE_BYTES:
            raise ManifestCatalogError(f"JSONL-Zeilenlimit überschritten: {label}:{number}")
        value = _strict_json(line, label=f"{label}:{number}")
        if type(value) is not dict:
            raise ManifestCatalogError(f"JSONL-Eintrag ist kein Objekt: {label}:{number}")
        if stable_json_dumps(value, indent=None).encode("utf-8") != line + b"\n":
            raise ManifestCatalogError(f"JSONL ist nicht kanonisch: {label}:{number}")
        values.append(value)
    return tuple(values)


def _validate_catalog(catalog_bytes: bytes, files: dict[str, bytes]) -> None:
    catalog = _strict_json(catalog_bytes, label="catalog.json")
    if type(catalog) is not dict or set(catalog) != {"collections", "schema_version"}:
        raise ManifestCatalogError("Katalogvertrag ist ungültig")
    if catalog["schema_version"] != "1.0.0":
        raise ManifestCatalogError("Katalogversion ist unbekannt")
    if stable_json_dumps(catalog).encode("utf-8") != catalog_bytes:
        raise ManifestCatalogError("Katalog-JSON ist nicht kanonisch")
    descriptors = catalog["collections"]
    if type(descriptors) is not list or len(descriptors) != len(_COLLECTIONS):
        raise ManifestCatalogError("Katalogsammlungen sind unvollständig")
    for descriptor, (kind, relative, _attribute) in zip(descriptors, _COLLECTIONS, strict=True):
        if type(descriptor) is not dict or set(descriptor) != {
            "bytes",
            "count",
            "kind",
            "path",
            "sha256",
        }:
            raise ManifestCatalogError("Katalogdeskriptor ist ungültig")
        if (
            type(descriptor["bytes"]) is not int
            or type(descriptor["count"]) is not int
            or type(descriptor["kind"]) is not str
            or type(descriptor["path"]) is not str
            or type(descriptor["sha256"]) is not str
        ):
            raise ManifestCatalogError("Katalogdeskriptor enthält ungültige Typen")
        payload = files[relative]
        expected = {
            "bytes": len(payload),
            "count": len(payload.splitlines()),
            "kind": kind,
            "path": relative,
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        if descriptor != expected:
            raise ManifestCatalogError(f"Katalogdeskriptor driftet: {relative}")


def _load_catalog_files(files: dict[str, bytes]) -> LoadedManifestCatalog:
    collections = {
        attribute: _load_jsonl(files[relative], label=relative)
        for _kind, relative, attribute in _COLLECTIONS
    }
    _validate_catalog(files["catalog.json"], files)
    graph = build_manifest_graph(
        sources=collections["sources"],
        documents=collections["documents"],
        conversions=collections["conversions"],
        storage_references=collections["storage_references"],
    )
    rendered = render_manifest_catalog(graph)
    if dict(rendered.files) != files:
        raise ManifestCatalogError("Manifestgraph oder stabile Reihenfolge driftet")
    return LoadedManifestCatalog(graph=graph, rendered=rendered)


def load_manifest_catalog(root: Path) -> LoadedManifestCatalog:
    """Load one fixed-layout manifest root without following symlinks."""

    return _load_catalog_files(_read_catalog_files(Path(root)))


def _write_stage_file(stage: Path, relative: str, payload: bytes) -> None:
    expected = {path for _kind, path, _attribute in _COLLECTIONS} | {"catalog.json"}
    if relative not in expected:
        raise ManifestCatalogError(f"Unregistrierter Stagingpfad: {relative}")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(stage / relative, flags, 0o644)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Manifest-Staging-Write machte keinen Fortschritt")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_stage(stage: Path) -> None:
    directories = [stage / relative.split("/", 1)[0] for _, relative, _ in _COLLECTIONS]
    for path in (*directories, stage):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def materialize_manifest_catalog(
    graph: ManifestGraph,
    *,
    temp_root: Path,
    ledger: EffectLedger,
) -> ManifestCatalogResult:
    """Atomically replace one generated manifest snapshot below ``temp_root``."""

    root = Path(temp_root).resolve()
    if ledger.mode is not RunMode.MATERIALIZE or ledger.temp_root != root:
        raise ValueError("Manifest-Materialisierung benötigt passenden materialize ledger/root")
    rendered = render_manifest_catalog(graph)
    target = root / "rki" / "Bulletins" / "Manifeste"
    if target.exists():
        loaded = load_manifest_catalog(target)
        if loaded.rendered == rendered:
            return ManifestCatalogResult(root=target, rendered=rendered, changed=False)

    event_count = len(ledger.events)
    staging_state = StagingState()
    try:
        for relative, payload in rendered.files:
            ledger.record(
                EffectKind.TEMP_FILE,
                (target / relative).as_posix(),
                sha256=hashlib.sha256(payload).hexdigest(),
                size=len(payload),
            )
        with staged_directory(
            target,
            allowed_root=root,
            replace_existing=True,
            state=staging_state,
        ) as stage:
            for directory in sorted(
                {relative.split("/", 1)[0] for _kind, relative, _attribute in _COLLECTIONS}
            ):
                os.mkdir(stage / directory, mode=0o755)
            for relative, payload in rendered.files:
                _write_stage_file(stage, relative, payload)
            _fsync_stage(stage)
            stage_fd = os.open(stage, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                loaded = _load_catalog_files(_read_catalog_files_fd(stage_fd))
            finally:
                os.close(stage_fd)
            if loaded.rendered != rendered:
                raise ManifestCatalogError("Staging-Manifest driftet vor Veröffentlichung")
    except BaseException:
        if not staging_state.published:
            del ledger.events[event_count:]
        raise
    return ManifestCatalogResult(root=target, rendered=rendered, changed=True)
