"""Read-only content inventory adapted from the frozen Cheatsheets blueprint."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType

from scripts.rki_pipeline.io_utils import portable_collision_key
from scripts.web.content_model import (
    ContentModelError,
    ContentPage,
    HeadingRecord,
    mask_protected,
    normalize_key,
    protected_spans,
    slugify,
)


_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$")
_EXPLICIT_ANCHOR_RE = re.compile(r"\s*\{#([-\w:.]+)\}\s*$")
_LFS_POINTER_RE = re.compile(
    rb"\Aversion https://git-lfs\.github\.com/spec/v1\r?\n"
    rb"oid sha256:[0-9a-f]{64}\r?\n"
    rb"size [0-9]+\r?\n?\Z"
)


@dataclass(frozen=True, slots=True)
class ContentIndex:
    root: Path
    pages: tuple[ContentPage, ...]
    _by_path: MappingProxyType[str, ContentPage]
    _by_folded_path: MappingProxyType[str, ContentPage]
    _lookup: MappingProxyType[str, tuple[ContentPage, ...]]

    def page_for_path(self, value: str | Path | PurePosixPath) -> ContentPage | None:
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.resolve(strict=False).relative_to(self.root)
            except ValueError:
                return None
        relative = PurePosixPath(path.as_posix()).as_posix()
        return self._by_path.get(relative)

    def folded_page_for_path(self, value: str | Path | PurePosixPath) -> ContentPage | None:
        path = Path(value)
        if path.is_absolute():
            try:
                path = path.resolve(strict=False).relative_to(self.root)
            except ValueError:
                return None
        return self._by_folded_path.get(portable_collision_key(path.as_posix()))

    def lookup_pages(self, value: str) -> tuple[ContentPage, ...]:
        return self._lookup.get(normalize_key(value), ())


def _strip_prose_markup(value: str) -> str:
    value = re.sub(r"(?<!\w)_+|_+(?!\w)", "", value)
    return re.sub(r"[`*~]", "", value)


def _strip_heading_markup(
    value: str,
    code_spans: tuple[tuple[int, int], ...] = (),
) -> tuple[str, str | None]:
    code_spans = tuple(
        (max(0, start), min(len(value), end))
        for start, end in code_spans
        if start < len(value) and end > 0
    )
    probe = list(value)
    for start, end in code_spans:
        probe[start:end] = " " * (end - start)
    explicit = _EXPLICIT_ANCHOR_RE.search("".join(probe))
    explicit_anchor = explicit.group(1) if explicit else None
    if explicit is not None:
        value = value[: explicit.start()]
        probe = probe[: explicit.start()]
    closing_hashes = re.search(r"\s+#+\s*$", "".join(probe))
    if closing_hashes is not None:
        value = value[: closing_hashes.start()]

    parts: list[str] = []
    cursor = 0
    for start, end in code_spans:
        if start >= len(value):
            break
        end = min(end, len(value))
        parts.append(_strip_prose_markup(value[cursor:start]))
        delimiter_end = start
        while delimiter_end < end and value[delimiter_end] == "`":
            delimiter_end += 1
        delimiter_length = delimiter_end - start
        content_end = end
        if delimiter_length and value[max(delimiter_end, end - delimiter_length) : end] == (
            "`" * delimiter_length
        ):
            content_end = end - delimiter_length
        parts.append(value[delimiter_end:content_end])
        cursor = end
    parts.append(_strip_prose_markup(value[cursor:]))
    return " ".join("".join(parts).split()), explicit_anchor


def parse_headings(body: str, body_start_line: int) -> tuple[HeadingRecord, ...]:
    masked = mask_protected(body, include_frontmatter=False)
    code_spans = protected_spans(
        body,
        include_frontmatter=False,
        include_comments=False,
        include_indented=False,
    )
    headings: list[HeadingRecord] = []
    used: set[str] = set()
    counters: dict[str, int] = {}
    source_offset = 0
    code_index = 0
    for line_offset, (source_line, masked_line) in enumerate(
        zip(body.splitlines(keepends=True), masked.splitlines(keepends=True), strict=True)
    ):
        source_content = source_line.rstrip("\r\n")
        masked_content = masked_line.rstrip("\r\n")
        if _HEADING_RE.fullmatch(masked_content) is None:
            source_offset += len(source_line)
            continue
        line_end = source_offset + len(source_content)
        while code_index < len(code_spans) and code_spans[code_index][1] <= source_offset:
            code_index += 1
        visible = list(masked_content)
        line_code_spans: list[tuple[int, int]] = []
        span_index = code_index
        while span_index < len(code_spans) and code_spans[span_index][0] < line_end:
            start, end = code_spans[span_index]
            restore_start = max(start, source_offset)
            restore_end = min(end, line_end)
            visible[restore_start - source_offset : restore_end - source_offset] = body[
                restore_start:restore_end
            ]
            line_code_spans.append((restore_start - source_offset, restore_end - source_offset))
            span_index += 1
        match = _HEADING_RE.fullmatch("".join(visible))
        if match is None:
            source_offset += len(source_line)
            continue
        marker, raw_title = match.groups()
        title_start = match.start(2)
        title, explicit = _strip_heading_markup(
            raw_title,
            tuple(
                (max(start, title_start) - title_start, end - title_start)
                for start, end in line_code_spans
                if end > title_start
            ),
        )
        if explicit is not None:
            if explicit in used:
                raise ContentModelError(f"duplicate explicit heading anchor: {explicit}")
            anchor = explicit
        else:
            base = slugify(title)
            count = counters.get(base, 0)
            anchor = base if count == 0 else f"{base}_{count}"
            while anchor in used:
                count += 1
                anchor = f"{base}_{count}"
            counters[base] = count + 1
        used.add(anchor)
        headings.append(
            HeadingRecord(
                level=len(marker),
                text=title,
                anchor=anchor,
                line=body_start_line + line_offset,
                explicit=explicit is not None,
            )
        )
        source_offset += len(source_line)
    return tuple(headings)


def _lookup_keys(page: ContentPage) -> set[str]:
    relative = page.relative_path.as_posix()
    without_suffix = page.relative_path.with_suffix("").as_posix()
    return {
        page.page_id,
        page.title,
        *page.aliases,
        relative,
        without_suffix,
        page.relative_path.name,
        page.relative_path.stem,
    }


def build_content_index(root: Path) -> ContentIndex:
    """Read and validate a content tree without mutating it."""

    canonical_root = root.resolve(strict=True)
    paths = sorted(canonical_root.rglob("*.md"), key=lambda path: path.as_posix().casefold())
    pages: list[ContentPage] = []
    portable_paths: dict[str, str] = {}
    ids: dict[str, str] = {}
    urls: dict[str, str] = {}
    identity_keys: dict[str, str] = {}

    for path in paths:
        if path.is_symlink():
            raise ContentModelError(f"symlinked content page is forbidden: {path}")
        try:
            relative = path.resolve(strict=True).relative_to(canonical_root).as_posix()
        except ValueError as exc:
            raise ContentModelError(f"content page escapes root: {path}") from exc
        portable = portable_collision_key(relative)
        previous_path = portable_paths.get(portable)
        if previous_path is not None:
            raise ContentModelError(
                f"portable content path collision: {previous_path} and {relative}"
            )
        portable_paths[portable] = relative

        raw_bytes = path.read_bytes()
        if _LFS_POINTER_RE.fullmatch(raw_bytes):
            raise ContentModelError(f"Git LFS pointer is not a Markdown page: {relative}")
        try:
            source = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContentModelError(f"content page is not UTF-8: {relative}") from exc
        parsed = ContentPage.from_markdown(relative, source)
        page = replace(
            parsed,
            headings=parse_headings(parsed.body, parsed.body_start_line),
            source_path=path,
            source_sha256=hashlib.sha256(raw_bytes).hexdigest(),
        )
        previous_id = ids.get(page.page_id)
        if previous_id is not None:
            raise ContentModelError(f"duplicate path-derived page id: {previous_id} and {relative}")
        ids[page.page_id] = relative
        url_key = normalize_key(page.canonical_url)
        previous_url = urls.get(url_key)
        if previous_url is not None:
            raise ContentModelError(f"duplicate canonical URL: {previous_url} and {relative}")
        urls[url_key] = relative
        for identity in (page.title, *page.aliases):
            key = normalize_key(identity)
            previous_identity = identity_keys.get(key)
            if previous_identity is not None:
                raise ContentModelError(
                    f"duplicate title or alias: {identity!r} in {previous_identity} and {relative}"
                )
            identity_keys[key] = relative
        pages.append(page)

    by_path = {page.relative_path.as_posix(): page for page in pages}
    by_folded = {portable_collision_key(path): page for path, page in by_path.items()}
    lookup: dict[str, dict[str, ContentPage]] = {}
    for page in pages:
        for key in _lookup_keys(page):
            lookup.setdefault(normalize_key(key), {})[page.page_id] = page
    frozen_lookup = {
        key: tuple(sorted(value.values(), key=lambda page: page.relative_path.as_posix()))
        for key, value in lookup.items()
    }
    return ContentIndex(
        root=canonical_root,
        pages=tuple(pages),
        _by_path=MappingProxyType(by_path),
        _by_folded_path=MappingProxyType(by_folded),
        _lookup=MappingProxyType(frozen_lookup),
    )
