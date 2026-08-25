"""Deterministic Wikilink conversion for generated Markdown copies."""

from __future__ import annotations

from collections.abc import Iterable
import posixpath
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from scripts.web.content_index import ContentIndex, build_content_index
from scripts.web.link_resolution import resolve_occurrence
from scripts.web.link_types import (
    IMAGE_SUFFIXES,
    LinkError,
    LinkOccurrence,
    Resolution,
    scan_wikilinks,
)


def _relative_posix(source: PurePosixPath, target: PurePosixPath) -> str:
    return posixpath.relpath(target.as_posix(), start=source.parent.as_posix())


def _encoded_link(path: str, anchor: str | None = None) -> str:
    encoded = quote(path, safe="/._~-@")
    return encoded + ("#" + quote(anchor, safe="-._~:") if anchor is not None else "")


def _escape_generated_label(value: str) -> str:
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = escaped.replace("\\", "\\\\")
    for marker in "[]`*_!":
        escaped = escaped.replace(marker, "\\" + marker)
    return escaped


def _replace_occurrences(
    text: str,
    replacements: Iterable[tuple[LinkOccurrence, str]],
) -> str:
    fragments: list[str] = []
    cursor = len(text)
    for occurrence, replacement in sorted(
        replacements,
        key=lambda item: item[0].start,
        reverse=True,
    ):
        fragments.append(text[occurrence.end : cursor])
        fragments.append(replacement)
        cursor = occurrence.start
    fragments.append(text[:cursor])
    return "".join(reversed(fragments))


def _target_generated_path(index: ContentIndex, resolution: Resolution) -> PurePosixPath:
    if resolution.page is not None:
        return resolution.page.generated_path
    if resolution.path is None:
        raise LinkError("resolved target has no path")
    return PurePosixPath(resolution.path.relative_to(index.root).as_posix())


def _web_replacement(
    index: ContentIndex,
    source_generated_path: PurePosixPath,
    occurrence: LinkOccurrence,
    resolution: Resolution,
) -> str:
    if not resolution.ok or resolution.path is None:
        raise LinkError(resolution.message or f"invalid internal link: {occurrence.raw}")
    target = _target_generated_path(index, resolution)
    relative = _relative_posix(source_generated_path, target)
    destination = _encoded_link(relative, resolution.anchor)
    label = _escape_generated_label(occurrence.label)
    if occurrence.embed and resolution.path.suffix.casefold() in IMAGE_SUFFIXES:
        return f"![{label}]({destination})"
    return f"[{label}]({destination})"


def convert_for_web(
    text: str,
    source: Path,
    root: Path,
    *,
    index: ContentIndex | None = None,
) -> str:
    """Convert a source string without mutating it or any file."""

    catalog = index or build_content_index(root)
    source_page = catalog.page_for_path(source)
    if source_page is None:
        raise LinkError(f"source page is not indexed: {source}")
    replacements = [
        (
            occurrence,
            _web_replacement(
                catalog,
                source_page.generated_path,
                occurrence,
                resolve_occurrence(catalog, occurrence),
            ),
        )
        for occurrence in scan_wikilinks(text, source)
    ]
    return _replace_occurrences(text, replacements)
