"""Hardened content primitives adapted from the frozen Cheatsheets blueprint."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path, PurePosixPath
import re
from types import MappingProxyType
from typing import Any
import unicodedata

import yaml
from yaml.composer import ComposerError
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent
from yaml.resolver import BaseResolver

from scripts.rki_pipeline.io_utils import UnsafePathError, normalize_posix_path


PAGE_ROLES = frozenset(
    {
        "landing",
        "axis",
        "bulletin",
        "instruction",
        "method",
        "maintenance",
        "generated-wrapper",
    }
)
PAGE_ID_NAMESPACE = "desinfect-page-v1"
_MAX_YAML_DEPTH = 16
_MAX_YAML_NODES = 1024
_ID_PREFIX = "p_"
_METADATA_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_FENCE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})(?P<info>[^\r\n]*)(?:\r?\n)?$")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}(?P<marker>`{3,}|~{3,})[ \t]*(?:\r?\n)?$")
_INVALID_BACKTICK_FENCE_RE = re.compile(r"^ {0,3}`{3,}")

FenceState = tuple[str, int]


class ContentModelError(ValueError):
    """Content violates a deterministic website-source trust boundary."""


@dataclass(frozen=True, slots=True)
class HeadingRecord:
    level: int
    text: str
    anchor: str
    line: int
    explicit: bool = False


@dataclass(frozen=True, slots=True)
class ContentPage:
    """One immutable Markdown page addressed relative to ``content/``."""

    relative_path: PurePosixPath
    role: str
    title: str
    aliases: tuple[str, ...]
    metadata: Mapping[str, object]
    source: str = field(repr=False)
    body: str = field(repr=False)
    body_start_line: int
    page_id: str
    canonical_url: str
    headings: tuple[HeadingRecord, ...] = ()
    source_path: Path | None = None
    source_sha256: str = ""

    @property
    def path(self) -> str:
        return self.relative_path.as_posix()

    @property
    def generated_path(self) -> PurePosixPath:
        return self.relative_path

    @classmethod
    def from_markdown(cls, path: str | PurePosixPath, source: str) -> ContentPage:
        relative = PurePosixPath(_canonical_path(path, name="content path", markdown=True))
        metadata, body, body_start_line = _parse_frontmatter(source)
        if "id" in metadata:
            raise ContentModelError(
                "frontmatter.id is forbidden; identity is derived from content path"
            )
        role = _safe_text(metadata.get("role"), name="frontmatter.role", maximum=40)
        if role not in PAGE_ROLES:
            raise ContentModelError(f"unknown or non-canonical frontmatter.role: {role!r}")
        title = _safe_text(metadata.get("title"), name="frontmatter.title", maximum=1000)
        aliases_value = metadata.get("aliases", ())
        if not isinstance(aliases_value, tuple):
            raise ContentModelError("frontmatter.aliases must be a YAML list")
        aliases = tuple(
            _safe_text(value, name=f"frontmatter.aliases[{index}]", maximum=200)
            for index, value in enumerate(aliases_value)
        )
        identity_keys = [normalize_key(title), *(normalize_key(alias) for alias in aliases)]
        if len(identity_keys) != len(set(identity_keys)):
            raise ContentModelError("duplicate title or alias within page")
        return cls(
            relative_path=relative,
            role=role,
            title=title,
            aliases=aliases,
            metadata=metadata,
            source=source,
            body=body,
            body_start_line=body_start_line,
            page_id=page_id_from_path(relative),
            canonical_url=canonical_url_from_path(relative, role),
        )


class _UniqueKeyLoader(yaml.SafeLoader):
    def __init__(self, stream: str) -> None:
        self._compose_depth = 0
        self._composed_nodes = 0
        super().__init__(stream)

    def compose_node(
        self,
        parent: yaml.nodes.Node | None,
        index: object,
    ) -> yaml.nodes.Node:
        if self.check_event(AliasEvent):
            event = self.peek_event()
            raise ComposerError(None, None, "YAML aliases are not allowed", event.start_mark)
        self._compose_depth += 1
        self._composed_nodes += 1
        try:
            if self._compose_depth > _MAX_YAML_DEPTH:
                event = self.peek_event()
                raise ComposerError(
                    None,
                    None,
                    f"frontmatter exceeds depth budget of {_MAX_YAML_DEPTH} nesting levels",
                    event.start_mark,
                )
            if self._composed_nodes > _MAX_YAML_NODES:
                event = self.peek_event()
                raise ComposerError(
                    None,
                    None,
                    f"frontmatter exceeds node budget of {_MAX_YAML_NODES}",
                    event.start_mark,
                )
            return super().compose_node(parent, index)
        finally:
            self._compose_depth -= 1


def _construct_unique_mapping(
    loader: _UniqueKeyLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[str, Any]:
    loader.flatten_mapping(node)
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConstructorError(
                "while constructing frontmatter",
                node.start_mark,
                "frontmatter keys must be strings",
                key_node.start_mark,
            )
        if key in result:
            raise ConstructorError(
                "while constructing frontmatter",
                node.start_mark,
                f"duplicate frontmatter key: {key}",
                key_node.start_mark,
            )
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeyLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def normalize_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def slugify(value: str) -> str:
    value = re.sub(r"\s*\{[^{}]+\}\s*$", "", value)
    value = re.sub(r"[`*~]", "", value)
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-") or "section"


def page_id_from_path(path: str | Path | PurePosixPath) -> str:
    relative = _canonical_path(path, name="content path", markdown=True)
    payload = f"{PAGE_ID_NAMESPACE}\0{relative}".encode("utf-8")
    return _ID_PREFIX + hashlib.sha256(payload).hexdigest()[:16]


def canonical_url_from_path(path: str | Path | PurePosixPath, role: str) -> str:
    relative = PurePosixPath(_canonical_path(path, name="content path", markdown=True))
    if role == "landing":
        return "/"
    if relative.name.casefold() in {"index.md", "readme.md"}:
        parent = relative.parent.as_posix()
        return "/" if parent == "." else f"/{parent}/"
    return f"/{relative.with_suffix('').as_posix().strip('/')}/"


def _safe_text(value: object, *, name: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise ContentModelError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ContentModelError(f"{name} must be non-empty without surrounding whitespace")
    if len(value) > maximum:
        raise ContentModelError(f"{name} exceeds {maximum} characters")
    if unicodedata.normalize("NFC", value) != value:
        raise ContentModelError(f"{name} must use canonical NFC")
    if any(unicodedata.category(character) == "Cs" for character in value):
        raise ContentModelError(f"{name} contains a Unicode surrogate")
    if any(unicodedata.category(character) == "Cc" for character in value):
        raise ContentModelError(f"{name} contains a control character")
    return value


def _canonical_path(
    value: object,
    *,
    name: str,
    markdown: bool = False,
) -> str:
    if isinstance(value, Path | PurePosixPath):
        value = value.as_posix()
    raw = _safe_text(value, name=name, maximum=1000)
    try:
        normalized = normalize_posix_path(raw)
    except UnsafePathError as exc:
        raise ContentModelError(f"unsafe {name}: {raw!r}") from exc
    if normalized != raw:
        raise ContentModelError(f"non-canonical {name}: {raw!r}")
    if markdown and not normalized.endswith(".md"):
        raise ContentModelError(f"{name} must end in .md")
    return normalized


def _safe_metadata_key(value: object) -> str:
    key = _safe_text(value, name="frontmatter key", maximum=100)
    if not _METADATA_KEY_RE.fullmatch(key):
        raise ContentModelError(f"unsafe frontmatter key: {key!r}")
    return key


def _freeze_metadata(value: object, *, name: str) -> object:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContentModelError(f"{name} contains a non-finite number")
        return value
    if isinstance(value, str):
        return _safe_text(value, name=name, maximum=4000)
    if isinstance(value, list):
        return tuple(
            _freeze_metadata(item, name=f"{name}[{index}]") for index, item in enumerate(value)
        )
    if isinstance(value, dict):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            safe_key = _safe_metadata_key(key)
            frozen[safe_key] = _freeze_metadata(item, name=f"{name}.{safe_key}")
        return MappingProxyType(frozen)
    raise ContentModelError(f"{name} has an unsupported YAML value")


def _parse_frontmatter(source: str) -> tuple[Mapping[str, object], str, int]:
    if not isinstance(source, str):
        raise ContentModelError("Markdown source must be a string")
    match = _FRONTMATTER_RE.match(source)
    if match is None:
        raise ContentModelError("Markdown source must start with closed YAML frontmatter")
    try:
        parsed = yaml.load(match.group(1), Loader=_UniqueKeyLoader)
    except ContentModelError:
        raise
    except (RecursionError, ValueError, OverflowError, yaml.YAMLError) as exc:
        raise ContentModelError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ContentModelError("YAML frontmatter must be a mapping")
    metadata: dict[str, object] = {}
    for raw_key, raw_value in parsed.items():
        key = _safe_metadata_key(raw_key)
        if key.endswith("_path") or key == "source_pdf":
            metadata[key] = _canonical_path(raw_value, name="source path")
        else:
            metadata[key] = _freeze_metadata(raw_value, name=f"frontmatter.{key}")
    return MappingProxyType(metadata), source[match.end() :], source[: match.end()].count("\n") + 1


def advance_fence_state(
    line: str,
    state: FenceState | None,
) -> tuple[FenceState | None, bool]:
    if state is not None:
        match = _FENCE_CLOSE_RE.fullmatch(line)
        if match is not None:
            marker = match.group("marker")
            if marker[0] == state[0] and len(marker) >= state[1]:
                return None, True
        return state, True
    match = _FENCE_RE.fullmatch(line)
    if match is None:
        return None, False
    marker = match.group("marker")
    if marker[0] == "`" and "`" in match.group("info"):
        return None, False
    return (marker[0], len(marker)), True


def _backtick_run_end(source: str, start: int, limit: int) -> int:
    end = start
    while end < limit and source[end] == "`":
        end += 1
    return end


def _is_backslash_escaped(source: str, position: int, lower_bound: int) -> bool:
    backslashes = 0
    cursor = position - 1
    while cursor >= lower_bound and source[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _blocked_end(
    position: int,
    ranges: tuple[tuple[int, int], ...],
    starts: tuple[int, ...],
) -> int | None:
    index = bisect_right(starts, position) - 1
    if index >= 0 and position < ranges[index][1]:
        return ranges[index][1]
    return None


def _inline_spans(
    source: str,
    start: int,
    end: int,
    blocked: tuple[tuple[int, int], ...],
    blocked_starts: tuple[int, ...],
) -> list[tuple[int, int]]:
    runs: list[tuple[int, int]] = []
    positions_by_length: dict[int, list[int]] = {}
    cursor = start
    while (position := source.find("`", cursor, end)) >= 0:
        blocked_end = _blocked_end(position, blocked, blocked_starts)
        if blocked_end is not None:
            cursor = blocked_end
            continue
        run_end = _backtick_run_end(source, position, end)
        runs.append((position, run_end))
        positions_by_length.setdefault(run_end - position, []).append(position)
        cursor = run_end

    spans: list[tuple[int, int]] = []
    run_index = 0
    while run_index < len(runs):
        position, opening_end = runs[run_index]
        if _is_backslash_escaped(source, position, start):
            run_index += 1
            continue
        matching_positions = positions_by_length[opening_end - position]
        match_index = bisect_right(matching_positions, position)
        if match_index >= len(matching_positions):
            run_index += 1
            continue
        closing_end = matching_positions[match_index] + opening_end - position
        spans.append((position, closing_end))
        run_index += 1
        while run_index < len(runs) and runs[run_index][0] < closing_end:
            run_index += 1
    return spans


def _code_spans(source: str) -> tuple[tuple[int, int], ...]:
    lines = source.splitlines(keepends=True)
    fenced: list[tuple[int, int]] = []
    prose: list[tuple[int, int]] = []
    invalid_lines: list[tuple[int, int]] = []
    fence: FenceState | None = None
    fence_start = 0
    prose_start = 0
    offset = 0
    for line in lines:
        line_end = offset + len(line)
        if fence is not None:
            fence, _is_fenced = advance_fence_state(line, fence)
            if fence is None:
                fenced.append((fence_start, line_end))
                prose_start = line_end
            offset = line_end
            continue
        opening, is_fenced = advance_fence_state(line, None)
        if is_fenced:
            if prose_start < offset:
                prose.append((prose_start, offset))
            fence = opening
            fence_start = offset
        elif _INVALID_BACKTICK_FENCE_RE.match(line):
            invalid_lines.append((offset, line_end))
        offset = line_end
    if fence is not None:
        fenced.append((fence_start, len(source)))
    elif prose_start < len(source):
        prose.append((prose_start, len(source)))
    blocked = tuple(invalid_lines)
    blocked_starts = tuple(start for start, _end in blocked)
    inline: list[tuple[int, int]] = []
    for start, end in prose:
        inline.extend(_inline_spans(source, start, end, blocked, blocked_starts))
    return tuple(sorted((*fenced, *inline)))


def _merge_spans(spans: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _comment_spans(
    source: str,
    blocked: tuple[tuple[int, int], ...],
) -> tuple[tuple[int, int], ...]:
    comments: list[tuple[int, int]] = []
    blocked_starts = tuple(start for start, _end in blocked)
    cursor = 0
    while (opening := source.find("<!--", cursor)) >= 0:
        blocked_end = _blocked_end(opening, blocked, blocked_starts)
        if blocked_end is not None:
            cursor = blocked_end
            continue
        closing = source.find("-->", opening + 4)
        end = len(source) if closing < 0 else closing + 3
        comments.append((opening, end))
        cursor = end
    return tuple(comments)


def protected_spans(
    source: str,
    *,
    include_frontmatter: bool = True,
    include_comments: bool = True,
    include_indented: bool = True,
) -> tuple[tuple[int, int], ...]:
    """Return byte-stable character ranges scanners must not interpret."""

    frontmatter = _FRONTMATTER_RE.match(source) if include_frontmatter else None
    content_start = frontmatter.end() if frontmatter is not None else 0
    spans = [
        (start + content_start, end + content_start)
        for start, end in _code_spans(source[content_start:])
    ]
    if frontmatter is not None:
        spans.append((0, frontmatter.end()))
    base = _merge_spans(spans)
    if include_comments:
        spans.extend(_comment_spans(source, base))
    if include_indented:
        offset = 0
        existing = _merge_spans(spans)
        for line in source.splitlines(keepends=True):
            line_end = offset + len(line)
            if line.startswith(("    ", "\t")) and not any(
                start < line_end and end > offset for start, end in existing
            ):
                spans.append((offset, line_end))
            offset = line_end
    return _merge_spans(spans)


def mask_protected(
    source: str,
    *,
    include_frontmatter: bool = True,
    include_comments: bool = True,
    include_indented: bool = True,
) -> str:
    chars = list(source)
    for start, end in protected_spans(
        source,
        include_frontmatter=include_frontmatter,
        include_comments=include_comments,
        include_indented=include_indented,
    ):
        for index in range(start, end):
            if chars[index] not in "\r\n":
                chars[index] = " "
    return "".join(chars)
