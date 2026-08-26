"""Deterministic, atomic generated Markdown documentation builds."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
import hashlib
import html
import json
import os
from pathlib import Path, PurePosixPath
import stat
import sys
from types import MappingProxyType

import yaml

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
    atomic_write_bytes,
    atomic_write_text,
    ensure_within,
    normalize_posix_path,
    open_directory_beneath,
    open_root_directory,
    read_bounded_utf8_text_beneath,
    relative_path_beneath,
    sha256_file,
)
from scripts.rki_pipeline.documents import bitstream_identity, document_identity
from scripts.rki_pipeline.manifests import (
    LoadedManifestCatalog,
    ManifestCatalogError,
    ManifestGraph,
    ManifestGraphError,
    _index,
    _load_jsonl,
    _read_catalog_files_fd,
    _validate_catalog,
    _validate_conversions,
    _validate_documents,
    _validate_storage,
    _validated,
    render_manifest_catalog,
)
from scripts.rki_pipeline.rights import (
    MAX_POLICY_BYTES,
    MAX_REGISTER_BYTES,
    ApprovalKey,
    ComponentsState,
    PublicationMode,
    RightsAction,
    RightsAttribution,
    RightsDecision,
    RightsPolicy,
    RightsPolicyError,
    RightsRegister,
    RightsState,
    decision_sha256,
    is_monotone_restriction,
    parse_rights_policy,
    parse_rights_register,
)
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.staging import StagingError, preview_directory, staged_directory
from scripts.web.callouts import convert_obsidian_callouts_for_web
from scripts.web.content_index import ContentIndex, build_content_index
from scripts.web.content_model import ContentPage
from scripts.web.link_converters import convert_for_web
from scripts.web.build_tables import (
    load_table_inputs,
    render_table_page,
    write_table_data_assets,
)
from scripts.web.build_downloads import (
    DownloadInputs,
    DownloadProjectionError,
    DownloadTombstone,
    project_downloads,
    render_download_page,
)


_CONFIG_PATH = PurePosixPath("config/publication.yaml")
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "content_root",
        "build_root",
        "docs_dir",
        "site_dir",
        "generated_sentinel",
    }
)
_PATH_KEYS = ("content_root", "build_root", "docs_dir", "site_dir")
_RESERVED_SOURCE_ROOTS = (PurePosixPath("config"), PurePosixPath("web"))
_GENERATED_SENTINEL = ".desinfect-generated"
_TABLE_PAGE = PurePosixPath("Tabelle.md")
_DOWNLOADS_PAGE = PurePosixPath("Downloads.md")
_RAW_TABLE_PROJECTIONS = frozenset(
    {
        PurePosixPath("generated-data/corpus-table.json"),
        PurePosixPath("generated-data/anleitungen.json"),
    }
)
_EXACT_PUBLICATION_INPUTS = (
    (PurePosixPath("research/corpus-readiness.json"), False),
    (PurePosixPath("research/taxonomy.yml"), False),
)
_BOUNDED_PUBLICATION_INPUTS = MappingProxyType(
    {
        PurePosixPath("status.json"): 256 * 1024,
        PurePosixPath("research/rights-register.yml"): MAX_REGISTER_BYTES,
        PurePosixPath("config/rights-policy.toml"): MAX_POLICY_BYTES,
    }
)
_MANIFEST_ROOT = PurePosixPath("rki/Bulletins/Manifeste")
_DOWNLOAD_MANIFEST_PATHS = (
    PurePosixPath("Quellen/manifest.jsonl"),
    PurePosixPath("Dokumente/manifest.jsonl"),
    PurePosixPath("Konvertierungen/manifest.jsonl"),
    PurePosixPath("Storage/manifest.jsonl"),
    PurePosixPath("catalog.json"),
)


class BuildDocsError(ValueError):
    """Docs source, configuration, or generated-output validation failed."""


@dataclass(frozen=True, slots=True)
class PublicationConfig:
    content_root: Path
    build_root: Path
    docs_dir: Path
    site_dir: Path
    generated_sentinel: str


@dataclass(frozen=True, slots=True)
class DocsBuildResult:
    source_hashes: Mapping[str, str]
    docs_hashes: Mapping[str, str]
    published: bool
    excluded_docs: tuple[PurePosixPath, ...] = ()
    content_index: ContentIndex | None = None


@dataclass(frozen=True, slots=True)
class DocsPreview:
    docs_dir: Path
    result: DocsBuildResult


@dataclass(frozen=True, slots=True)
class CapturedPublicationInputs:
    """One descriptor-bound byte capture of every exact publication authority."""

    status_bytes: bytes
    rights_register_bytes: bytes
    rights_policy_bytes: bytes
    manifest_files: tuple[tuple[str, bytes], ...]
    hashes: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class BoundPublicationSources:
    """Immutable build model shared by docs and site phases."""

    source_hashes: Mapping[str, str]
    content_index: ContentIndex
    download_inputs: DownloadInputs


def _repo_root(repo_root: Path) -> Path:
    try:
        return repo_root.resolve(strict=True)
    except OSError as exc:
        raise BuildDocsError(f"invalid repository root: {repo_root}") from exc


def _config_relative_path(value: object, *, name: str) -> PurePosixPath:
    if not isinstance(value, str):
        raise BuildDocsError(f"publication config {name} must be a string")
    try:
        normalized = normalize_posix_path(value)
    except UnsafePathError as exc:
        raise BuildDocsError(f"unsafe publication config {name}: {value!r}") from exc
    if normalized != value:
        raise BuildDocsError(f"non-canonical publication config {name}: {value!r}")
    return PurePosixPath(normalized)


def _path_beneath_repo(repo_root: Path, relative: PurePosixPath) -> Path:
    candidate = repo_root / relative
    try:
        relative_path_beneath(candidate, repo_root)
        return ensure_within(candidate, repo_root, allow_missing_parents=True)
    except (OSError, UnsafePathError) as exc:
        raise BuildDocsError(f"unsafe publication path: {relative}") from exc


def _paths_overlap(left: PurePosixPath, right: PurePosixPath) -> bool:
    """Return whether either canonical path is an ancestor of the other."""

    return (
        left.parts[: len(right.parts)] == right.parts
        or right.parts[: len(left.parts)] == left.parts
    )


def _is_strictly_beneath(path: PurePosixPath, root: PurePosixPath) -> bool:
    """Return whether *path* is a strict descendant of *root*."""

    return path != root and path.parts[: len(root.parts)] == root.parts


def _reject_duplicate_yaml_keys(node: yaml.nodes.Node | None) -> None:
    if isinstance(node, yaml.nodes.MappingNode):
        seen: set[tuple[str, str]] = set()
        for key, value in node.value:
            if isinstance(key, yaml.nodes.ScalarNode):
                if key.tag == "tag:yaml.org,2002:merge":
                    raise BuildDocsError("YAML merge keys are forbidden in publication config")
                identifier = (key.tag, key.value)
                if identifier in seen:
                    raise BuildDocsError("duplicate YAML key in publication config")
                seen.add(identifier)
            _reject_duplicate_yaml_keys(key)
            _reject_duplicate_yaml_keys(value)
    elif isinstance(node, yaml.nodes.SequenceNode):
        for value in node.value:
            _reject_duplicate_yaml_keys(value)


def _load_publication_yaml(config_path: Path) -> object:
    try:
        document = config_path.read_text(encoding="utf-8")
        _reject_duplicate_yaml_keys(yaml.compose(document, Loader=yaml.SafeLoader))
        return yaml.safe_load(document)
    except BuildDocsError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise BuildDocsError(f"invalid publication config: {config_path}") from exc


def load_publication_config(repo_root: Path) -> PublicationConfig:
    """Load exactly one safe publication configuration from *repo_root*."""

    root = _repo_root(repo_root)
    config_path = _path_beneath_repo(root, _CONFIG_PATH)
    if config_path.is_symlink():
        raise BuildDocsError("publication config must not be a symlink")
    parsed = _load_publication_yaml(config_path)
    if not isinstance(parsed, dict) or set(parsed) != _CONFIG_KEYS:
        raise BuildDocsError("publication config must contain exactly the required keys")
    if type(parsed["schema_version"]) is not int or parsed["schema_version"] != 1:
        raise BuildDocsError("publication config schema_version must be integer 1")
    paths = {name: _config_relative_path(parsed[name], name=name) for name in _PATH_KEYS}
    if not _is_strictly_beneath(paths["docs_dir"], paths["build_root"]):
        raise BuildDocsError("publication config docs_dir must be strictly beneath build_root")
    if _paths_overlap(paths["content_root"], paths["build_root"]):
        raise BuildDocsError("publication config content_root overlaps build_root")
    if _paths_overlap(paths["site_dir"], paths["build_root"]):
        raise BuildDocsError("publication config site_dir overlaps build_root")
    if _paths_overlap(paths["site_dir"], paths["content_root"]):
        raise BuildDocsError("publication config site_dir overlaps content_root")
    for name in ("content_root", "build_root", "site_dir"):
        for source_root in _RESERVED_SOURCE_ROOTS:
            if _paths_overlap(paths[name], source_root):
                raise BuildDocsError(
                    f"publication config {name} overlaps reserved source root {source_root}"
                )
    sentinel = parsed["generated_sentinel"]
    if sentinel != _GENERATED_SENTINEL:
        raise BuildDocsError(f"generated_sentinel must be {_GENERATED_SENTINEL}")
    return PublicationConfig(
        content_root=_path_beneath_repo(root, paths["content_root"]),
        build_root=_path_beneath_repo(root, paths["build_root"]),
        docs_dir=_path_beneath_repo(root, paths["docs_dir"]),
        site_dir=_path_beneath_repo(root, paths["site_dir"]),
        generated_sentinel=sentinel,
    )


def _regular_files(
    root: Path,
    *,
    required: bool,
    allow_root_symlink: bool = False,
) -> Iterator[tuple[PurePosixPath, Path]]:
    if not root.exists():
        if required:
            raise BuildDocsError(f"required source root is missing: {root}")
        return
    if root.is_symlink() and not allow_root_symlink:
        raise BuildDocsError(f"symlinked source root is forbidden: {root}")
    if not root.is_dir():
        raise BuildDocsError(f"source root is not a directory: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise BuildDocsError(f"cannot inspect source entry: {path}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise BuildDocsError(f"symlinked source entry is forbidden: {path}")
        if stat.S_ISREG(metadata.st_mode):
            yield PurePosixPath(path.relative_to(root).as_posix()), path


def _source_roots(repo_root: Path) -> tuple[tuple[PurePosixPath, Path, bool], ...]:
    config = load_publication_config(repo_root)
    return (
        (PurePosixPath("content"), config.content_root, True),
        (PurePosixPath("config"), repo_root / "config", True),
        (PurePosixPath("mkdocs.yml"), repo_root / "mkdocs.yml", False),
        (PurePosixPath("web"), repo_root / "web", False),
    )


def _manifest_capture(repo_root: Path) -> tuple[tuple[str, bytes], ...]:
    manifest_fd: int | None = None
    try:
        with open_root_directory(repo_root) as repo_fd:
            current_fd = os.dup(repo_fd)
            try:
                for component in _MANIFEST_ROOT.parts:
                    try:
                        metadata = os.stat(
                            component,
                            dir_fd=current_fd,
                            follow_symlinks=False,
                        )
                    except FileNotFoundError:
                        return ()
                    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                        raise BuildDocsError("Manifestroot muss komponentenweise unverlinkt sein")
                    next_fd = open_directory_beneath(current_fd, (component,))
                    os.close(current_fd)
                    current_fd = next_fd
                manifest_fd = current_fd
                current_fd = -1
                files = _read_catalog_files_fd(manifest_fd)
            finally:
                if current_fd >= 0:
                    os.close(current_fd)
    except BuildDocsError:
        raise
    except (ManifestCatalogError, OSError, UnsafePathError) as exc:
        raise BuildDocsError(f"Manifest-Capture fehlgeschlagen: {exc}") from exc
    finally:
        if manifest_fd is not None:
            os.close(manifest_fd)
    if set(files) != {path.as_posix() for path in _DOWNLOAD_MANIFEST_PATHS}:
        raise BuildDocsError("Manifest-Capture besitzt nicht exakt die kanonischen Pfade")
    return tuple((path.as_posix(), files[path.as_posix()]) for path in _DOWNLOAD_MANIFEST_PATHS)


def capture_publication_inputs(repo_root: Path) -> CapturedPublicationInputs:
    """Read every exact publication input once through bounded no-follow descriptors."""

    root = _repo_root(repo_root)
    payloads: dict[PurePosixPath, bytes] = {}
    try:
        for relative, maximum in _BOUNDED_PUBLICATION_INPUTS.items():
            text = read_bounded_utf8_text_beneath(root, relative, max_bytes=maximum)
            payloads[relative] = text.encode("utf-8")
        manifest_files = _manifest_capture(root)
    except BuildDocsError:
        raise
    except (OSError, UnsafePathError, UnicodeDecodeError, ValueError) as exc:
        raise BuildDocsError(f"gebundener Publikationsinput ist ungültig: {exc}") from exc
    hashes = {
        relative.as_posix(): hashlib.sha256(payload).hexdigest()
        for relative, payload in payloads.items()
    }
    hashes.update(
        {
            (_MANIFEST_ROOT / relative).as_posix(): hashlib.sha256(payload).hexdigest()
            for relative, payload in manifest_files
        }
    )
    return CapturedPublicationInputs(
        status_bytes=payloads[PurePosixPath("status.json")],
        rights_register_bytes=payloads[PurePosixPath("research/rights-register.yml")],
        rights_policy_bytes=payloads[PurePosixPath("config/rights-policy.toml")],
        manifest_files=manifest_files,
        hashes=MappingProxyType(dict(sorted(hashes.items()))),
    )


def snapshot_sources(repo_root: Path, *, captured: CapturedPublicationInputs) -> Mapping[str, str]:
    """Hash all publication inputs without changing source bytes."""

    root = _repo_root(repo_root)
    if type(captured) is not CapturedPublicationInputs:
        raise BuildDocsError("Publikationssnapshot benötigt exakten Capturewert")
    hashes: dict[str, str] = dict(captured.hashes)
    for relative, required in _EXACT_PUBLICATION_INPUTS:
        path = root / relative
        try:
            safe_path = ensure_within(path, root, allow_missing_parents=not required)
            metadata = safe_path.lstat()
        except FileNotFoundError:
            if required:
                raise BuildDocsError(f"required source file is missing: {relative}") from None
            continue
        except (OSError, UnsafePathError) as exc:
            raise BuildDocsError(f"cannot inspect source file: {relative}") from exc
        if not stat.S_ISREG(metadata.st_mode):
            raise BuildDocsError(f"source file must be regular: {relative}")
        hashes[relative.as_posix()] = sha256_file(safe_path)
    for prefix, source, required in _source_roots(root):
        if prefix == PurePosixPath("mkdocs.yml"):
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file():
                raise BuildDocsError(f"mkdocs source must be a regular file: {source}")
            hashes[prefix.as_posix()] = sha256_file(source)
            continue
        for relative, path in _regular_files(source, required=required):
            repository_relative = prefix / relative
            if repository_relative in _BOUNDED_PUBLICATION_INPUTS:
                continue
            hashes[repository_relative.as_posix()] = sha256_file(path)
    return MappingProxyType(dict(sorted(hashes.items())))


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def freeze_download_inputs(
    inputs: DownloadInputs, input_hashes: Mapping[str, str]
) -> DownloadInputs:
    """Return projection with recursively immutable status and hash mappings."""

    if type(inputs) is not DownloadInputs:
        raise BuildDocsError("Downloadprojektion besitzt keinen exakten Typ")
    frozen_status = _freeze(inputs.public_status)
    frozen_hashes = _freeze(input_hashes)
    assert isinstance(frozen_status, Mapping)
    assert isinstance(frozen_hashes, Mapping)
    return replace(inputs, public_status=frozen_status, input_hashes=frozen_hashes)


def _strict_status(payload: bytes) -> Mapping[str, object]:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise BuildDocsError(f"status.json enthält doppelten Schlüssel: {key}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise BuildDocsError(f"status.json enthält nichtendlichen Wert: {value}")

    try:
        parsed = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
        validate_document("status", parsed)
    except BuildDocsError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise BuildDocsError(f"status.json ist ungültig: {exc}") from exc
    if type(parsed) is not dict:
        raise BuildDocsError("status.json muss ein exaktes Objekt sein")
    frozen = _freeze(parsed)
    assert isinstance(frozen, Mapping)
    return frozen


def _historical_decision(source: Mapping[str, object]) -> RightsDecision:
    rights = source.get("rights")
    if type(rights) is not dict or type(rights.get("approval_key")) is not dict:
        raise ManifestCatalogError("Source enthält keine eingebettete Rechteentscheidung")
    approval = rights["approval_key"]
    attribution_value = rights.get("attribution")
    attribution = None
    if attribution_value is not None:
        if type(attribution_value) is not dict:
            raise ManifestCatalogError("eingebettete Attribution ist ungültig")
        attribution = RightsAttribution(
            creators=tuple(attribution_value["creators"]),
            attribution_parties=tuple(attribution_value["attribution_parties"]),
            copyright_notice=attribution_value["copyright_notice"],
            license_notice=attribution_value["license_notice"],
            license_url=attribution_value["license_url"],
            disclaimer_notice=attribution_value["disclaimer_notice"],
            origin_url=attribution_value["origin_url"],
            prior_change_history=tuple(attribution_value["prior_change_history"]),
            current_change_notice=attribution_value["current_change_notice"],
        )
    try:
        draft = RightsDecision(
            approval_key=ApprovalKey(
                approval["source_id"],
                approval["canonical_url"],
                approval["version_or_bitstream"],
                approval["source_sha256"],
            ),
            state=RightsState(rights["state"]),
            mode=PublicationMode(rights["mode"]),
            allowed_actions=tuple(RightsAction(action) for action in rights["allowed_actions"]),
            components_state=ComponentsState(rights["components_state"]),
            attribution=attribution,
            basis=rights["basis"],
            reviewed_by=rights["reviewed_by"],
            reviewed_at=rights["reviewed_at"],
            decision_sha256=None,
        )
        decision = replace(draft, decision_sha256=decision_sha256(draft))
    except (KeyError, TypeError, ValueError, RightsPolicyError) as exc:
        raise ManifestCatalogError("eingebettete Rechteentscheidung ist ungültig") from exc
    if source.get("decision_sha256") != decision.decision_sha256:
        raise ManifestCatalogError("eingebetteter Entscheidungshash driftet")
    return decision


def _validate_source_structure(sources: dict[str, dict[str, object]]) -> None:
    by_sha256: dict[str, list[str]] = defaultdict(list)
    for bitstream_id, source in sources.items():
        identity = document_identity(source["handle"])
        bitstream = bitstream_identity(source["bitstream_url"])
        if (
            source["source_id"] != identity.source_id
            or source["version"] != identity.version
            or bitstream_id != bitstream.bitstream_id
            or source["bitstream_version"] != bitstream.version
            or source["bitstream_url"] != bitstream.canonical_url
            or not bitstream.canonical_url.startswith(
                f"https://edoc.rki.de/bitstream/handle/{identity.handle}/"
            )
            or source["source_url"] != f"https://edoc.rki.de/handle/{identity.handle}"
        ):
            raise ManifestGraphError("Source-Identität oder Bitstream ist nicht kanonisch")
        decision = _historical_decision(source)
        if decision.approval_key != ApprovalKey(
            source["source_id"], source["bitstream_url"], bitstream_id, source["sha256"]
        ):
            raise ManifestGraphError("Source-ApprovalKey widerspricht Source")
        by_sha256[source["sha256"]].append(bitstream_id)
    for ids in by_sha256.values():
        ordered = sorted(ids)
        canonical = ordered[0]
        for bitstream_id in ordered:
            expected = [] if bitstream_id == canonical else [canonical]
            if sources[bitstream_id]["same_content_as"] != expected:
                raise ManifestGraphError("Source-Alias ist nicht kanonisch")


def _structural_manifest_graph(files: dict[str, bytes]) -> ManifestGraph:
    collections = {
        "sources": _load_jsonl(files["Quellen/manifest.jsonl"], label="Quellen/manifest.jsonl"),
        "documents": _load_jsonl(
            files["Dokumente/manifest.jsonl"], label="Dokumente/manifest.jsonl"
        ),
        "conversions": _load_jsonl(
            files["Konvertierungen/manifest.jsonl"], label="Konvertierungen/manifest.jsonl"
        ),
        "storage": _load_jsonl(files["Storage/manifest.jsonl"], label="Storage/manifest.jsonl"),
    }
    _validate_catalog(files["catalog.json"], files)
    source_values = _validated(collections["sources"], contract="source-manifest")
    document_values = _validated(collections["documents"], contract="document-manifest")
    conversion_values = _validated(collections["conversions"], contract="conversion-manifest")
    storage_values = _validated(collections["storage"], contract="storage-reference")
    sources = _index(source_values, key=lambda value: value["bitstream_id"], label="Source")
    documents = _index(
        document_values,
        key=lambda value: (value["document_id"], value["bitstream_id"]),
        label="Dokument",
    )
    conversions = _index(
        conversion_values, key=lambda value: value["conversion_id"], label="Conversion"
    )
    storage = _index(storage_values, key=lambda value: value["artifact_id"], label="Storage")
    _validate_source_structure(sources)
    _validate_documents(documents, sources)
    _validate_conversions(conversions, documents, sources)
    _validate_storage(storage, sources, documents, conversions, None)  # type: ignore[arg-type]
    return ManifestGraph(
        sources=tuple(sources[key] for key in sorted(sources)),
        documents=tuple(documents[key] for key in sorted(documents)),
        conversions=tuple(conversions[key] for key in sorted(conversions)),
        storage_references=tuple(storage[key] for key in sorted(storage)),
    )


def _attribution_only_revision(previous: RightsDecision, current: RightsDecision) -> bool:
    if (
        previous.approval_key != current.approval_key
        or previous.mode != current.mode
        or previous.state != current.state
        or previous.components_state != current.components_state
        or previous.allowed_actions != current.allowed_actions
        or previous.basis != current.basis
        or previous.attribution is None
        or current.attribution is None
        or previous.attribution == current.attribution
    ):
        return False
    return current.attribution.prior_change_history == (
        *previous.attribution.prior_change_history,
        previous.attribution.current_change_notice,
    )


def _quarantine_decision(current: RightsDecision) -> RightsDecision:
    draft = RightsDecision(
        approval_key=current.approval_key,
        state=RightsState.METADATA_ONLY,
        mode=PublicationMode.ORIGIN_LINK,
        allowed_actions=(),
        components_state=ComponentsState.UNKNOWN,
        attribution=None,
        basis="attribution_revision_requires_manifest_refresh",
        reviewed_by=None,
        reviewed_at=None,
        decision_sha256=None,
    )
    return replace(draft, decision_sha256=decision_sha256(draft))


def _register_with(
    register: RightsRegister, replacements: Mapping[ApprovalKey, RightsDecision]
) -> RightsRegister:
    entries = tuple(
        sorted(
            (replacements.get(entry.approval_key, entry) for entry in register.entries),
            key=lambda entry: (
                entry.approval_key.source_id,
                entry.approval_key.canonical_url,
                entry.approval_key.version_or_bitstream,
                entry.approval_key.source_sha256,
            ),
        )
    )
    return RightsRegister(schema_version=2, entries=entries)


def _without_sources(graph: ManifestGraph, bitstreams: set[str]) -> ManifestGraph:
    documents = tuple(
        document for document in graph.documents if document["bitstream_id"] not in bitstreams
    )
    document_ids = {document["document_id"] for document in documents}
    conversions = tuple(
        conversion
        for conversion in graph.conversions
        if conversion["bitstream_id"] not in bitstreams
    )
    conversion_ids = {conversion["conversion_id"] for conversion in conversions}
    return ManifestGraph(
        sources=tuple(
            source for source in graph.sources if source["bitstream_id"] not in bitstreams
        ),
        documents=documents,
        conversions=conversions,
        storage_references=tuple(
            reference
            for reference in graph.storage_references
            if reference["document_id"] in document_ids
            and (reference["conversion_id"] is None or reference["conversion_id"] in conversion_ids)
        ),
    )


def load_publication_manifest_catalog(
    captured: CapturedPublicationInputs,
    *,
    register: RightsRegister,
    policy: RightsPolicy,
) -> tuple[
    LoadedManifestCatalog | None,
    ManifestGraph,
    RightsRegister,
    tuple[DownloadTombstone, ...],
]:
    """Load captured manifests with web-only fail-closed rights drift handling."""

    if not captured.manifest_files:
        empty = ManifestGraph((), (), (), ())
        return None, empty, register, ()
    files = dict(captured.manifest_files)
    try:
        graph = _structural_manifest_graph(files)
        rendered = render_manifest_catalog(graph)
        if dict(rendered.files) != files:
            raise ManifestCatalogError("Manifestgraph oder stabile Reihenfolge driftet")
        loaded = LoadedManifestCatalog(graph=graph, rendered=rendered)
        current_by_key = {entry.approval_key: entry for entry in register.entries}
        quarantines: dict[ApprovalKey, RightsDecision] = {}
        missing_bitstreams: set[str] = set()
        for source in graph.sources:
            historical = _historical_decision(source)
            current = current_by_key.get(historical.approval_key)
            if current is not None and current.decision_sha256 == historical.decision_sha256:
                continue
            if current is None:
                missing_bitstreams.add(source["bitstream_id"])
                continue
            if is_monotone_restriction(historical, current, policy=policy, visibility="public"):
                continue
            if _attribution_only_revision(historical, current):
                quarantines[current.approval_key] = _quarantine_decision(current)
                continue
            raise ManifestCatalogError("aktuelle Rechteentscheidung driftet lateral oder permissiv")
        effective_graph = _without_sources(graph, missing_bitstreams)
        effective_register = _register_with(register, quarantines)
        effective_by_key = {entry.approval_key: entry for entry in effective_register.entries}
        nonmaterialized_bitstreams = {
            source["bitstream_id"]
            for source in effective_graph.sources
            if effective_by_key[_historical_decision(source).approval_key].mode
            is not PublicationMode.MATERIALIZED
        }
        tombstone_document_ids = {
            document["document_id"]
            for document in graph.documents
            if document["bitstream_id"] in nonmaterialized_bitstreams | missing_bitstreams
        }
        tombstones = tuple(
            DownloadTombstone(reference["source_id"], reference["artifact_id"])
            for reference in graph.storage_references
            if reference["document_id"] in tombstone_document_ids
        )
        nonmaterialized_document_ids = {
            document["document_id"]
            for document in effective_graph.documents
            if document["bitstream_id"] in nonmaterialized_bitstreams
        }
        effective_graph = replace(
            effective_graph,
            storage_references=tuple(
                reference
                for reference in effective_graph.storage_references
                if reference["document_id"] not in nonmaterialized_document_ids
            ),
        )
        projected_sources = []
        for source in effective_graph.sources:
            key = _historical_decision(source).approval_key
            effective = effective_by_key.get(key)
            if effective is None or effective.decision_sha256 is None:
                raise ManifestCatalogError("effektive Rechteentscheidung fehlt")
            projected_sources.append({**source, "decision_sha256": effective.decision_sha256})
        effective_graph = replace(
            effective_graph,
            sources=tuple(projected_sources),
        )
        return loaded, effective_graph, effective_register, tombstones
    except (ManifestCatalogError, ManifestGraphError, RightsPolicyError, ValueError) as exc:
        raise BuildDocsError(f"Manifest-Publikationskatalog ist ungültig: {exc}") from exc


def require_unchanged(current: Mapping[str, str], before: Mapping[str, str]) -> None:
    """Abort a build if any publication source changed while rendering."""

    if dict(current) != dict(before):
        raise BuildDocsError("source-hash drift during docs build")


def select_pages(index: ContentIndex, only: tuple[PurePosixPath, ...]) -> tuple[ContentPage, ...]:
    """Return all pages or an exact, canonical Markdown subset."""

    if only == ():
        return index.pages
    selected = []
    seen: set[str] = set()
    for requested in only:
        if not isinstance(requested, PurePosixPath):
            raise BuildDocsError(f"partial page path must be PurePosixPath: {requested!r}")
        raw = requested.as_posix()
        try:
            normalized = normalize_posix_path(raw)
        except UnsafePathError as exc:
            raise BuildDocsError(f"unsafe partial page path: {raw!r}") from exc
        if normalized != raw or not raw.endswith(".md"):
            raise BuildDocsError(f"partial page path must be canonical Markdown: {raw!r}")
        if raw in seen:
            raise BuildDocsError(f"duplicate partial page path: {raw}")
        page = index.page_for_path(raw)
        if page is None:
            raise BuildDocsError(f"missing partial page path: {raw}")
        seen.add(raw)
        selected.append(page)
    return tuple(selected)


def _stage_root(stage: Path) -> Path:
    try:
        metadata = stage.stat()
    except OSError as exc:
        raise BuildDocsError(f"invalid generated stage: {stage}") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise BuildDocsError(f"invalid generated stage: {stage}")
    return stage


def _write_generated_text(stage: Path, path: Path, text: str) -> None:
    root = _stage_root(stage)
    try:
        relative = relative_path_beneath(path, stage)
        atomic_write_text(root / relative, text, allowed_root=root)
    except (OSError, UnsafePathError) as exc:
        raise BuildDocsError(f"cannot write generated output: {path}") from exc


def write_generated_markdown(path: Path, rendered: str, *, stage: Path) -> None:
    """Write one transformed UTF-8 Markdown page below generated stage."""

    _write_generated_text(stage, path, rendered)


def copy_only_regular_local_assets(
    source: Path,
    destination: Path,
    *,
    stage: Path | None = None,
    excluded: frozenset[PurePosixPath] = frozenset(),
) -> None:
    """Copy non-Markdown regular files while rejecting all source symlinks."""

    root = stage if stage is not None else destination
    stage_root = _stage_root(root)
    try:
        prefix = PurePosixPath() if stage is None else relative_path_beneath(destination, root)
    except UnsafePathError as exc:
        raise BuildDocsError(f"unsafe generated asset destination: {destination}") from exc
    for relative, path in _regular_files(source, required=False):
        if path.suffix.casefold() == ".md" or relative in excluded:
            continue
        try:
            target = stage_root / Path((prefix / relative).as_posix())
            atomic_write_bytes(target, path.read_bytes(), allowed_root=stage_root)
        except (OSError, UnsafePathError) as exc:
            raise BuildDocsError(f"cannot copy local asset: {path}") from exc


def validate_generated_docs(stage: Path) -> Mapping[str, str]:
    """Reject unsafe stage entries and return sorted hashes of regular output files."""

    sentinel = stage / _GENERATED_SENTINEL
    try:
        metadata = sentinel.lstat()
    except OSError as exc:
        raise BuildDocsError(f"generated sentinel is missing: {sentinel}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise BuildDocsError(f"generated sentinel is not a regular file: {sentinel}")
    hashes: dict[str, str] = {}
    for relative, path in _regular_files(stage, required=True, allow_root_symlink=True):
        hashes[relative.as_posix()] = sha256_file(path)
    return dict(sorted(hashes.items()))


def _require_index_matches_snapshot(index: ContentIndex, source_hashes: Mapping[str, str]) -> None:
    indexed_sources = {
        (PurePosixPath("content") / page.relative_path).as_posix(): page.source_sha256
        for page in index.pages
    }
    snapshotted_sources = {
        path: digest
        for path, digest in source_hashes.items()
        if path.startswith("content/") and path.endswith(".md")
    }
    if indexed_sources != snapshotted_sources:
        raise BuildDocsError("source-hash drift: content index does not match source snapshot")


def _require_bound_unchanged(repo_root: Path, before: Mapping[str, str]) -> None:
    captured = capture_publication_inputs(repo_root)
    require_unchanged(snapshot_sources(repo_root, captured=captured), before)


def bind_publication_sources(repo_root: Path) -> BoundPublicationSources:
    """Bind content, status, rights, and manifests between one snapshot pair."""

    root = _repo_root(repo_root)
    captured = capture_publication_inputs(root)
    before = snapshot_sources(root, captured=captured)
    try:
        config = load_publication_config(root)
        index = build_content_index(config.content_root)
        _require_index_matches_snapshot(index, before)
        register = parse_rights_register(captured.rights_register_bytes.decode("utf-8"))
        policy = parse_rights_policy(captured.rights_policy_bytes.decode("utf-8"))
        status = _strict_status(captured.status_bytes)
        _loaded, graph, effective_register, catalog_tombstones = load_publication_manifest_catalog(
            captured,
            register=register,
            policy=policy,
        )
        projected_downloads = project_downloads(
            graph,
            status,
            register=effective_register,
            policy=policy,
        )
        downloads = freeze_download_inputs(
            replace(
                projected_downloads,
                tombstones=tuple((*projected_downloads.tombstones, *catalog_tombstones)),
            ),
            before,
        )
    except BuildDocsError:
        raise
    except (DownloadProjectionError, RightsPolicyError, UnicodeDecodeError, ValueError) as exc:
        raise BuildDocsError(f"Publikationsinputs sind ungültig: {exc}") from exc
    _require_bound_unchanged(root, before)
    return BoundPublicationSources(
        source_hashes=MappingProxyType(dict(before)),
        content_index=index,
        download_inputs=downloads,
    )


def render_docs_tree(
    repo_root: Path,
    stage: Path,
    *,
    only: tuple[PurePosixPath, ...] = (),
    bound_sources: BoundPublicationSources | None = None,
) -> DocsBuildResult:
    """Render validated source content into an unpublished generated stage."""

    root = _repo_root(repo_root)
    config = load_publication_config(root)
    try:
        bound = bind_publication_sources(root) if bound_sources is None else bound_sources
        if type(bound) is not BoundPublicationSources:
            raise BuildDocsError("Renderer benötigt exakt gebundene Publikationsinputs")
        before = bound.source_hashes
        index = bound.content_index
        _require_index_matches_snapshot(index, before)
        _require_bound_unchanged(root, before)
        selected = select_pages(index, only)
        selected_paths = {page.relative_path for page in selected}
        excluded_pages = (
            tuple(page for page in index.pages if page.relative_path not in selected_paths)
            if only
            else ()
        )
        table_inputs = (
            load_table_inputs(root, captured_status=bound.download_inputs.public_status)
            if any(page.relative_path == _TABLE_PAGE for page in selected)
            else None
        )
        download_inputs = (
            bound.download_inputs
            if any(page.relative_path == _DOWNLOADS_PAGE for page in selected)
            else None
        )
        for page in selected:
            if page.source_path is None:
                raise BuildDocsError(f"indexed page has no source path: {page.path}")
            wikilinks_converted = convert_for_web(
                page.source,
                page.source_path,
                config.content_root,
                index=index,
            )
            rendered = convert_obsidian_callouts_for_web(wikilinks_converted)
            if page.relative_path == _TABLE_PAGE:
                assert table_inputs is not None
                rendered = render_table_page(rendered, table_inputs)
            if page.relative_path == _DOWNLOADS_PAGE:
                assert download_inputs is not None
                if not download_inputs.rows:
                    rendered = rendered.replace(
                        "<!-- DESINFECT:DOWNLOADS -->",
                        "Noch keine validierten Downloads.\n\n<!-- DESINFECT:DOWNLOADS -->",
                    )
                rendered = rendered.replace(
                    "<!-- DESINFECT:DOWNLOADS -->", "<!-- DESINFECT_DOWNLOADS -->"
                )
                rendered = render_download_page(rendered, download_inputs)
                creators = tuple(
                    creator
                    for row in download_inputs.rows
                    if row.attribution is not None
                    for creator in row.attribution.creators
                )
                if creators:
                    rendered += (
                        "\n\n<p>Urheber: " + html.escape(", ".join(creators), quote=True) + "</p>\n"
                    )
            write_generated_markdown(stage / page.relative_path, rendered, stage=stage)
        for page in excluded_pages:
            write_generated_markdown(
                stage / page.generated_path,
                page.source,
                stage=stage,
            )
        _write_generated_text(
            stage,
            stage / config.generated_sentinel,
            "generated by desinfect\n",
        )
        copy_only_regular_local_assets(
            config.content_root,
            stage,
            excluded=_RAW_TABLE_PROJECTIONS,
        )
        copy_only_regular_local_assets(root / "web/assets", stage / "assets", stage=stage)
        if table_inputs is not None:
            write_table_data_assets(stage, table_inputs)
        rendered_hashes = validate_generated_docs(stage)
        omitted_paths = {page.generated_path.as_posix() for page in excluded_pages}
        docs_hashes = {
            path: digest for path, digest in rendered_hashes.items() if path not in omitted_paths
        }
        _require_bound_unchanged(root, before)
    except BuildDocsError:
        raise
    except (OSError, UnsafePathError, ValueError, yaml.YAMLError) as exc:
        raise BuildDocsError(str(exc)) from exc
    return DocsBuildResult(
        source_hashes=before,
        docs_hashes=docs_hashes,
        published=False,
        excluded_docs=tuple(page.generated_path for page in excluded_pages),
        content_index=index,
    )


def build_docs(
    repo_root: Path,
    *,
    force: bool,
    bound_sources: BoundPublicationSources | None = None,
) -> DocsBuildResult:
    """Atomically publish a complete generated docs tree."""

    root = _repo_root(repo_root)
    config = load_publication_config(root)
    try:
        with staged_directory(config.docs_dir, allowed_root=root, force=force) as stage:
            result = render_docs_tree(
                root,
                stage,
                bound_sources=bound_sources,
            )
    except BuildDocsError:
        raise
    except (OSError, StagingError, UnsafePathError, ValueError) as exc:
        raise BuildDocsError(str(exc)) from exc
    return replace(result, published=True)


@contextmanager
def docs_preview_session(
    repo_root: Path,
    *,
    only: tuple[PurePosixPath, ...] = (),
    bound_sources: BoundPublicationSources | None = None,
) -> Iterator[DocsPreview]:
    """Yield a validated disposable docs tree without publishing it."""

    root = _repo_root(repo_root)
    config = load_publication_config(root)
    preview_target = config.build_root / ".docs-preview"
    stack = ExitStack()
    try:
        try:
            preview_path = stack.enter_context(preview_directory(preview_target, allowed_root=root))
            result = render_docs_tree(
                root,
                preview_path,
                only=only,
                bound_sources=bound_sources,
            )
        except BuildDocsError:
            raise
        except (OSError, StagingError, UnsafePathError, ValueError) as exc:
            raise BuildDocsError(str(exc)) from exc
        yield DocsPreview(docs_dir=preview_path, result=result)
    except BaseException:
        active = sys.exc_info()
        try:
            stack.__exit__(*active)
        except BaseException as teardown_error:
            if active[1] is not None:
                active[1].add_note(f"Zusätzlicher Preview-Teardownfehler: {teardown_error}")
            else:
                raise
        raise
    else:
        try:
            stack.close()
        except BuildDocsError:
            raise
        except (OSError, StagingError, UnsafePathError, ValueError) as exc:
            raise BuildDocsError(str(exc)) from exc
