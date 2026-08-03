#!/usr/bin/env python3
"""Deterministic Poppler text extraction with explicit page boundaries."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scripts.rki_pipeline.conversion.base import EnvironmentVariable, ToolEvidence
from scripts.rki_pipeline.pdf_validation import (
    DEFAULT_PDF_LIMITS,
    PdfLimits,
    ProcessRunner,
    ProcessRunnerError,
    Runner,
)


class TextExtractionError(RuntimeError):
    """Poppler output cannot satisfy deterministic text contracts."""


@dataclass(frozen=True, slots=True)
class TextExtraction:
    pages: tuple[str, ...]
    markdown: str
    tool: ToolEvidence


_ARGUMENT_TEMPLATE = (
    "-layout",
    "-enc",
    "UTF-8",
    "-eol",
    "unix",
    "$INPUT",
    "-",
)


def _version(result_stdout: bytes, result_stderr: bytes) -> str:
    try:
        value = (result_stdout + result_stderr).decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise TextExtractionError("pdftotext-Version ist nicht UTF-8") from exc
    if not value:
        raise TextExtractionError("pdftotext-Version fehlt")
    return value


def _pages(stdout: bytes, expected_page_count: int) -> tuple[str, ...]:
    if not stdout.endswith(b"\f"):
        raise TextExtractionError("pdftotext-Ausgabe endet nicht mit Seitenumbruch")
    raw_pages = stdout[:-1].split(b"\f")
    if len(raw_pages) != expected_page_count:
        raise TextExtractionError(
            "pdftotext-Seitenzahl stimmt nicht mit pdfinfo überein"
        )
    try:
        pages = tuple(page.decode("utf-8", errors="strict") for page in raw_pages)
    except UnicodeDecodeError as exc:
        raise TextExtractionError("pdftotext-Ausgabe ist nicht UTF-8") from exc
    if any("\r" in page for page in pages):
        raise TextExtractionError("pdftotext-Ausgabe verletzt feste Unix-Zeilenenden")
    return pages


def render_page_markers(pages: tuple[str, ...]) -> str:
    """Render exactly one stable marker for every positional PDF page."""

    blocks = []
    for number, page in enumerate(pages, start=1):
        blocks.append(f"<!-- rki-page: {number} -->\n{page.rstrip(chr(10))}")
    return "\n\n".join(blocks) + "\n"


def extract_text(
    source: Path,
    *,
    workdir: Path,
    expected_page_count: int,
    runner: Runner | None = None,
    limits: PdfLimits = DEFAULT_PDF_LIMITS,
    executable: str | Path = "pdftotext",
) -> TextExtraction:
    """Run one version probe and one fixed text extraction invocation."""

    if type(expected_page_count) is not int or expected_page_count <= 0:
        raise ValueError("expected_page_count muss positiv sein")
    source_path = Path(source)
    if source_path.is_symlink() or not source_path.is_file():
        raise TextExtractionError("pdftotext-Quelle ist keine reguläre Datei")
    cwd = Path(workdir)
    active_runner = runner if runner is not None else ProcessRunner()
    try:
        version_result = active_runner.run(
            executable,
            ("-v",),
            cwd=cwd,
            limits=limits,
        )
        arguments = (*_ARGUMENT_TEMPLATE[:-2], source_path.as_posix(), "-")
        extraction_result = active_runner.run(
            executable,
            arguments,
            cwd=cwd,
            limits=limits,
        )
    except ProcessRunnerError as exc:
        raise TextExtractionError("pdftotext konnte nicht ausgeführt werden") from exc
    if version_result.executable_sha256 != extraction_result.executable_sha256:
        raise TextExtractionError("pdftotext-Executable driftete zwischen Aufrufen")

    pages = _pages(extraction_result.stdout, expected_page_count)
    tool = ToolEvidence(
        name="pdftotext",
        version_output=_version(version_result.stdout, version_result.stderr),
        executable_sha256=extraction_result.executable_sha256,
        argv=("pdftotext", *_ARGUMENT_TEMPLATE),
        environment=(
            EnvironmentVariable("LANG", "C.UTF-8"),
            EnvironmentVariable("LC_ALL", "C.UTF-8"),
        ),
        ocr_settings=None,
    )
    return TextExtraction(
        pages=pages,
        markdown=render_page_markers(pages),
        tool=tool,
    )
