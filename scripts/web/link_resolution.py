"""Fail-closed internal link resolution within one content root."""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath
import re
import unicodedata

from scripts.web.content_index import ContentIndex
from scripts.web.content_model import ContentPage, normalize_key, slugify
from scripts.web.link_types import ASSET_SUFFIXES, IMAGE_SUFFIXES, LinkOccurrence, Resolution


_EXTERNAL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _candidate_relatives(source: ContentPage, target: str) -> tuple[tuple[str, ...], bool]:
    if (
        target.startswith(("/", "\\"))
        or _WINDOWS_DRIVE_RE.match(target)
        or "\\" in target
        or "?" in target
        or unicodedata.normalize("NFC", target) != target
    ):
        return (), True
    combined = posixpath.normpath(posixpath.join(source.relative_path.parent.as_posix(), target))
    if combined == ".." or combined.startswith("../"):
        return (), True
    if combined == ".":
        return (), False
    raw = PurePosixPath(combined)
    candidates: list[PurePosixPath]
    if raw.suffix:
        candidates = [raw]
    else:
        candidates = [raw, raw.with_suffix(".md"), raw / "INDEX.md", raw / "README.md"]
    return tuple(dict.fromkeys(candidate.as_posix() for candidate in candidates)), False


def _safe_asset(index: ContentIndex, relative: str) -> tuple[Path | None, bool]:
    candidate = index.root / relative
    if candidate.suffix.casefold() not in ASSET_SUFFIXES:
        return None, False
    if candidate.is_symlink():
        return None, True
    if not candidate.is_file():
        return None, False
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(index.root)
    except (OSError, ValueError):
        return None, True
    if candidate.suffix.casefold() == ".md":
        return None, False
    return resolved, False


def _direct_targets(
    index: ContentIndex,
    source: ContentPage,
    target: str,
) -> tuple[dict[str, ContentPage], dict[str, Path], bool, bool]:
    relative_candidates, escaped = _candidate_relatives(source, target)
    pages: dict[str, ContentPage] = {}
    assets: dict[str, Path] = {}
    case_mismatch = False
    for relative in relative_candidates:
        page = index.page_for_path(relative)
        if page is not None:
            pages[page.page_id] = page
            continue
        folded = index.folded_page_for_path(relative)
        if folded is not None:
            pages[folded.page_id] = folded
            case_mismatch = True
            continue
        asset, asset_escaped = _safe_asset(index, relative)
        escaped = escaped or asset_escaped
        if asset is not None:
            assets[asset.as_posix()] = asset
    return pages, assets, escaped, case_mismatch


def _resolve_heading(page: ContentPage, requested: str) -> Resolution:
    key = normalize_key(requested)
    slug = slugify(requested)
    matches = {
        (heading.anchor, heading.text): heading
        for heading in page.headings
        if heading.anchor == requested
        or (not heading.explicit and (normalize_key(heading.text) == key or heading.anchor == slug))
    }
    path = page.source_path
    if len(matches) == 1:
        heading = next(iter(matches.values()))
        return Resolution(
            "ok",
            f"section:{page.page_id}#{heading.anchor}",
            path=path,
            page=page,
            heading=heading.text,
            anchor=heading.anchor,
        )
    if len(matches) > 1:
        return Resolution(
            "ambiguous-heading",
            page.page_id,
            path=path,
            page=page,
            candidates=tuple(sorted(f"#{heading.anchor}" for heading in matches.values())),
            message=f"heading is ambiguous in {page.relative_path}: {requested}",
        )
    return Resolution(
        "missing-heading",
        page.page_id,
        path=path,
        page=page,
        message=f"heading not found in {page.relative_path}: {requested}",
    )


def resolve_occurrence(index: ContentIndex, occurrence: LinkOccurrence) -> Resolution:
    source = index.page_for_path(occurrence.source)
    if source is None:
        return Resolution("missing-source", None, message="source page is not indexed")
    target = occurrence.target
    if target and _EXTERNAL_SCHEME_RE.match(target):
        return Resolution(
            "external", None, message=f"external Wikilink target is forbidden: {target}"
        )

    if not target:
        pages, assets, escaped, case_mismatch = {source.page_id: source}, {}, False, False
    else:
        pages, assets, escaped, case_mismatch = _direct_targets(index, source, target)
        if escaped:
            return Resolution("root-escape", None, message=f"target escapes content root: {target}")
        if not pages and not assets:
            pages = {page.page_id: page for page in index.lookup_pages(target)}

    candidates = tuple(
        sorted(
            [page.relative_path.as_posix() for page in pages.values()]
            + [path.relative_to(index.root).as_posix() for path in assets.values()]
        )
    )
    if len(pages) + len(assets) > 1:
        return Resolution(
            "ambiguous",
            None,
            candidates=candidates,
            message=f"target is ambiguous: {target}",
        )
    if pages:
        page = next(iter(pages.values()))
        if case_mismatch:
            return Resolution(
                "case-mismatch",
                page.page_id,
                path=page.source_path,
                page=page,
                candidates=(page.relative_path.as_posix(),),
                message=f"target case does not match: {target}",
            )
        if occurrence.embed:
            return Resolution(
                "markdown-transclusion",
                page.page_id,
                path=page.source_path,
                page=page,
                message="Markdown transclusion is forbidden",
            )
        if occurrence.heading is not None:
            return _resolve_heading(page, occurrence.heading)
        return Resolution("ok", page.page_id, path=page.source_path, page=page)
    if assets:
        path = next(iter(assets.values()))
        target_id = "asset:" + path.relative_to(index.root).as_posix()
        if occurrence.heading is not None:
            return Resolution(
                "missing-heading",
                target_id,
                path=path,
                message="assets do not have Markdown headings",
            )
        if occurrence.embed and path.suffix.casefold() not in IMAGE_SUFFIXES:
            return Resolution(
                "unsupported-embed",
                target_id,
                path=path,
                message="only image assets may be embedded",
            )
        return Resolution("ok", target_id, path=path)
    return Resolution("missing-document", None, message=f"target not found: {target}")
