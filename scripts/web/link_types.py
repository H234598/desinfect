"""Typed Wikilink occurrences and protected-source scanner."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import unicodedata

from scripts.web.content_model import ContentPage, mask_protected


IMAGE_SUFFIXES = frozenset({".avif", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"})


class LinkError(ValueError):
    """An internal link cannot be scanned, resolved, or converted safely."""


@dataclass(frozen=True, slots=True)
class LinkTarget:
    path: Path
    heading: str | None = None


@dataclass(frozen=True, slots=True)
class LinkOccurrence:
    source: Path
    raw: str
    target: str
    heading: str | None
    label: str
    embed: bool
    line: int
    column: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class Resolution:
    status: str
    target_id: str | None
    path: Path | None = None
    page: ContentPage | None = None
    heading: str | None = None
    anchor: str | None = None
    candidates: tuple[str, ...] = ()
    message: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _safe_component(value: str, *, name: str, allow_empty: bool = False) -> str:
    if not allow_empty and not value:
        raise LinkError(f"{name} must not be empty")
    if value != value.strip():
        raise LinkError(f"{name} has surrounding whitespace")
    if len(value) > 1000:
        raise LinkError(f"{name} is too long")
    if unicodedata.normalize("NFC", value) != value:
        raise LinkError(f"{name} must use canonical NFC")
    if any(unicodedata.category(character) == "Cs" for character in value):
        raise LinkError(f"{name} contains a Unicode surrogate")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise LinkError(f"{name} contains a control character")
    return value


def split_link(raw: str) -> tuple[str, str | None, str]:
    """Split ``target#heading|label`` once per supported separator."""

    if raw.count("|") > 1:
        raise LinkError(f"malformed Wikilink label separator: {raw!r}")
    target_heading, separator, label_raw = raw.partition("|")
    if target_heading.count("#") > 1:
        raise LinkError(f"malformed Wikilink heading separator: {raw!r}")
    target_raw, heading_separator, heading_raw = target_heading.partition("#")
    target = _safe_component(target_raw.strip(), name="Wikilink target", allow_empty=True)
    heading = (
        _safe_component(heading_raw.strip(), name="Wikilink heading") if heading_separator else None
    )
    if separator:
        label = _safe_component(label_raw.strip(), name="Wikilink label")
    else:
        label = heading or Path(target).stem or target or "Link"
    return target, heading, label


def scan_wikilinks(text: str, source: Path) -> tuple[LinkOccurrence, ...]:
    """Find Wikilinks outside frontmatter, code, comments, and indented code."""

    masked = mask_protected(text)
    occurrences: list[LinkOccurrence] = []
    cursor = 0
    line_number = 1
    line_start = 0
    line_cursor = 0
    while (opening := masked.find("[[", cursor)) >= 0:
        newline = masked.find("\n", opening + 2)
        limit = len(masked) if newline < 0 else newline
        closing_start = -1
        closing_end = -1
        search = opening + 2
        unmatched_brackets = 0
        while (bracket := masked.find("]", search, limit)) >= 0:
            unmatched_brackets += masked.count("[", search, bracket)
            run_end = bracket
            while run_end < limit and masked[run_end] == "]":
                run_end += 1
            delimiter_start = bracket + min(unmatched_brackets, run_end - bracket)
            if run_end - delimiter_start >= 2:
                closing_start = delimiter_start
                closing_end = delimiter_start + 2
                break
            unmatched_brackets = max(0, unmatched_brackets - (run_end - bracket))
            search = run_end
        if closing_start < 0:
            raise LinkError(f"malformed Wikilink at offset {opening}")
        body = masked[opening + 2 : closing_start]
        if "[[" in body:
            raise LinkError(f"nested Wikilink at offset {opening}")
        target, heading, label = split_link(body)
        start = opening - 1 if opening > 0 and masked[opening - 1] == "!" else opening
        while (line_break := text.find("\n", line_cursor, start)) >= 0:
            line_number += 1
            line_start = line_break + 1
            line_cursor = line_start
        line_cursor = start
        occurrences.append(
            LinkOccurrence(
                source=source,
                raw=text[start:closing_end],
                target=target,
                heading=heading,
                label=label,
                embed=start != opening,
                line=line_number,
                column=start - line_start + 1,
                start=start,
                end=closing_end,
            )
        )
        cursor = closing_end
    return tuple(occurrences)
