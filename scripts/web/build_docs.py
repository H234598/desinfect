"""Deterministic, atomic generated Markdown documentation builds."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

import yaml

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
    atomic_write_bytes,
    atomic_write_text,
    ensure_within,
    fd_directory_path,
    mark_generated_root_fd,
    normalize_posix_path,
    open_directory_beneath,
    open_root_directory,
    relative_path_beneath,
    remove_tree_at,
    sha256_file,
)
from scripts.rki_pipeline.staging import StagingError, staged_directory
from scripts.web.callouts import convert_obsidian_callouts_for_web
from scripts.web.content_index import ContentIndex, build_content_index
from scripts.web.content_model import ContentPage
from scripts.web.link_converters import convert_for_web


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
_GENERATED_SENTINEL = ".desinfect-generated"


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
        return stage.resolve(strict=True)
    except OSError as exc:
        raise BuildDocsError(f"invalid generated stage: {stage}") from exc


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
    source: Path, destination: Path, *, stage: Path | None = None
) -> None:
    """Copy non-Markdown regular files while rejecting all source symlinks."""

    root = stage if stage is not None else destination
    stage_root = _stage_root(root)
    try:
        prefix = PurePosixPath() if stage is None else relative_path_beneath(destination, root)
    except UnsafePathError as exc:
        raise BuildDocsError(f"unsafe generated asset destination: {destination}") from exc
    for relative, path in _regular_files(source, required=False):
        if path.suffix.casefold() == ".md":
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


def render_docs_tree(
    repo_root: Path,
    stage: Path,
    *,
    only: tuple[PurePosixPath, ...] = (),
) -> DocsBuildResult:
    """Render validated source content into an unpublished generated stage."""

    root = _repo_root(repo_root)
    config = load_publication_config(root)
    try:
        index = build_content_index(config.content_root)
        before = snapshot_sources(root)
        selected = select_pages(index, only)
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
            write_generated_markdown(stage / page.relative_path, rendered, stage=stage)
        _write_generated_text(
            stage,
            stage / config.generated_sentinel,
            "generated by desinfect\n",
        )
        copy_only_regular_local_assets(config.content_root, stage)
        copy_only_regular_local_assets(root / "web/assets", stage / "assets", stage=stage)
        docs_hashes = validate_generated_docs(stage)
        current = snapshot_sources(root)
        require_unchanged(current, before)
    except BuildDocsError:
        raise
    except (OSError, UnsafePathError, ValueError, yaml.YAMLError) as exc:
        raise BuildDocsError(str(exc)) from exc
    return DocsBuildResult(source_hashes=before, docs_hashes=docs_hashes, published=False)


def build_docs(repo_root: Path, *, force: bool) -> DocsBuildResult:
    """Atomically publish a complete generated docs tree."""

    root = _repo_root(repo_root)
    config = load_publication_config(root)
    try:
        with staged_directory(config.docs_dir, allowed_root=root, force=force) as stage:
            result = render_docs_tree(root, stage)
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
) -> Iterator[DocsPreview]:
    """Yield a validated disposable docs tree without publishing it."""

    root = _repo_root(repo_root)
    config = load_publication_config(root)
    try:
        build_relative = relative_path_beneath(config.build_root, root)
    except (OSError, UnsafePathError, ValueError) as exc:
        raise BuildDocsError(str(exc)) from exc
    with open_root_directory(root, create=True) as root_fd:
        build_fd: int | None = None
        preview_fd: int | None = None
        preview_name: str | None = None
        try:
            try:
                build_fd = open_directory_beneath(root_fd, build_relative.parts, create=True)
                build_path = fd_directory_path(build_fd)
                preview_path = Path(tempfile.mkdtemp(prefix=".docs-preview-", dir=build_path))
                preview_name = preview_path.name
                preview_fd = open_directory_beneath(build_fd, (preview_name,))
                mark_generated_root_fd(preview_fd)
                result = render_docs_tree(root, fd_directory_path(preview_fd), only=only)
            except BuildDocsError:
                raise
            except (OSError, UnsafePathError, ValueError) as exc:
                raise BuildDocsError(str(exc)) from exc
            yield DocsPreview(docs_dir=preview_path.resolve(), result=result)
        finally:
            if preview_fd is not None:
                os.close(preview_fd)
            if preview_name is not None and build_fd is not None:
                remove_tree_at(build_fd, preview_name)
            if build_fd is not None:
                os.close(build_fd)
