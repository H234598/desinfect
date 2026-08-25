#!/usr/bin/env python3
"""Strict, atomic MkDocs site builds."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from html.parser import HTMLParser
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.rki_pipeline.io_utils import (
    UnsafePathError,
    assert_generated_root_fd,
    fd_directory_path,
    normalize_posix_path,
    open_directory_beneath,
    open_root_directory,
    relative_path_beneath,
    sha256_file,
    source_date_epoch,
    validate_tree_no_symlinks_fd,
)
from scripts.rki_pipeline.staging import StagingError, preview_directory, staged_directory
from scripts.web.build_docs import (
    BuildDocsError,
    DocsBuildResult,
    _regular_files,
    build_docs,
    docs_preview_session,
    load_publication_config,
    snapshot_sources,
)


class SiteBuildError(RuntimeError):
    """A strict site build could not complete safely."""


@dataclass(frozen=True, slots=True)
class SiteBuildResult:
    """Result of one strict site transaction."""

    docs: DocsBuildResult
    site_hashes: Mapping[str, str]
    published: bool


_FD_DIRECTORY_PATH = re.compile(r"^(?:/proc/self/fd|/dev/fd)/(0|[1-9][0-9]*)$")
_STATIC_MKDOCS_KEYS = {
    "extra_css",
    "markdown_extensions",
    "plugins",
    "site_name",
    "theme",
}
_RESOURCE_LINK_RELATIONS = frozenset(
    {
        "dns-prefetch",
        "icon",
        "manifest",
        "modulepreload",
        "preconnect",
        "prefetch",
        "preload",
        "prerender",
        "stylesheet",
    }
)
_CSS_URL = re.compile(
    r"url\s*\(\s*(?P<target>\"[^\"]*\"?|'[^']*'?|[^)]*)\s*(?:\)|$)",
    re.IGNORECASE,
)
_CSS_IMPORT = re.compile(
    r"@import\s+(?P<target>\"[^\"]*\"?|'[^']*'?|[^;\s]+)",
    re.IGNORECASE,
)
_CSS_IMAGE_FUNCTIONS = frozenset({"-webkit-image-set", "image", "image-set"})
_CSS_ESCAPE = re.compile(
    r"\\(?:"
    r"(?P<hex>[0-9a-f]{1,6})(?:\r\n|[\t\n\f\r ])?"
    r"|(?P<continuation>\r\n|[\n\f\r])"
    r"|(?P<character>.)"
    r")",
    re.IGNORECASE | re.DOTALL,
)
_ASCII_C0_SPACE = "".join(chr(value) for value in range(0x21))
_ASCII_WHITESPACE = "\t\n\f\r "
_BROWSER_URL_REMOVALS = str.maketrans("", "", "\t\n\r")
_MAX_SRCDOC_DEPTH = 4
_MAX_SRCDOC_LENGTH = 64 * 1024
_SVG_PRESENTATION_URL_ATTRIBUTES = (
    "clip-path",
    "fill",
    "filter",
    "marker",
    "marker-end",
    "marker-mid",
    "marker-start",
    "mask",
    "stroke",
)


def _browser_url(value: str) -> str:
    normalized = value.translate(_BROWSER_URL_REMOVALS).strip(_ASCII_C0_SPACE)
    return normalized.replace("\\", "/")


def _external_url(value: str) -> bool:
    return _browser_url(value).casefold().startswith(("http:", "https:", "//"))


def _srcset_urls(value: str) -> Iterator[str]:
    normalized = value.translate(_BROWSER_URL_REMOVALS)
    position = 0
    while position < len(normalized):
        while position < len(normalized) and normalized[position] in _ASCII_WHITESPACE + ",":
            position += 1
        start = position
        while position < len(normalized) and normalized[position] not in _ASCII_WHITESPACE:
            position += 1
        candidate = normalized[start:position]
        if candidate.endswith(","):
            candidate = candidate.rstrip(",")
            if candidate:
                yield candidate
            continue
        if candidate:
            yield candidate
        parentheses = 0
        while position < len(normalized):
            character = normalized[position]
            position += 1
            if character == "(":
                parentheses += 1
            elif character == ")" and parentheses:
                parentheses -= 1
            elif character == "," and not parentheses:
                break


def _external_srcset(value: str) -> bool:
    return any(_external_url(candidate) for candidate in _srcset_urls(value))


def _contains_external_url_token(value: str) -> bool:
    normalized = value.translate(_BROWSER_URL_REMOVALS).casefold()
    return any(token in normalized for token in ("http:", "https:", "//"))


class _ExternalAssetParser(HTMLParser):
    _RESOURCE_ATTRIBUTES = {
        "audio": ("src",),
        "base": ("href",),
        "embed": ("src",),
        "feimage": ("href", "xlink:href"),
        "iframe": ("src",),
        "image": ("href", "xlink:href"),
        "img": ("src",),
        "input": ("src",),
        "lineargradient": ("href", "xlink:href"),
        "link": ("href",),
        "mpath": ("href", "xlink:href"),
        "object": ("data",),
        "pattern": ("href", "xlink:href"),
        "radialgradient": ("href", "xlink:href"),
        "script": ("src", "href", "xlink:href"),
        "source": ("src",),
        "svg:feimage": ("href", "xlink:href"),
        "svg:image": ("href", "xlink:href"),
        "svg:lineargradient": ("href", "xlink:href"),
        "svg:mpath": ("href", "xlink:href"),
        "svg:pattern": ("href", "xlink:href"),
        "svg:radialgradient": ("href", "xlink:href"),
        "svg:textpath": ("href", "xlink:href"),
        "svg:use": ("href", "xlink:href"),
        "textpath": ("href", "xlink:href"),
        "track": ("src",),
        "use": ("href", "xlink:href"),
        "video": ("src", "poster"),
    }
    _SRCSET_ATTRIBUTES = {
        "img": ("srcset",),
        "link": ("imagesrcset",),
        "source": ("srcset",),
    }

    def __init__(self, *, srcdoc_depth: int = 0) -> None:
        super().__init__()
        self.urls: list[str] = []
        self._srcdoc_depth = srcdoc_depth
        self._style_buffers: list[list[str]] = []

    @staticmethod
    def _attribute_values(attrs: list[tuple[str, str | None]], name: str) -> Iterator[str]:
        return (value for attribute, value in attrs if attribute == name and value is not None)

    def _scan_srcdoc(self, value: str) -> None:
        if self._srcdoc_depth >= _MAX_SRCDOC_DEPTH or len(value) > _MAX_SRCDOC_LENGTH:
            if _contains_external_url_token(value):
                self.urls.append(value)
            return
        nested = _ExternalAssetParser(srcdoc_depth=self._srcdoc_depth + 1)
        nested.feed(value)
        nested.close()
        self.urls.extend(nested.urls)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "style":
            self._style_buffers.append([])
        for value in self._attribute_values(attrs, "style"):
            self.urls.extend(external_css_asset_urls(value))
        for attribute in _SVG_PRESENTATION_URL_ATTRIBUTES:
            for value in self._attribute_values(attrs, attribute):
                self.urls.extend(external_css_asset_urls(value))
        if tag == "iframe":
            for value in self._attribute_values(attrs, "srcdoc"):
                self._scan_srcdoc(value)

        if tag == "link":
            relations = {
                token
                for value in self._attribute_values(attrs, "rel")
                for token in value.casefold().split()
            }
            if not _RESOURCE_LINK_RELATIONS.intersection(relations):
                return

        for attribute in self._RESOURCE_ATTRIBUTES.get(tag, ()):
            for value in self._attribute_values(attrs, attribute):
                if _external_url(value):
                    self.urls.append(value)
        for attribute in self._SRCSET_ATTRIBUTES.get(tag, ()):
            for value in self._attribute_values(attrs, attribute):
                if _external_srcset(value):
                    self.urls.append(value)

    def handle_data(self, data: str) -> None:
        if self._style_buffers:
            self._style_buffers[-1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "style" and self._style_buffers:
            self.urls.extend(external_css_asset_urls("".join(self._style_buffers.pop())))

    def close(self) -> None:
        super().close()
        while self._style_buffers:
            self.urls.extend(external_css_asset_urls("".join(self._style_buffers.pop())))


def external_asset_urls(html: str) -> list[str]:
    """Return external browser-resource URLs referenced by generated HTML."""

    parser = _ExternalAssetParser()
    parser.feed(html)
    parser.close()
    return parser.urls


def _css_target(raw: str) -> str:
    target = raw.strip()
    if target[:1] in {'"', "'"}:
        quote = target[0]
        target = target[1:]
        if target.endswith(quote):
            target = target[:-1]
        target = target.strip()
    return target


def _decode_css_escape(match: re.Match[str]) -> str:
    if match.group("continuation") is not None:
        return ""
    if match.group("hex") is None:
        return match.group("character")
    codepoint = int(match.group("hex"), 16)
    if codepoint == 0 or codepoint > sys.maxunicode or 0xD800 <= codepoint <= 0xDFFF:
        return "\N{REPLACEMENT CHARACTER}"
    return chr(codepoint)


def _normalized_css(css: str) -> tuple[str, frozenset[int]]:
    normalized: list[str] = []
    escaped_positions: set[int] = set()
    quote: str | None = None
    position = 0

    while position < len(css):
        character = css[position]
        if character == "\\":
            escape = _CSS_ESCAPE.match(css, position)
            if escape is not None:
                decoded = _decode_css_escape(escape)
                if decoded:
                    escaped_positions.add(len(normalized))
                    normalized.append(decoded)
                position = escape.end()
                continue
        if quote is None and css.startswith("/*", position):
            comment_end = css.find("*/", position + 2)
            normalized.append(" ")
            position = len(css) if comment_end < 0 else comment_end + 2
            continue

        normalized.append(character)
        if character in {'"', "'"}:
            if quote is None:
                quote = character
            elif character == quote:
                quote = None
        position += 1

    return "".join(normalized), frozenset(escaped_positions)


def _css_image_function_strings(
    css: str,
    escaped_positions: frozenset[int] = frozenset(),
) -> Iterator[tuple[int, str]]:
    function_stack: list[bool] = []
    token_start: int | None = None
    pending_name: str | None = None
    quote: str | None = None
    quote_start = 0
    capture_quote = False

    for position, character in enumerate(css):
        if quote is not None:
            if character == quote and position not in escaped_positions:
                if capture_quote:
                    yield quote_start, css[quote_start:position]
                quote = None
            continue

        if character in {'"', "'"} and position not in escaped_positions:
            quote = character
            quote_start = position + 1
            capture_quote = bool(function_stack and function_stack[-1])
            token_start = None
            pending_name = None
        elif character.isalnum() or character in {"-", "_"}:
            if token_start is None:
                token_start = position
                pending_name = None
        elif character in _ASCII_WHITESPACE:
            if token_start is not None:
                pending_name = css[token_start:position].casefold()
                token_start = None
        elif character == "(":
            name = css[token_start:position].casefold() if token_start is not None else pending_name
            function_stack.append(name in _CSS_IMAGE_FUNCTIONS)
            token_start = None
            pending_name = None
        else:
            if character == ")" and function_stack:
                function_stack.pop()
            token_start = None
            pending_name = None

    if quote is not None and capture_quote:
        yield quote_start, css[quote_start:]


def external_css_asset_urls(css: str) -> list[str]:
    """Return external browser-resource URLs referenced by generated CSS."""

    normalized, escaped_positions = _normalized_css(css)
    regex_input = "".join(
        " " if position in escaped_positions and character in {'"', "'"} else character
        for position, character in enumerate(normalized)
    )
    candidates = [
        (
            match.start(),
            _css_target(normalized[slice(*match.span("target"))]),
        )
        for pattern in (_CSS_URL, _CSS_IMPORT)
        for match in pattern.finditer(regex_input)
    ]
    candidates.extend(_css_image_function_strings(normalized, escaped_positions))
    return [target for _, target in sorted(candidates) if _external_url(target)]


def _repo_root(repo_root: Path) -> Path:
    try:
        return repo_root.resolve(strict=True)
    except OSError as exc:
        raise SiteBuildError(f"invalid repository root: {repo_root}") from exc


def _validated_site_url(site_url: str | None) -> str | None:
    if site_url is None:
        return None
    try:
        parsed = urlsplit(site_url)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise SiteBuildError("site_url is not a valid HTTPS URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(character.isspace() for character in site_url)
    ):
        raise SiteBuildError(
            "site_url must be an HTTPS URL without credentials, query, or fragment"
        )
    return site_url


def _validated_only(only: tuple[PurePosixPath, ...]) -> tuple[PurePosixPath, ...]:
    selected: list[PurePosixPath] = []
    for value in only:
        if not isinstance(value, PurePosixPath):
            raise SiteBuildError(f"--only must be a relative Markdown path: {value!r}")
        raw = value.as_posix()
        try:
            normalized = normalize_posix_path(raw)
        except UnsafePathError as exc:
            raise SiteBuildError(f"unsafe --only path: {raw!r}") from exc
        if normalized != raw or not raw.endswith(".md"):
            raise SiteBuildError(f"--only must be canonical Markdown: {raw!r}")
        if value in selected:
            raise SiteBuildError(f"duplicate --only path: {raw}")
        selected.append(value)
    return tuple(selected)


def _contains_external_url(value: object) -> bool:
    if isinstance(value, str):
        return value.lstrip().lower().startswith(("http:", "https:", "//"))
    if isinstance(value, Mapping):
        return any(_contains_external_url(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_external_url(item) for item in value)
    return False


def _validate_static_mkdocs_config(parsed: dict[object, object]) -> None:
    theme = parsed.get("theme")
    if isinstance(theme, Mapping):
        asset_fields = (
            parsed.get("extra_css"),
            parsed.get("extra_javascript"),
            theme.get("favicon"),
            theme.get("font"),
            theme.get("icon"),
            theme.get("logo"),
        )
        if any(_contains_external_url(value) for value in asset_fields):
            raise SiteBuildError("mkdocs asset and font fields must use local resources")
    if set(parsed) != _STATIC_MKDOCS_KEYS:
        raise SiteBuildError("mkdocs configuration has unsupported top-level fields")
    if parsed.get("site_name") != "Desinfect":
        raise SiteBuildError("mkdocs site_name must be Desinfect")
    if not isinstance(theme, dict):
        raise SiteBuildError("mkdocs theme must be one Material mapping")
    if theme.get("font") is not False:
        raise SiteBuildError("mkdocs theme font must be false")
    if theme != {
        "name": "material",
        "language": "de",
        "font": False,
        "custom_dir": "web/overrides",
        "palette": [
            {
                "media": "(prefers-color-scheme: light)",
                "scheme": "default",
                "toggle": {
                    "icon": "material/brightness-7",
                    "name": "Dunkles Farbschema aktivieren",
                },
            },
            {
                "media": "(prefers-color-scheme: dark)",
                "scheme": "slate",
                "toggle": {
                    "icon": "material/brightness-4",
                    "name": "Helles Farbschema aktivieren",
                },
            },
        ],
    }:
        raise SiteBuildError("mkdocs theme must use local German Material settings")
    if parsed.get("plugins") != [{"search": {"lang": "de"}}]:
        raise SiteBuildError("mkdocs search plugin language must be de")
    if parsed.get("markdown_extensions") != [
        "admonition",
        "pymdownx.details",
        "pymdownx.superfences",
    ]:
        raise SiteBuildError("mkdocs Markdown extensions are unsupported")
    if parsed.get("extra_css") != ["assets/stylesheets/extra.css"]:
        raise SiteBuildError("mkdocs extra_css must contain only the local stylesheet")


def _regular_site_hashes(stage: Path) -> Mapping[str, str]:
    try:
        hashes: dict[str, str] = {}
        for relative, path in _regular_files(stage, required=True, allow_root_symlink=True):
            if path.suffix.casefold() == ".html":
                external = external_asset_urls(path.read_text(encoding="utf-8"))
            elif path.suffix.casefold() == ".css":
                external = external_css_asset_urls(path.read_text(encoding="utf-8"))
            else:
                external = []
            if external:
                raise SiteBuildError(
                    f"external browser resource in generated site file: {relative}"
                )
            hashes[relative.as_posix()] = sha256_file(path)
        return dict(sorted(hashes.items()))
    except (BuildDocsError, OSError, ValueError) as exc:
        raise SiteBuildError(f"cannot validate generated site: {stage}") from exc


def _require_regular_file(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SiteBuildError(f"required site output is missing: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise SiteBuildError(f"required site output is not a regular file: {path}")


def _validate_site_tree(stage: Path) -> Mapping[str, str]:
    _require_regular_file(stage / "index.html")
    _require_regular_file(stage / "404.html")
    return _regular_site_hashes(stage)


def write_temp_mkdocs_config(
    *,
    repo_root: Path,
    config_dir: Path,
    docs_dir: Path,
    site_dir: Path,
    site_url: str | None,
    source_date_epoch: int,
) -> Path:
    """Write one MkDocs config through a held FD-backed config directory."""

    del source_date_epoch
    source = repo_root / "mkdocs.yml"
    try:
        if source.is_symlink() or not source.is_file():
            raise SiteBuildError(f"mkdocs source must be a regular file: {source}")
        parsed = yaml.safe_load(source.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise SiteBuildError("mkdocs configuration must be a mapping")
        _validate_static_mkdocs_config(parsed)
        if not docs_dir.is_absolute() or not site_dir.is_absolute():
            raise SiteBuildError("mkdocs docs_dir and site_dir must be absolute directories")
        absolute_docs = (
            docs_dir
            if _FD_DIRECTORY_PATH.fullmatch(docs_dir.as_posix())
            else docs_dir.resolve(strict=True)
        )
        absolute_site = (
            site_dir
            if _FD_DIRECTORY_PATH.fullmatch(site_dir.as_posix())
            else site_dir.resolve(strict=True)
        )
        if not absolute_docs.is_dir() or not absolute_site.is_dir():
            raise SiteBuildError("mkdocs docs_dir and site_dir must be existing directories")
        if _FD_DIRECTORY_PATH.fullmatch(config_dir.as_posix()) is None or not config_dir.is_dir():
            raise SiteBuildError("mkdocs config_dir must be an existing FD-backed directory")
        parsed["theme"]["custom_dir"] = str((repo_root / "web/overrides").resolve(strict=True))
        parsed["docs_dir"] = str(absolute_docs)
        parsed["site_dir"] = str(absolute_site)
        if site_url is not None:
            parsed["site_url"] = site_url
        descriptor, raw_path = tempfile.mkstemp(
            prefix=".mkdocs-build-", suffix=".yml", dir=config_dir
        )
        path = Path(raw_path)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                descriptor = -1
                yaml.safe_dump(parsed, handle, allow_unicode=True, sort_keys=True)
        except BaseException:
            if descriptor != -1:
                os.close(descriptor)
            path.unlink(missing_ok=True)
            raise
        return path
    except SiteBuildError:
        raise
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError) as exc:
        raise SiteBuildError(f"cannot create temporary MkDocs config: {source}") from exc


def _fd_descriptor(path: Path) -> int | None:
    match = _FD_DIRECTORY_PATH.fullmatch(path.as_posix())
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError as exc:
        raise SiteBuildError(f"invalid FD-backed stage path: {path}") from exc


@contextmanager
def _held_generated_directory(target: Path, *, allowed_root: Path) -> Iterator[Path]:
    """Yield a no-follow, sentinel-validated FD path for generated output."""

    relative = relative_path_beneath(target, allowed_root)
    with open_root_directory(allowed_root) as root_fd:
        descriptor = open_directory_beneath(root_fd, relative.parts)
        try:
            assert_generated_root_fd(descriptor)
            validate_tree_no_symlinks_fd(descriptor)
            yield fd_directory_path(descriptor)
        finally:
            os.close(descriptor)


def run_mkdocs_build(
    repo_root: Path,
    config_path: Path,
    *,
    strict: bool,
    epoch: int,
    pass_fds: tuple[int, ...] = (),
) -> int:
    """Run MkDocs through the current interpreter without shell parsing."""

    command = [
        sys.executable,
        "-m",
        "mkdocs",
        "build",
        "--config-file",
        str(config_path),
    ]
    if strict:
        command.append("--strict")
    environment = os.environ.copy()
    environment["SOURCE_DATE_EPOCH"] = str(epoch)
    options: dict[str, object] = {
        "cwd": repo_root,
        "check": False,
        "capture_output": True,
        "text": True,
        "env": environment,
    }
    if pass_fds:
        options["pass_fds"] = pass_fds
    completed = subprocess.run(command, **options)
    return completed.returncode


def _run_site_build(
    *,
    repo_root: Path,
    docs_dir: Path,
    site_dir: Path,
    site_url: str | None,
    epoch: int,
) -> Mapping[str, str]:
    try:
        publication = load_publication_config(repo_root)
        config_target = publication.build_root / ".mkdocs-config-preview" / "config"
        with preview_directory(config_target, allowed_root=repo_root) as config_stage:
            config_path: Path | None = None
            try:
                config_path = write_temp_mkdocs_config(
                    repo_root=repo_root,
                    config_dir=config_stage,
                    docs_dir=docs_dir,
                    site_dir=site_dir,
                    site_url=site_url,
                    source_date_epoch=epoch,
                )
                descriptors = tuple(
                    sorted(
                        {
                            descriptor
                            for descriptor in (
                                _fd_descriptor(config_stage),
                                _fd_descriptor(docs_dir),
                                _fd_descriptor(site_dir),
                            )
                            if descriptor is not None
                        }
                    )
                )
                if (
                    run_mkdocs_build(
                        repo_root,
                        config_path,
                        strict=True,
                        epoch=epoch,
                        pass_fds=descriptors,
                    )
                    != 0
                ):
                    raise SiteBuildError("MkDocs strict build failed")
                result = _validate_site_tree(site_dir)
            except BaseException as primary:
                if config_path is not None:
                    try:
                        config_path.unlink(missing_ok=True)
                    except BaseException as cleanup_error:
                        primary.add_note(f"Temporary MkDocs config cleanup failed: {cleanup_error}")
                raise
            if config_path is not None:
                try:
                    config_path.unlink(missing_ok=True)
                except BaseException as cleanup_error:
                    raise SiteBuildError(
                        "Temporary MkDocs config cleanup failed"
                    ) from cleanup_error
            return result
    except SiteBuildError:
        raise
    except (BuildDocsError, OSError, StagingError, UnsafePathError, ValueError) as exc:
        raise SiteBuildError(str(exc)) from exc


@contextmanager
def site_preview_session(repo_root: Path) -> Iterator[Path]:
    """Yield a disposable FD-anchored site directory without publishing it."""

    root = _repo_root(repo_root)
    try:
        config = load_publication_config(root)
    except BuildDocsError as exc:
        raise SiteBuildError(str(exc)) from exc
    stack = ExitStack()
    try:
        try:
            stage = stack.enter_context(preview_directory(config.site_dir, allowed_root=root))
        except BuildDocsError:
            raise
        except (OSError, StagingError, UnsafePathError, ValueError) as exc:
            raise SiteBuildError(str(exc)) from exc
        yield stage
    except BaseException:
        active = sys.exc_info()
        try:
            stack.__exit__(*active)
        except BaseException as teardown_error:
            if active[1] is not None:
                active[1].add_note(f"Additional site preview teardown error: {teardown_error}")
            else:
                raise
        raise
    else:
        try:
            stack.close()
        except SiteBuildError:
            raise
        except (OSError, StagingError, UnsafePathError, ValueError) as exc:
            raise SiteBuildError(str(exc)) from exc


def build_site(
    repo_root: Path,
    *,
    check: bool,
    dry_run: bool,
    strict: bool,
    force: bool,
    site_url: str | None,
    only: tuple[PurePosixPath, ...] = (),
) -> SiteBuildResult:
    """Build and atomically publish a strict site, or run disposable previews."""

    if only and not dry_run:
        raise SiteBuildError("--only requires --dry-run")
    if force and dry_run:
        raise SiteBuildError("--force cannot be combined with --dry-run")
    if not strict:
        raise SiteBuildError("--strict is required for site builds")
    selected = _validated_only(only)
    validated_url = _validated_site_url(site_url)
    root = _repo_root(repo_root)
    epoch = source_date_epoch()
    try:
        before = snapshot_sources(root)
        config = load_publication_config(root)
    except (BuildDocsError, OSError, UnsafePathError, ValueError) as exc:
        raise SiteBuildError(str(exc)) from exc
    del check

    if dry_run:
        try:
            with docs_preview_session(root, only=selected) as docs:
                with site_preview_session(root) as site_stage:
                    site_hashes = _run_site_build(
                        repo_root=root,
                        docs_dir=docs.docs_dir,
                        site_dir=site_stage,
                        site_url=validated_url,
                        epoch=epoch,
                    )
                    if snapshot_sources(root) != before:
                        raise SiteBuildError("source-hash drift during site build")
        except SiteBuildError:
            raise
        except (BuildDocsError, OSError, StagingError, UnsafePathError, ValueError) as exc:
            raise SiteBuildError(str(exc)) from exc
        return SiteBuildResult(docs=docs.result, site_hashes=site_hashes, published=False)

    try:
        docs = build_docs(root, force=force)
        with _held_generated_directory(config.docs_dir, allowed_root=root) as held_docs:
            with staged_directory(config.site_dir, allowed_root=root, force=force) as site_stage:
                site_hashes = _run_site_build(
                    repo_root=root,
                    docs_dir=held_docs,
                    site_dir=site_stage,
                    site_url=validated_url,
                    epoch=epoch,
                )
                if snapshot_sources(root) != before:
                    raise SiteBuildError("source-hash drift during site build")
    except SiteBuildError:
        raise
    except (BuildDocsError, OSError, StagingError, UnsafePathError, ValueError) as exc:
        raise SiteBuildError(str(exc)) from exc
    return SiteBuildResult(docs=docs, site_hashes=site_hashes, published=True)


def _parse_only(values: Sequence[str]) -> tuple[PurePosixPath, ...]:
    parsed: list[PurePosixPath] = []
    for value in values:
        try:
            normalized = normalize_posix_path(value)
        except UnsafePathError as exc:
            raise SiteBuildError(f"unsafe --only path: {value!r}") from exc
        if normalized != value:
            raise SiteBuildError(f"--only must use canonical Markdown syntax: {value!r}")
        parsed.append(PurePosixPath(value))
    return tuple(parsed)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build strict MkDocs site")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--site-url")
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        build_site(
            args.repo_root,
            check=args.check,
            dry_run=args.dry_run,
            strict=args.strict,
            force=args.force,
            site_url=args.site_url,
            only=_parse_only(args.only),
        )
    except (SiteBuildError, ValueError) as exc:
        print(f"site build failed: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
