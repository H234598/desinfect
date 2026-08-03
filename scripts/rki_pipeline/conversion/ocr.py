#!/usr/bin/env python3
"""Bounded pagewise OCR with deterministic tool and review evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat

from scripts.rki_pipeline.conversion.base import (
    EnvironmentVariable,
    NamedDigest,
    OcrSettings,
    ToolEvidence,
)
from scripts.rki_pipeline.conversion.pdftotext import render_page_markers
from scripts.rki_pipeline.pdf_validation import (
    DEFAULT_PDF_LIMITS,
    PdfLimits,
    ProcessResult,
    ProcessRunner,
    ProcessRunnerError,
    Runner,
)


class OcrError(RuntimeError):
    """OCR output cannot satisfy deterministic conversion contracts."""


class OcrUnavailableError(OcrError):
    """A required OCR executable cannot be invoked."""


@dataclass(frozen=True, slots=True)
class OcrExtraction:
    pages: tuple[str, ...]
    markdown: str
    toolchain: tuple[ToolEvidence, ...]
    quality: str = field(init=False, default="needs_review")


_LANGUAGES = ("deu", "eng")
_ENVIRONMENT = (
    EnvironmentVariable("LANG", "C.UTF-8"),
    EnvironmentVariable("LC_ALL", "C.UTF-8"),
)
_PDFTOPPM_ARGUMENT_TEMPLATE = (
    "-f",
    "$PAGE",
    "-l",
    "$PAGE",
    "-singlefile",
    "-r",
    "300",
    "-gray",
    "$INPUT",
    "$OUTPUT_PREFIX",
)
_TESSERACT_ARGUMENT_TEMPLATE = (
    "$INPUT",
    "stdout",
    "-l",
    "deu+eng",
    "--psm",
    "3",
    "--oem",
    "1",
)
_PGM_HEADER_BYTES = 4096


def _version(result: ProcessResult, tool: str) -> str:
    try:
        value = (result.stdout + result.stderr).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OcrError(f"{tool}-Version ist nicht UTF-8") from exc
    version = value.strip()
    if not version:
        raise OcrError(f"{tool}-Version fehlt")
    return version


def _same_executable(probe: ProcessResult, invocation: ProcessResult, tool: str) -> None:
    if (
        probe.executable_sha256 != invocation.executable_sha256
        or not probe.argv
        or not invocation.argv
        or probe.argv[0] != invocation.argv[0]
    ):
        raise OcrError(f"{tool}-Executable driftete zwischen Aufrufen")


def _probe(
    runner: Runner,
    executable: str | Path,
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    limits: PdfLimits,
) -> ProcessResult:
    tool = Path(executable).name
    try:
        result = runner.run(executable, arguments, cwd=cwd, limits=limits)
    except ProcessRunnerError as exc:
        raise OcrUnavailableError(f"{tool} ist nicht verfügbar") from exc
    if result.returncode != 0:
        raise OcrUnavailableError(f"{tool} ist nicht verfügbar")
    return result


def _run(
    runner: Runner,
    executable: str | Path,
    arguments: tuple[str, ...],
    *,
    cwd: Path,
    limits: PdfLimits,
) -> ProcessResult:
    tool = Path(executable).name
    try:
        result = runner.run(executable, arguments, cwd=cwd, limits=limits)
    except ProcessRunnerError as exc:
        raise OcrError(f"{tool} konnte nicht ausgeführt werden") from exc
    if result.returncode != 0:
        raise OcrError(f"{tool} endete mit Status {result.returncode}")
    return result


def _raster_paths(raster_dir: Path, expected_page_count: int) -> tuple[Path, ...]:
    expected = tuple(
        raster_dir / f"page-{number:04d}.pgm"
        for number in range(1, expected_page_count + 1)
    )
    try:
        entries = tuple(sorted(os.scandir(raster_dir), key=lambda entry: entry.name))
    except OSError as exc:
        raise OcrError("OCR-Rasterausgaben sind nicht lesbar") from exc
    if tuple(entry.name for entry in entries) != tuple(path.name for path in expected):
        raise OcrError("Anzahl oder Namen der OCR-Rasterausgaben sind ungültig")
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise OcrError("OCR-Rasterausgabe ist nicht lesbar") from exc
        if entry.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise OcrError("OCR-Rasterausgabe ist keine reguläre Nicht-Symlink-Datei")
    return expected


def _pgm_tokens(data: bytes) -> tuple[bytes, ...]:
    tokens: list[bytes] = []
    index = 0
    while len(tokens) < 4:
        while index < len(data) and chr(data[index]).isspace():
            index += 1
        if index < len(data) and data[index] == ord("#"):
            newline = data.find(b"\n", index)
            if newline < 0:
                break
            index = newline + 1
            continue
        start = index
        while (
            index < len(data)
            and not chr(data[index]).isspace()
            and data[index] != ord("#")
        ):
            index += 1
        if start == index:
            break
        tokens.append(data[start:index])
    return tuple(tokens)


def _validate_pgm(path: Path, *, max_pixels: int) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OcrError("PGM-Raster ist nicht sicher lesbar") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OcrError("PGM-Raster ist keine reguläre Datei")
        header = os.read(descriptor, _PGM_HEADER_BYTES)
    finally:
        os.close(descriptor)
    tokens = _pgm_tokens(header)
    if len(tokens) != 4 or tokens[0] != b"P5":
        raise OcrError("PGM-Header ist ungültig")
    try:
        width, height, maximum = (int(token) for token in tokens[1:])
    except ValueError as exc:
        raise OcrError("PGM-Dimensionen sind ungültig") from exc
    if width <= 0 or height <= 0 or maximum != 255:
        raise OcrError("PGM-Dimensionen oder Farbtiefe sind ungültig")
    if width * height > max_pixels:
        raise OcrError("PGM-Rasterpixel überschreiten das Limit")


def _page_text(stdout: bytes) -> str:
    try:
        page = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise OcrError("Tesseract-Ausgabe ist nicht UTF-8") from exc
    if "\r" in page:
        raise OcrError("Tesseract-Ausgabe verletzt feste Unix-Zeilenenden")
    return page


def extract_text(
    source: Path,
    *,
    workdir: Path,
    expected_page_count: int,
    tessdata: tuple[NamedDigest, ...],
    runner: Runner | None = None,
    limits: PdfLimits = DEFAULT_PDF_LIMITS,
    pdftoppm_executable: str | Path = "pdftoppm",
    tesseract_executable: str | Path = "tesseract",
) -> OcrExtraction:
    """Raster every PDF page, OCR it, and always require semantic review."""

    if type(expected_page_count) is not int or expected_page_count <= 0:
        raise ValueError("expected_page_count muss positiv sein")
    if expected_page_count > limits.pages:
        raise OcrError("PDF-Seitenlimit für OCR überschritten")
    if (
        type(tessdata) is not tuple
        or not all(isinstance(item, NamedDigest) for item in tessdata)
        or tuple(item.name for item in tessdata) != _LANGUAGES
    ):
        raise ValueError("tessdata muss exakt deu und eng enthalten")
    settings = OcrSettings(
        dpi=300,
        color_mode="gray",
        psm=3,
        oem=1,
        languages=_LANGUAGES,
        tessdata=tessdata,
    )
    source_path = Path(source)
    if source_path.is_symlink() or not source_path.is_file():
        raise OcrError("OCR-Quelle ist keine reguläre Datei")
    cwd = Path(workdir)
    if cwd.is_symlink() or not cwd.is_dir():
        raise OcrError("OCR-Arbeitsverzeichnis ist kein reguläres Verzeichnis")
    raster_dir = cwd / "ocr-raster"
    try:
        raster_dir.mkdir(mode=0o700)
    except OSError as exc:
        raise OcrError("OCR-Rasterverzeichnis konnte nicht angelegt werden") from exc

    active_runner = runner if runner is not None else ProcessRunner()
    pdftoppm_probe = _probe(
        active_runner,
        pdftoppm_executable,
        ("-v",),
        cwd=cwd,
        limits=limits,
    )
    tesseract_probe = _probe(
        active_runner,
        tesseract_executable,
        ("--version",),
        cwd=cwd,
        limits=limits,
    )
    pdftoppm_version = _version(pdftoppm_probe, "pdftoppm")
    tesseract_version = _version(tesseract_probe, "tesseract")

    for number in range(1, expected_page_count + 1):
        output_prefix = raster_dir / f"page-{number:04d}"
        arguments = (
            "-f",
            str(number),
            "-l",
            str(number),
            "-singlefile",
            "-r",
            "300",
            "-gray",
            source_path.as_posix(),
            output_prefix.as_posix(),
        )
        result = _run(
            active_runner,
            pdftoppm_executable,
            arguments,
            cwd=cwd,
            limits=limits,
        )
        _same_executable(pdftoppm_probe, result, "pdftoppm")

    rasters = _raster_paths(raster_dir, expected_page_count)
    for raster in rasters:
        _validate_pgm(raster, max_pixels=limits.raster_pixels)

    pages: list[str] = []
    for raster in rasters:
        arguments = (
            raster.as_posix(),
            "stdout",
            "-l",
            "deu+eng",
            "--psm",
            "3",
            "--oem",
            "1",
        )
        result = _run(
            active_runner,
            tesseract_executable,
            arguments,
            cwd=cwd,
            limits=limits,
        )
        _same_executable(tesseract_probe, result, "tesseract")
        pages.append(_page_text(result.stdout))

    page_tuple = tuple(pages)
    toolchain = (
        ToolEvidence(
            name="pdftoppm",
            version_output=pdftoppm_version,
            executable_sha256=pdftoppm_probe.executable_sha256,
            argv=("pdftoppm", *_PDFTOPPM_ARGUMENT_TEMPLATE),
            environment=_ENVIRONMENT,
            ocr_settings=None,
        ),
        ToolEvidence(
            name="tesseract",
            version_output=tesseract_version,
            executable_sha256=tesseract_probe.executable_sha256,
            argv=("tesseract", *_TESSERACT_ARGUMENT_TEMPLATE),
            environment=_ENVIRONMENT,
            ocr_settings=settings,
        ),
    )
    return OcrExtraction(
        pages=page_tuple,
        markdown=render_page_markers(page_tuple),
        toolchain=toolchain,
    )
