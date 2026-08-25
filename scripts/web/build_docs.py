"""Deterministic, atomic generated Markdown documentation builds."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
import stat
import sys

import yaml

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
    atomic_write_bytes,
    atomic_write_text,
    ensure_within,
    normalize_posix_path,
    relative_path_beneath,
    sha256_file,
)
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
_RAW_TABLE_PROJECTIONS = frozenset(
    {
        PurePosixPath("generated-data/corpus-table.json"),
        PurePosixPath("generated-data/anleitungen.json"),
    }
)
_EXACT_PUBLICATION_INPUTS = (
    (PurePosixPath("status.json"), True),
    (PurePosixPath("research/corpus-readiness.json"), False),
    (PurePosixPath("research/taxonomy.yml"), False),
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


def snapshot_sources(repo_root: Path) -> Mapping[str, str]:
    """Hash all publication inputs without changing source bytes."""

    root = _repo_root(repo_root)
    hashes: dict[str, str] = {}
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
            hashes[(prefix / relative).as_posix()] = sha256_file(path)
    return dict(sorted(hashes.items()))


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


def bind_publication_sources(repo_root: Path) -> tuple[Mapping[str, str], ContentIndex]:
    """Bind one immutable content index to one stable publication-source snapshot."""

    root = _repo_root(repo_root)
    before = snapshot_sources(root)
    index = build_content_index(load_publication_config(root).content_root)
    _require_index_matches_snapshot(index, before)
    require_unchanged(snapshot_sources(root), before)
    return before, index


def render_docs_tree(
    repo_root: Path,
    stage: Path,
    *,
    only: tuple[PurePosixPath, ...] = (),
    source_hashes: Mapping[str, str] | None = None,
    content_index: ContentIndex | None = None,
) -> DocsBuildResult:
    """Render validated source content into an unpublished generated stage."""

    root = _repo_root(repo_root)
    config = load_publication_config(root)
    try:
        if (source_hashes is None) != (content_index is None):
            raise BuildDocsError("source snapshot and content index must be bound together")
        if source_hashes is None:
            before, index = bind_publication_sources(root)
        else:
            before = dict(source_hashes)
            assert content_index is not None
            index = content_index
            _require_index_matches_snapshot(index, before)
            require_unchanged(snapshot_sources(root), before)
        selected = select_pages(index, only)
        selected_paths = {page.relative_path for page in selected}
        excluded_pages = (
            tuple(page for page in index.pages if page.relative_path not in selected_paths)
            if only
            else ()
        )
        table_inputs = (
            load_table_inputs(root)
            if any(page.relative_path == _TABLE_PAGE for page in selected)
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
        current = snapshot_sources(root)
        require_unchanged(current, before)
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
    source_hashes: Mapping[str, str] | None = None,
    content_index: ContentIndex | None = None,
) -> DocsBuildResult:
    """Atomically publish a complete generated docs tree."""

    root = _repo_root(repo_root)
    config = load_publication_config(root)
    try:
        with staged_directory(config.docs_dir, allowed_root=root, force=force) as stage:
            result = render_docs_tree(
                root,
                stage,
                source_hashes=source_hashes,
                content_index=content_index,
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
    source_hashes: Mapping[str, str] | None = None,
    content_index: ContentIndex | None = None,
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
                source_hashes=source_hashes,
                content_index=content_index,
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
