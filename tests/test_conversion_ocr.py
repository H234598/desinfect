from __future__ import annotations

import hashlib
from importlib import import_module
from pathlib import Path

import pytest


def _ocr_module():
    return import_module("scripts.rki_pipeline.conversion.ocr")


def _result(
    executable: str,
    arguments: tuple[str, ...],
    *,
    sha256: str,
    stdout: bytes = b"",
    stderr: bytes = b"",
):
    validation = import_module("scripts.rki_pipeline.pdf_validation")
    return validation.ProcessResult(
        argv=(f"/usr/bin/{executable}", *arguments),
        executable_sha256=sha256,
        returncode=0,
        stdout=stdout,
        stderr=stderr,
    )


class _OcrRunner:
    locale_environment = (("LANG", "C.UTF-8"), ("LC_ALL", "C.UTF-8"))

    def __init__(
        self,
        page_text: tuple[bytes, ...],
        *,
        pgm_headers: tuple[bytes, ...] | None = None,
        extra_output: bool = False,
        missing_output: int | None = None,
        symlink_output: int | None = None,
        drift_tool: str | None = None,
        drift_tessdata: bool = False,
        missing_tool: str | None = None,
    ) -> None:
        self.page_text = page_text
        self.pgm_headers = pgm_headers or tuple(
            b"P5\n2 3\n255\n" for _ in page_text
        )
        self.extra_output = extra_output
        self.missing_output = missing_output
        self.symlink_output = symlink_output
        self.drift_tool = drift_tool
        self.drift_tessdata = drift_tessdata
        self.missing_tool = missing_tool
        self.calls: list[tuple[str | Path, tuple[str, ...], Path]] = []

    def run(self, executable, arguments, *, cwd, limits):
        validation = import_module("scripts.rki_pipeline.pdf_validation")
        name = Path(executable).name
        self.calls.append((executable, arguments, cwd))
        if name == self.missing_tool and arguments in {("-v",), ("--version",)}:
            raise validation.ProcessRunnerError(f"{name} fehlt")
        if arguments == ("-v",):
            return _result(
                name,
                arguments,
                sha256="a" * 64,
                stderr=b"pdftoppm version 26.01.0\n",
            )
        if arguments == ("--version",):
            return _result(
                name,
                arguments,
                sha256="b" * 64,
                stdout=b"tesseract 5.5.1\nleptonica 1.85.0\n",
            )
        if name == "pdftoppm":
            page_number = int(arguments[1])
            if page_number != self.missing_output:
                output = Path(arguments[-1]).with_suffix(".pgm")
                if page_number == self.symlink_output:
                    target = cwd / f"outside-{page_number}.pgm"
                    target.write_bytes(self.pgm_headers[page_number - 1] + b"\0" * 6)
                    output.symlink_to(target)
                else:
                    output.write_bytes(
                        self.pgm_headers[page_number - 1] + b"\0" * 6
                    )
            if self.extra_output and page_number == len(self.page_text):
                (Path(arguments[-1]).parent / "page-extra.pgm").write_bytes(
                    b"P5\n1 1\n255\n\0"
                )
            sha256 = ("c" if self.drift_tool == name else "a") * 64
            return _result(name, arguments, sha256=sha256)
        page_number = int(Path(arguments[0]).stem.split("-")[-1])
        if self.drift_tessdata:
            model = Path(arguments[3]) / "deu.traineddata"
            model.chmod(0o600)
            model.write_bytes(b"changed model")
        sha256 = ("c" if self.drift_tool == name else "b") * 64
        return _result(
            name,
            arguments,
            sha256=sha256,
            stdout=self.page_text[page_number - 1],
        )


def _tessdata(tmp_path: Path) -> tuple[Path, Path]:
    model_dir = tmp_path / "models"
    model_dir.mkdir(exist_ok=True)
    paths = tuple(model_dir / f"{language}.traineddata" for language in ("deu", "eng"))
    for path in paths:
        path.write_bytes(f"{path.stem} model".encode())
    return paths


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"unused by injected runner")
    return source


def test_ocr_uses_pagewise_fixed_tools_and_forces_review(tmp_path: Path) -> None:
    ocr = _ocr_module()
    source = _source(tmp_path)
    runner = _OcrRunner((b"Erste Seite\n", b"Second page\n"))

    result = ocr.extract_text(
        source,
        workdir=tmp_path,
        expected_page_count=2,
        tessdata=_tessdata(tmp_path),
        runner=runner,
    )

    raster_dir = tmp_path / "ocr-raster"
    assert result.pages == ("Erste Seite\n", "Second page\n")
    assert result.markdown == (
        "<!-- rki-page: 1 -->\nErste Seite\n\n"
        "<!-- rki-page: 2 -->\nSecond page\n"
    )
    assert result.quality == "needs_review"
    assert runner.calls == [
        ("pdftoppm", ("-v",), tmp_path),
        ("tesseract", ("--version",), tmp_path),
        (
            "pdftoppm",
            (
                "-f",
                "1",
                "-l",
                "1",
                "-singlefile",
                "-r",
                "300",
                "-gray",
                source.as_posix(),
                (raster_dir / "page-0001").as_posix(),
            ),
            tmp_path,
        ),
        (
            "pdftoppm",
            (
                "-f",
                "2",
                "-l",
                "2",
                "-singlefile",
                "-r",
                "300",
                "-gray",
                source.as_posix(),
                (raster_dir / "page-0002").as_posix(),
            ),
            tmp_path,
        ),
        (
            "tesseract",
            (
                (raster_dir / "page-0001.pgm").as_posix(),
                "stdout",
                "--tessdata-dir",
                (tmp_path / "ocr-tessdata").as_posix(),
                "-l",
                "deu+eng",
                "--psm",
                "3",
                "--oem",
                "1",
            ),
            tmp_path,
        ),
        (
            "tesseract",
            (
                (raster_dir / "page-0002.pgm").as_posix(),
                "stdout",
                "--tessdata-dir",
                (tmp_path / "ocr-tessdata").as_posix(),
                "-l",
                "deu+eng",
                "--psm",
                "3",
                "--oem",
                "1",
            ),
            tmp_path,
        ),
    ]
    assert tuple(tool.name for tool in result.toolchain) == (
        "pdftoppm",
        "tesseract",
    )
    assert tuple(tool.version_output for tool in result.toolchain) == (
        "pdftoppm version 26.01.0",
        "tesseract 5.5.1\nleptonica 1.85.0",
    )
    assert result.toolchain[0].argv == (
        "pdftoppm",
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
    assert result.toolchain[1].argv == (
        "tesseract",
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
    assert result.toolchain[1].ocr_settings.to_dict() == {
        "dpi": 300,
        "color_mode": "gray",
        "psm": 3,
        "oem": 1,
        "languages": ["deu", "eng"],
        "tessdata": [
            {"name": "deu", "sha256": hashlib.sha256(b"deu model").hexdigest()},
            {"name": "eng", "sha256": hashlib.sha256(b"eng model").hexdigest()},
        ],
    }


def test_ocr_result_cannot_silently_claim_good_quality() -> None:
    ocr = _ocr_module()

    with pytest.raises(TypeError, match="quality"):
        ocr.OcrExtraction(
            pages=(),
            markdown="",
            toolchain=(),
            quality="good",
        )


@pytest.mark.parametrize(
    ("runner", "message"),
    [
        (_OcrRunner((b"text", b"text"), missing_output=2), "Anzahl"),
        (_OcrRunner((b"text",), extra_output=True), "Anzahl"),
        (_OcrRunner((b"text",), symlink_output=1), "regulär"),
    ],
)
def test_ocr_requires_exact_regular_non_symlink_rasters(
    tmp_path: Path,
    runner: _OcrRunner,
    message: str,
) -> None:
    ocr = _ocr_module()

    with pytest.raises(ocr.OcrError, match=message):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=len(runner.page_text),
            tessdata=_tessdata(tmp_path),
            runner=runner,
        )

    assert all(Path(call[0]).name != "tesseract" for call in runner.calls[2:])


@pytest.mark.parametrize("header", (b"P6\n2 3\n255\n", b"P5\nnope 3\n255\n"))
def test_ocr_rejects_invalid_pgm_before_tesseract(
    tmp_path: Path,
    header: bytes,
) -> None:
    ocr = _ocr_module()
    runner = _OcrRunner((b"text",), pgm_headers=(header,))

    with pytest.raises(ocr.OcrError, match="PGM"):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=1,
            tessdata=_tessdata(tmp_path),
            runner=runner,
        )

    assert len(runner.calls) == 3


def test_ocr_rejects_raster_pixel_limit_before_tesseract(tmp_path: Path) -> None:
    ocr = _ocr_module()
    validation = import_module("scripts.rki_pipeline.pdf_validation")
    runner = _OcrRunner((b"text",), pgm_headers=(b"P5\n11 10\n255\n",))

    with pytest.raises(ocr.OcrError, match="Rasterpixel"):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=1,
            tessdata=_tessdata(tmp_path),
            runner=runner,
            limits=validation.PdfLimits(raster_pixels=100),
        )

    assert len(runner.calls) == 3


@pytest.mark.parametrize("tool", ("pdftoppm", "tesseract"))
def test_ocr_reports_missing_tool_as_unavailable(tmp_path: Path, tool: str) -> None:
    ocr = _ocr_module()

    with pytest.raises(ocr.OcrUnavailableError, match=tool):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=1,
            tessdata=_tessdata(tmp_path),
            runner=_OcrRunner((b"text",), missing_tool=tool),
        )


@pytest.mark.parametrize("tool", ("pdftoppm", "tesseract"))
def test_ocr_rejects_executable_drift(tmp_path: Path, tool: str) -> None:
    ocr = _ocr_module()

    with pytest.raises(ocr.OcrError, match="Executable"):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=1,
            tessdata=_tessdata(tmp_path),
            runner=_OcrRunner((b"text",), drift_tool=tool),
        )


def test_ocr_rejects_tessdata_snapshot_drift(tmp_path: Path) -> None:
    ocr = _ocr_module()

    with pytest.raises(ocr.OcrError, match="Tessdata-Snapshot deu driftete"):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=1,
            tessdata=_tessdata(tmp_path),
            runner=_OcrRunner((b"text",), drift_tessdata=True),
        )


def test_ocr_maps_tessdata_snapshot_fsync_failure(tmp_path: Path, monkeypatch) -> None:
    ocr = _ocr_module()

    def fail_fsync(_descriptor):
        raise OSError("fsync failed")

    monkeypatch.setattr(ocr.os, "fsync", fail_fsync)

    with pytest.raises(ocr.OcrError, match=r"Tessdata deu.*Snapshot"):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=1,
            tessdata=_tessdata(tmp_path),
            runner=_OcrRunner((b"text",)),
        )


def test_ocr_maps_reserved_marker_to_ocr_error(tmp_path: Path) -> None:
    ocr = _ocr_module()

    with pytest.raises(ocr.OcrError, match="reservierten Seitenmarker"):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=1,
            tessdata=_tessdata(tmp_path),
            runner=_OcrRunner((b"<!-- rki-page: 1 -->\n",)),
        )


@pytest.mark.parametrize(
    "names",
    (("deu",), ("deu", "eng", "fra"), ("eng", "deu")),
)
def test_ocr_requires_exact_deu_eng_tessdata(
    tmp_path: Path,
    names: tuple[str, ...],
) -> None:
    ocr = _ocr_module()
    runner = _OcrRunner((b"text",))
    tessdata = tuple(tmp_path / f"{name}.traineddata" for name in names)

    with pytest.raises(ValueError, match=r"deu.*eng"):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=1,
            tessdata=tessdata,
            runner=runner,
        )

    assert runner.calls == []


def test_ocr_enforces_page_limit_before_running_tools(tmp_path: Path) -> None:
    ocr = _ocr_module()
    validation = import_module("scripts.rki_pipeline.pdf_validation")
    runner = _OcrRunner((b"one", b"two"))

    with pytest.raises(ocr.OcrError, match="Seitenlimit"):
        ocr.extract_text(
            _source(tmp_path),
            workdir=tmp_path,
            expected_page_count=2,
            tessdata=_tessdata(tmp_path),
            runner=runner,
            limits=validation.PdfLimits(pages=1),
        )

    assert runner.calls == []
