#!/usr/bin/env python3
"""Bounded pagewise OCR with deterministic tool and review evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import stat

from scripts.rki_pipeline.conversion.base import (
    EnvironmentVariable,
    NamedDigest,
    OcrSettings,
    ToolEvidence,
)
from scripts.rki_pipeline.conversion.pdftotext import (
    TextExtractionError,
    render_page_markers,
)
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
    "--tessdata-dir",
    "$TESSDATA",
    "-l",
    "deu+eng",
    "--psm",
    "3",
    "--oem",
    "1",
)
_PGM_HEADER_BYTES = 4096
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


def _snapshot_tessdata(
    paths: tuple[Path, ...],
    *,
    workdir: Path,
    maximum_bytes: int,
) -> tuple[Path, tuple[NamedDigest, ...]]:
    expected_names = tuple(f"{language}.traineddata" for language in _LANGUAGES)
    if (
        type(paths) is not tuple
        or not all(isinstance(path, Path) for path in paths)
        or tuple(path.name for path in paths) != expected_names
    ):
        raise ValueError("tessdata muss exakt deu.traineddata und eng.traineddata enthalten")
    snapshot_dir = workdir / "ocr-tessdata"
    try:
        snapshot_dir.mkdir(mode=0o700)
    except OSError as exc:
        raise OcrError("OCR-Tessdata-Snapshot konnte nicht angelegt werden") from exc
    evidence: list[NamedDigest] = []
    for language, source in zip(_LANGUAGES, paths, strict=True):
        try:
            source_fd = os.open(source, _FILE_FLAGS)
        except OSError as exc:
            raise OcrError(f"Tessdata {language} ist nicht sicher lesbar") from exc
        target_fd: int | None = None
        try:
            metadata = os.fstat(source_fd)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
                raise OcrError(f"Tessdata {language} verletzt Dateigrenzen")
            target_fd = os.open(
                snapshot_dir / source.name,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o400,
            )
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(source_fd, min(1024 * 1024, maximum_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise OcrError(f"Tessdata {language} überschreitet Dateigrenze")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    if written <= 0:
                        raise OcrError("Tessdata-Snapshot-Write machte keinen Fortschritt")
                    view = view[written:]
            os.fsync(target_fd)
            evidence.append(NamedDigest(language, digest.hexdigest()))
        except OcrError:
            raise
        except OSError as exc:
            raise OcrError(
                f"Tessdata {language} konnte nicht sicher als Snapshot geschrieben werden"
            ) from exc
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(source_fd)
    return snapshot_dir, tuple(evidence)


def _assert_tessdata_unchanged(
    snapshot_dir: Path,
    evidence: tuple[NamedDigest, ...],
    *,
    maximum_bytes: int,
) -> None:
    for item in evidence:
        path = snapshot_dir / f"{item.name}.traineddata"
        try:
            descriptor = os.open(path, _FILE_FLAGS)
        except OSError as exc:
            raise OcrError(f"Tessdata-Snapshot {item.name} ist nicht lesbar") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_bytes:
                raise OcrError(f"Tessdata-Snapshot {item.name} driftete")
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, maximum_bytes + 1 - total))
                if not chunk:
                    break
                total += len(chunk)
                if total > maximum_bytes:
                    raise OcrError(f"Tessdata-Snapshot {item.name} driftete")
                digest.update(chunk)
            if digest.hexdigest() != item.sha256:
                raise OcrError(f"Tessdata-Snapshot {item.name} driftete")
        except OcrError:
            raise
        except OSError as exc:
            raise OcrError(
                f"Tessdata-Snapshot {item.name} konnte nicht sicher geprüft werden"
            ) from exc
        finally:
            os.close(descriptor)


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
    tessdata: tuple[Path, ...],
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
    source_path = Path(source)
    if source_path.is_symlink() or not source_path.is_file():
        raise OcrError("OCR-Quelle ist keine reguläre Datei")
    cwd = Path(workdir)
    if cwd.is_symlink() or not cwd.is_dir():
        raise OcrError("OCR-Arbeitsverzeichnis ist kein reguläres Verzeichnis")
    tessdata_dir, tessdata_evidence = _snapshot_tessdata(
        tessdata,
        workdir=cwd,
        maximum_bytes=limits.source_bytes,
    )
    settings = OcrSettings(
        dpi=300,
        color_mode="gray",
        psm=3,
        oem=1,
        languages=_LANGUAGES,
        tessdata=tessdata_evidence,
    )
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
            "--tessdata-dir",
            tessdata_dir.as_posix(),
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

    _assert_tessdata_unchanged(
        tessdata_dir,
        tessdata_evidence,
        maximum_bytes=limits.source_bytes,
    )

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
    try:
        markdown = render_page_markers(page_tuple)
    except TextExtractionError as exc:
        raise OcrError("OCR-Ausgabe enthält reservierten Seitenmarker") from exc
    return OcrExtraction(pages=page_tuple, markdown=markdown, toolchain=toolchain)
