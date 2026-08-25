"""Convert supported Obsidian callouts only in generated Markdown copies."""

from __future__ import annotations

import re
import unicodedata

from scripts.web.content_model import protected_spans


_CALLOUT_START_RE = re.compile(
    r"^(?P<indent>[ \t]*)>\s*\[!(?P<kind>[A-Za-z0-9_-]+)\]"
    r"(?P<fold>[+-])?\s*(?P<title>.*?)[ \t]*(?P<newline>\r?\n)?$"
)
_CALLOUT_BODY_RE = re.compile(r"^(?P<indent>[ \t]*)>\s?(?P<body>.*?)(?P<newline>\r?\n)?$")
CALLOUT_TYPES = {
    "abstract": "abstract",
    "summary": "abstract",
    "tldr": "abstract",
    "info": "info",
    "todo": "info",
    "tip": "tip",
    "hint": "tip",
    "important": "important",
    "success": "success",
    "check": "success",
    "done": "success",
    "question": "question",
    "help": "question",
    "faq": "question",
    "warning": "warning",
    "caution": "warning",
    "attention": "warning",
    "failure": "failure",
    "fail": "failure",
    "missing": "failure",
    "danger": "danger",
    "error": "danger",
    "bug": "bug",
    "example": "example",
    "quote": "quote",
    "cite": "quote",
    "note": "note",
    "evidence": "evidence",
    "rights": "rights",
    "historical": "historical",
    "safety": "safety",
}
DEFAULT_TITLES = {
    "abstract": "Zusammenfassung",
    "info": "Hinweis",
    "tip": "Tipp",
    "important": "Wichtig",
    "success": "Erfolg",
    "question": "Frage",
    "warning": "Warnung",
    "failure": "Fehler",
    "danger": "Gefahr",
    "bug": "Fehlerbild",
    "example": "Beispiel",
    "quote": "Zitat",
    "note": "Hinweis",
    "evidence": "Evidenz",
    "rights": "Rechte",
    "historical": "Historischer Kontext",
    "safety": "Sicherheit",
}


def _safe_title(value: str) -> str | None:
    if len(value) > 200 or unicodedata.normalize("NFC", value) != value:
        return None
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        return None
    escaped = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = escaped.replace("\\", "\\\\")
    for marker in "[]`*_!":
        escaped = escaped.replace(marker, "\\" + marker)
    return escaped.replace('"', "&quot;")


def _protected_line_starts(text: str, lines: list[str]) -> frozenset[int]:
    ranges = protected_spans(text, include_indented=True)
    protected: set[int] = set()
    offset = 0
    range_index = 0
    for line_index, line in enumerate(lines):
        while range_index < len(ranges) and ranges[range_index][1] <= offset:
            range_index += 1
        if range_index < len(ranges) and ranges[range_index][0] <= offset < ranges[range_index][1]:
            protected.add(line_index)
        offset += len(line)
    return frozenset(protected)


def _copy_literal_block(
    lines: list[str],
    index: int,
    output: list[str],
    indent: str,
) -> int:
    output.append(lines[index])
    index += 1
    while index < len(lines):
        body = _CALLOUT_BODY_RE.fullmatch(lines[index])
        if body is None or body.group("indent") != indent:
            break
        output.append(lines[index])
        index += 1
    return index


def convert_obsidian_callouts_for_web(text: str) -> str:
    """Return converted copy; unknown or malformed callouts stay literal."""

    lines = text.splitlines(keepends=True)
    protected = _protected_line_starts(text, lines)
    output: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if index in protected:
            output.append(line)
            index += 1
            continue
        match = _CALLOUT_START_RE.fullmatch(line)
        if match is None:
            output.append(line)
            index += 1
            continue
        source_kind = match.group("kind").casefold()
        target_kind = CALLOUT_TYPES.get(source_kind)
        if target_kind is None:
            index = _copy_literal_block(lines, index, output, match.group("indent"))
            continue
        raw_title = match.group("title").strip() or DEFAULT_TITLES[target_kind]
        title = _safe_title(raw_title)
        if title is None:
            index = _copy_literal_block(lines, index, output, match.group("indent"))
            continue
        fold = match.group("fold")
        directive = "???+" if fold == "+" else ("???" if fold == "-" else "!!!")
        indent = match.group("indent")
        newline = match.group("newline") or "\n"
        output.append(f'{indent}{directive} {target_kind} "{title}"{newline}')
        index += 1
        while index < len(lines):
            body = _CALLOUT_BODY_RE.fullmatch(lines[index])
            if body is None or body.group("indent") != indent:
                break
            body_newline = body.group("newline") or ("\n" if lines[index].endswith("\n") else "")
            output.append(f"{indent}    {body.group('body')}{body_newline}")
            index += 1
    return "".join(output)
