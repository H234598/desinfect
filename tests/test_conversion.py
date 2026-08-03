from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from importlib import import_module

import pytest


SOURCE_SHA256 = "a" * 64
OPTIONS_SHA256 = "b" * 64
FINGERPRINT_SHA256 = "316fea4aac5ed8691386869d43dc2d89a92f658f516232ed89fbb129b528b234"


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
        options_sha256=OPTIONS_SHA256,
        toolchain=(tool,),
        runtime=runtime,
    )
    second_tool = replace(tool, name="pdfinfo")
    variants = (
        {"source_sha256": "0" * 64},
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
    ) == "conv-41ed0e27745b85bd988ee5fba26182fada5f9c8eacdfd919b2085275eea33637"


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


@pytest.mark.parametrize(
    ("pages", "expected_page_count", "reason"),
    [
        (("a" * 39,), 1, "too_few_characters"),
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
