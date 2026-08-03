from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from importlib import import_module
from pathlib import Path

import pytest


SOURCE_SHA256 = "a" * 64
OPTIONS_SHA256 = "b" * 64
FINGERPRINT_SHA256 = "f294e43694711cb2db6dbc71bdf26805ca504131fe6db2ae4b30e894ce3eedab"


def _base_module():
    return import_module("scripts.rki_pipeline.conversion.base")


def _evidence():
    base = _base_module()
    tool = base.ToolEvidence(
        name="pdftotext",
        version_output="pdftotext version 25.05.0",
        executable_sha256="d" * 64,
        argv=("pdftotext", "-layout", "$INPUT", "$OUTPUT"),
        environment=(
            base.EnvironmentVariable("LANG", "C.UTF-8"),
            base.EnvironmentVariable("LC_ALL", "C.UTF-8"),
        ),
        ocr_settings=None,
    )
    runtime = base.RuntimeEvidence(
        platform="linux-x86_64",
        libc="glibc-2.39",
        shared_libraries=(base.NamedDigest("libpoppler.so.140", "e" * 64),),
        fonts=(base.NamedDigest("DejaVuSans.ttf", "f" * 64),),
    )
    return tool, runtime


def test_conversion_fingerprint_has_hand_checked_canonical_value() -> None:
    base = _base_module()
    tool, runtime = _evidence()

    actual = base.conversion_fingerprint(
        source_sha256=SOURCE_SHA256,
        converter="pdftotext-layout",
        converter_version="25.05.0",
        options_sha256=OPTIONS_SHA256,
        toolchain=(tool,),
        runtime=runtime,
    )

    assert actual == FINGERPRINT_SHA256


def test_conversion_fingerprint_changes_for_every_reproducibility_input() -> None:
    base = _base_module()
    tool, runtime = _evidence()
    baseline = base.conversion_fingerprint(
        source_sha256=SOURCE_SHA256,
        converter="pdftotext-layout",
        converter_version="25.05.0",
        options_sha256=OPTIONS_SHA256,
        toolchain=(tool,),
        runtime=runtime,
    )
    second_tool = replace(tool, name="pdfinfo")
    variants = (
        {"source_sha256": "0" * 64},
        {"converter": "pdftotext-plain"},
        {"converter_version": "25.05.1"},
        {"options_sha256": "1" * 64},
        {"toolchain": (replace(tool, executable_sha256="2" * 64),)},
        {"toolchain": (tool, second_tool)},
        {"toolchain": (second_tool, tool)},
        {"runtime": replace(runtime, libc="musl-1.2.5")},
        {
            "runtime": replace(
                runtime,
                shared_libraries=(base.NamedDigest("libpoppler.so.140", "3" * 64),),
            )
        },
        {
            "runtime": replace(
                runtime,
                fonts=(base.NamedDigest("DejaVuSans.ttf", "4" * 64),),
            )
        },
    )

    for changed in variants:
        values = {
            "source_sha256": SOURCE_SHA256,
            "converter": "pdftotext-layout",
            "converter_version": "25.05.0",
            "options_sha256": OPTIONS_SHA256,
            "toolchain": (tool,),
            "runtime": runtime,
        }
        values.update(changed)
        assert base.conversion_fingerprint(**values) != baseline


def test_conversion_identity_binds_document_bitstream_and_fingerprint() -> None:
    base = _base_module()

    assert base.conversion_id(
        "rki-176904-12345-v2",
        "rki-bitstream-" + "1" * 64,
        FINGERPRINT_SHA256,
    ) == "conv-a7632e1e638770f8420f94dca8d7e842bd4ab0845a78a5ad9e05d73090f0980b"


def test_evidence_is_immutable_sorted_and_contains_no_machine_paths() -> None:
    base = _base_module()
    tool, runtime = _evidence()

    with pytest.raises(FrozenInstanceError):
        tool.name = "changed"
    with pytest.raises(base.EvidenceError, match="Maschinenpfad"):
        replace(tool, argv=("pdftotext", "/tmp/input.pdf"))
    with pytest.raises(base.EvidenceError, match="Maschinenpfad"):
        replace(tool, version_output="pdftotext from /usr/local/bin")
    with pytest.raises(base.EvidenceError, match="sortiert"):
        replace(tool, environment=tuple(reversed(tool.environment)))
    with pytest.raises(base.EvidenceError, match="sortiert"):
        replace(
            runtime,
            fonts=(
                base.NamedDigest("z.ttf", "1" * 64),
                base.NamedDigest("a.ttf", "2" * 64),
            ),
        )


@pytest.mark.parametrize(
    "argument",
    (
        "--input=/tmp/input.pdf",
        '-I/usr/include',
        "file:///tmp/input.pdf",
    ),
)
def test_evidence_rejects_embedded_machine_paths(argument: str) -> None:
    base = _base_module()
    tool, _runtime = _evidence()

    with pytest.raises(base.EvidenceError, match="Maschinenpfad"):
        replace(tool, argv=("pdftotext", argument))


@pytest.mark.parametrize("scheme", ("http", "https"))
def test_evidence_allows_web_urls_but_only_fixed_public_environment(scheme: str) -> None:
    base = _base_module()
    tool, _runtime = _evidence()

    assert replace(
        tool,
        version_output=f"documentation {scheme}://poppler.freedesktop.org/releases.html",
    ).version_output.endswith("releases.html")
    assert base.EnvironmentVariable("TZ", "UTC").to_dict() == {"name": "TZ", "value": "UTC"}

    with pytest.raises(base.EvidenceError, match="Umgebungsvariable"):
        base.EnvironmentVariable("GH_TOKEN", "secret")
    with pytest.raises(base.EvidenceError, match="Umgebungsvariable"):
        base.EnvironmentVariable("LANG", "de_DE.UTF-8")


def test_ocr_evidence_captures_all_fingerprint_inputs() -> None:
    base = _base_module()
    settings = base.OcrSettings(
        dpi=300,
        color_mode="gray",
        psm=3,
        oem=1,
        languages=("deu", "eng"),
        tessdata=(
            base.NamedDigest("deu", "1" * 64),
            base.NamedDigest("eng", "2" * 64),
        ),
    )

    assert settings.to_dict() == {
        "dpi": 300,
        "color_mode": "gray",
        "psm": 3,
        "oem": 1,
        "languages": ["deu", "eng"],
        "tessdata": [
            {"name": "deu", "sha256": "1" * 64},
            {"name": "eng", "sha256": "2" * 64},
        ],
    }


def test_quality_accepts_boundary_and_reports_metrics() -> None:
    quality = import_module("scripts.rki_pipeline.conversion.quality")

    result = quality.assess_quality(("a" * 40, "b" * 40), expected_page_count=2)

    assert result.quality == "good"
    assert result.character_count == 80
    assert result.replacement_ratio == 0.0
    assert result.empty_pages == ()
    assert result.reasons == ()


def test_quality_accepts_exact_replacement_ratio_boundary() -> None:
    quality = import_module("scripts.rki_pipeline.conversion.quality")

    result = quality.assess_quality(("a" * 99 + "\ufffd",), expected_page_count=1)

    assert result.quality == "good"
    assert result.replacement_ratio == 0.01
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("pages", "expected_page_count", "reason"),
    [
        (("a" * 39,), 1, "too_few_characters"),
        (("a" * 79, "b"), 2, "too_few_characters"),
        (("a" * 98 + "\ufffd\ufffd",), 1, "replacement_ratio"),
        (("a" * 40, "  "), 2, "empty_pages"),
        (("a" * 40,), 2, "page_count_mismatch"),
    ],
)
def test_quality_marks_each_threshold_violation_for_review(
    pages: tuple[str, ...], expected_page_count: int, reason: str
) -> None:
    quality = import_module("scripts.rki_pipeline.conversion.quality")

    result = quality.assess_quality(pages, expected_page_count=expected_page_count)

    assert result.quality == "needs_review"
    assert reason in result.reasons


def test_quality_rejects_non_positive_expected_page_count() -> None:
    quality = import_module("scripts.rki_pipeline.conversion.quality")

    with pytest.raises(ValueError, match="positiv"):
        quality.assess_quality(("text",), expected_page_count=0)


class _PdftotextRunner:
    def __init__(self, output: bytes, *, drift: bool = False) -> None:
        self.output = output
        self.drift = drift
        self.calls: list[tuple[str | Path, tuple[str, ...], Path]] = []

    def run(self, executable, arguments, *, cwd, limits):
        validation = import_module("scripts.rki_pipeline.pdf_validation")
        self.calls.append((executable, arguments, cwd))
        if arguments == ("-v",):
            return validation.ProcessResult(
                argv=("/usr/bin/pdftotext", "-v"),
                executable_sha256="d" * 64,
                returncode=0,
                stdout=b"",
                stderr=b"pdftotext version 26.01.0\n",
            )
        return validation.ProcessResult(
            argv=("/usr/bin/pdftotext", *arguments),
            executable_sha256=("e" if self.drift else "d") * 64,
            returncode=0,
            stdout=self.output,
            stderr=b"",
        )


def test_pdftotext_uses_fixed_argv_and_exact_page_markers(tmp_path: Path) -> None:
    pdftotext = import_module("scripts.rki_pipeline.conversion.pdftotext")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"unused by injected runner")
    runner = _PdftotextRunner(b"First page\n\fSecond page\n\f")

    result = pdftotext.extract_text(
        source,
        workdir=tmp_path,
        expected_page_count=2,
        runner=runner,
    )

    assert result.pages == ("First page\n", "Second page\n")
    assert result.markdown == (
        "<!-- rki-page: 1 -->\nFirst page\n\n"
        "<!-- rki-page: 2 -->\nSecond page\n"
    )
    assert runner.calls == [
        ("pdftotext", ("-v",), tmp_path),
        (
            "pdftotext",
            (
                "-layout",
                "-enc",
                "UTF-8",
                "-eol",
                "unix",
                source.as_posix(),
                "-",
            ),
            tmp_path,
        ),
    ]
    assert result.tool.argv == (
        "pdftotext",
        "-layout",
        "-enc",
        "UTF-8",
        "-eol",
        "unix",
        "$INPUT",
        "-",
    )
    assert result.tool.version_output == "pdftotext version 26.01.0"


@pytest.mark.parametrize(
    "output",
    (
        b"one page only\f",
        b"first\fsecond",
        b"first\fsecond\fextra\f",
        b"first\xff\fsecond\f",
    ),
)
def test_pdftotext_rejects_page_or_encoding_drift(
    tmp_path: Path,
    output: bytes,
) -> None:
    pdftotext = import_module("scripts.rki_pipeline.conversion.pdftotext")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"unused")

    with pytest.raises(pdftotext.TextExtractionError):
        pdftotext.extract_text(
            source,
            workdir=tmp_path,
            expected_page_count=2,
            runner=_PdftotextRunner(output),
        )


def test_pdftotext_preserves_empty_pages_for_quality_gate(tmp_path: Path) -> None:
    pdftotext = import_module("scripts.rki_pipeline.conversion.pdftotext")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"unused")

    result = pdftotext.extract_text(
        source,
        workdir=tmp_path,
        expected_page_count=2,
        runner=_PdftotextRunner(b"\f\f"),
    )

    assert result.pages == ("", "")
    assert result.markdown.count("<!-- rki-page:") == 2


def test_pdftotext_rejects_marker_shaped_source_text(tmp_path: Path) -> None:
    pdftotext = import_module("scripts.rki_pipeline.conversion.pdftotext")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"unused")

    with pytest.raises(pdftotext.TextExtractionError, match="Seitenmarker"):
        pdftotext.extract_text(
            source,
            workdir=tmp_path,
            expected_page_count=1,
            runner=_PdftotextRunner(b"payload <!-- rki-page: 9 -->\f"),
        )


def test_pdftotext_rejects_executable_drift_between_version_and_run(
    tmp_path: Path,
) -> None:
    pdftotext = import_module("scripts.rki_pipeline.conversion.pdftotext")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"unused")

    with pytest.raises(pdftotext.TextExtractionError, match="Executable"):
        pdftotext.extract_text(
            source,
            workdir=tmp_path,
            expected_page_count=1,
            runner=_PdftotextRunner(b"text\f", drift=True),
        )
