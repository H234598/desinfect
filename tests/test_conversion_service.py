"""Atomic, rights-bound PDF conversion orchestration."""
from __future__ import annotations
from contextlib import contextmanager

import hashlib
from importlib import import_module
import json
from pathlib import Path
import shutil

import pytest

from scripts.rki_pipeline import rights
from scripts.rki_pipeline.conversion.base import (
    EnvironmentVariable,
    NamedDigest,
    OcrSettings,
    RuntimeEvidence,
    ToolEvidence,
)
from scripts.rki_pipeline.conversion.ocr import OcrExtraction, OcrUnavailableError
from scripts.rki_pipeline.pdf_validation import PdfLimits, ProcessResult
from scripts.rki_pipeline.rights import (
    load_rights_authority,
    load_rights_policy,
    resolve_rights,
)
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.storage.base import RightsStorageAuthorizer, StorageIntent


PDF = (Path(__file__).parent / "fixtures" / "pdf" / "minimal.pdf").read_bytes()
SOURCE_ID = "rki:176904/12345.2"
DOCUMENT_ID = "rki-176904-12345-v2"
BITSTREAM_ID = "rki-bitstream-" + "1" * 64


class _ConversionRunner:
    def __init__(
        self,
        text_output: bytes,
        *,
        pdfinfo_sha256: str = "a" * 64,
        pdfinfo_version: bytes = b"pdfinfo version 26.01.0\n",
    ) -> None:
        self.text_output = text_output
        self.pdfinfo_sha256 = pdfinfo_sha256
        self.pdfinfo_version = pdfinfo_version
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def run(self, executable, arguments, *, cwd, limits):
        del limits
        name = Path(executable).name
        self.calls.append((name, arguments))
        if name == "pdfinfo" and arguments == ("-v",):
            return ProcessResult(
                argv=("/usr/bin/pdfinfo", "-v"),
                executable_sha256=self.pdfinfo_sha256,
                returncode=0,
                stdout=b"",
                stderr=self.pdfinfo_version,
            )
        if name == "pdfinfo":
            return ProcessResult(
                argv=("/usr/bin/pdfinfo", *arguments),
                executable_sha256=self.pdfinfo_sha256,
                returncode=0,
                stdout=b"Pages: 1\nEncrypted: no\n",
                stderr=b"",
            )
        if arguments == ("-v",):
            return ProcessResult(
                argv=("/usr/bin/pdftotext", "-v"),
                executable_sha256="b" * 64,
                returncode=0,
                stdout=b"",
                stderr=b"pdftotext version 26.01.0\n",
            )
        assert Path(arguments[-2]).parent == cwd
        return ProcessResult(
            argv=("/usr/bin/pdftotext", *arguments),
            executable_sha256="b" * 64,
            returncode=0,
            stdout=self.text_output,
            stderr=b"",
        )


def _runtime(font_sha: str = "d" * 64) -> RuntimeEvidence:
    return RuntimeEvidence(
        platform="linux-x86_64",
        libc="glibc-2.39",
        shared_libraries=(NamedDigest("libpoppler.so.140", "c" * 64),),
        fonts=(NamedDigest("DejaVuSans.ttf", font_sha),),
    )


def _authorizer_and_intent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rights_source_sha256: str | None = None,
    logical_key: str | None = None,
) -> tuple[RightsStorageAuthorizer, StorageIntent]:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    source_sha256 = hashlib.sha256(PDF).hexdigest()
    authorized_sha256 = rights_source_sha256 or source_sha256
    register = tmp_path / "rights-register.yml"
    register.write_text(
        f'''schema_version: 1
decisions:
  - source_id: "{SOURCE_ID}"
    source_sha256: "{authorized_sha256}"
    state: "approved"
    basis: "Reviewed synthetic conversion fixture"
    reviewed_by: "Test Reviewer"
    reviewed_at: "2026-08-03T08:00:00Z"
''',
        encoding="utf-8",
    )
    monkeypatch.setattr(rights, "DEFAULT_REGISTER_PATH", register)
    authority = load_rights_authority()
    policy = load_rights_policy()
    decision = resolve_rights(
        SOURCE_ID,
        authorized_sha256,
        authority=authority,
        policy=policy,
    )
    assert decision.decision_sha256 is not None
    intent = StorageIntent.from_path(
        source,
        artifact_id=BITSTREAM_ID,
        logical_key=logical_key or (
            "Jahre/2020/Markdown/2020-01-01_gesamtausgabe_"
            f"{DOCUMENT_ID}_{BITSTREAM_ID}.md"
        ),
        source_id=SOURCE_ID,
        source_sha256=authorized_sha256,
        decision_sha256=decision.decision_sha256,
        visibility="public",
        rights_state="approved",
        document_id=DOCUMENT_ID,
    )
    return RightsStorageAuthorizer(authority, policy), intent


def _materialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    runner: _ConversionRunner | None = None,
    runtime: RuntimeEvidence | None = None,
    limits: PdfLimits | None = None,
):
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    result = service.materialize_conversion(
        intent,
        bitstream_id=BITSTREAM_ID,
        temp_root=temp_root,
        ledger=ledger,
        authorizer=authorizer,
        runtime=runtime or _runtime(),
        runner=runner or _ConversionRunner(b"A" * 80 + b"\f"),
        limits=limits or PdfLimits(),
    )
    return service, result, ledger, intent


def test_good_text_conversion_publishes_atomic_valid_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, result, ledger, intent = _materialize(tmp_path, monkeypatch)

    assert result.state == "converted"
    assert result.quality == "good"
    assert result.ocr_used is False
    assert result.output_path.read_text(encoding="utf-8").count(
        "<!-- rki-page: 1 -->"
    ) == 1
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["conversion_id"] == result.conversion_id
    assert manifest["fingerprint_sha256"] == result.fingerprint_sha256
    assert manifest["output_sha256"] == hashlib.sha256(
        result.output_path.read_bytes()
    ).hexdigest()
    assert manifest["storage_reference"] is None
    assert manifest["state"] == "converted"
    assert [tool["name"] for tool in manifest["toolchain"]] == [
        "pdfinfo",
        "pdftotext",
    ]
    assert intent.source_path.read_bytes() == PDF
    assert [event.kind for event in ledger.events] == [
        EffectKind.TEMP_FILE,
        EffectKind.TEMP_FILE,
    ]
    assert {Path(event.target) for event in ledger.events} == {
        result.output_path.absolute(),
        result.manifest_path.absolute(),
    }


def test_long_toolchain_version_summary_stays_schema_bounded() -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    tools = tuple(
        ToolEvidence(
            name=name,
            version_output=f"{name} " + "v" * 80,
            executable_sha256=str(index) * 64,
            argv=(name, "--version"),
            environment=(),
            ocr_settings=None,
        )
        for index, name in enumerate(
            ("pdfinfo", "pdftotext", "pdftoppm", "tesseract"),
            start=1,
        )
    )

    converter, version = service._converter_identity(tools)

    assert converter == "pdfinfo+pdftotext+pdftoppm+tesseract"
    assert version.startswith("toolchain-sha256:")
    assert len(version) <= 120


def test_conversion_rejects_payload_not_bound_to_authorized_source_sha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(
        tmp_path,
        monkeypatch,
        rights_source_sha256="f" * 64,
    )
    runner = _ConversionRunner(b"A" * 80 + b"\f")
    temp_root = tmp_path / "materialized"

    with pytest.raises(service.ConversionIntegrityError, match="Rechte-SHA"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
            authorizer=authorizer,
            runtime=_runtime(),
            runner=runner,
        )

    assert runner.calls == []
    assert tuple(tmp_path.rglob("document.md")) == ()


def test_conversion_rejects_source_drift_before_first_pdf_tool(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    intent.source_path.write_bytes(PDF + b"\n")
    runner = _ConversionRunner(b"A" * 80 + b"\f")
    temp_root = tmp_path / "materialized"

    with pytest.raises(service.ConversionError, match="Byte-Identität"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
            authorizer=authorizer,
            runtime=_runtime(),
            runner=runner,
        )

    assert runner.calls == []
    assert tuple(tmp_path.rglob("document.md")) == ()


def test_conversion_binds_bitstream_id_to_storage_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"

    with pytest.raises(service.ConversionIntegrityError, match="Bitstream-ID"):
        service.materialize_conversion(
            intent,
            bitstream_id="rki-bitstream-" + "2" * 64,
            temp_root=temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
            authorizer=authorizer,
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )


def test_conversion_requires_canonical_markdown_logical_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(
        tmp_path,
        monkeypatch,
        logical_key="Jahre/2020/source.pdf",
    )
    temp_root = tmp_path / "materialized"

    with pytest.raises(service.ConversionIntegrityError, match="Markdown-Pfad"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root),
            authorizer=authorizer,
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )


def test_low_text_quality_uses_ocr_and_forces_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    ocr_settings = OcrSettings(
        dpi=300,
        color_mode="gray",
        psm=3,
        oem=1,
        languages=("deu", "eng"),
        tessdata=(NamedDigest("deu", "1" * 64), NamedDigest("eng", "2" * 64)),
    )
    ocr_tool = ToolEvidence(
        name="tesseract",
        version_output="tesseract 5.5.1",
        executable_sha256="e" * 64,
        argv=("tesseract", "$INPUT", "stdout", "-l", "deu+eng"),
        environment=(
            EnvironmentVariable("LANG", "C.UTF-8"),
            EnvironmentVariable("LC_ALL", "C.UTF-8"),
        ),
        ocr_settings=ocr_settings,
    )

    def fake_ocr(*args, **kwargs):
        return OcrExtraction(
            pages=("OCR text",),
            markdown="<!-- rki-page: 1 -->\nOCR text\n",
            toolchain=(ocr_tool,),
        )

    monkeypatch.setattr(service, "extract_ocr", fake_ocr)
    _service, result, _ledger, _intent = _materialize(
        tmp_path,
        monkeypatch,
        runner=_ConversionRunner(b"\f"),
    )

    assert result.state == "needs_review"
    assert result.quality == "needs_review"
    assert result.ocr_used is True
    assert result.output_path.read_text(encoding="utf-8").endswith("OCR text\n")


def test_ocr_markdown_must_match_positional_pages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    tool = ToolEvidence(
        name="tesseract",
        version_output="tesseract 5.5.1",
        executable_sha256="e" * 64,
        argv=("tesseract", "$INPUT", "stdout"),
        environment=(),
        ocr_settings=None,
    )

    def inconsistent_ocr(*args, **kwargs):
        return OcrExtraction(
            pages=("OCR text",),
            markdown="<!-- rki-page: 1 -->\nsubstituted\n",
            toolchain=(tool,),
        )

    monkeypatch.setattr(service, "extract_ocr", inconsistent_ocr)

    with pytest.raises(service.ConversionIntegrityError, match="Seitenmarkern"):
        _materialize(
            tmp_path,
            monkeypatch,
            runner=_ConversionRunner(b"\f"),
        )

    assert tuple(tmp_path.rglob("document.md")) == ()


def test_missing_ocr_tool_is_visible_and_publishes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")

    def unavailable(*args, **kwargs):
        raise OcrUnavailableError("tesseract fehlt")

    monkeypatch.setattr(service, "extract_ocr", unavailable)
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)

    with pytest.raises(service.ConversionNeedsReview, match="tesseract"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=ledger,
            authorizer=authorizer,
            runtime=_runtime(),
            runner=_ConversionRunner(b"\f"),
        )

    assert ledger.events == []
    assert not temp_root.exists() or tuple(temp_root.rglob("document.md")) == ()
    assert intent.source_path.read_bytes() == PDF


def test_same_fingerprint_and_output_skip_without_mtime_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, first, ledger, intent = _materialize(tmp_path, monkeypatch)
    output_mtime = first.output_path.stat().st_mtime_ns
    manifest_mtime = first.manifest_path.stat().st_mtime_ns
    second_ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=ledger.temp_root)
    second = service.materialize_conversion(
        intent,
        bitstream_id=BITSTREAM_ID,
        temp_root=ledger.temp_root,
        ledger=second_ledger,
        authorizer=_authorizer_and_intent(tmp_path, monkeypatch)[0],
        runtime=_runtime(),
        runner=_ConversionRunner(b"A" * 80 + b"\f"),
    )

    assert second.state == "skipped_unchanged"
    assert second.output_path == first.output_path
    assert second.output_path.stat().st_mtime_ns == output_mtime
    assert second.manifest_path.stat().st_mtime_ns == manifest_mtime
    assert second_ledger.events == []


def test_changed_limits_change_options_fingerprint_and_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, first, _ledger, _intent = _materialize(tmp_path, monkeypatch)
    changed = PdfLimits(wall_seconds=119)
    _service, second, _ledger, _intent = _materialize(
        tmp_path,
        monkeypatch,
        limits=changed,
    )

    first_manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    second_manifest = json.loads(second.manifest_path.read_text(encoding="utf-8"))
    assert second.fingerprint_sha256 != first.fingerprint_sha256
    assert second.conversion_id != first.conversion_id
    assert second_manifest["options_sha256"] != first_manifest["options_sha256"]


def test_pdfinfo_executable_drift_changes_fingerprint_and_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _service, first, _ledger, _intent = _materialize(tmp_path, monkeypatch)
    _service, second, _ledger, _intent = _materialize(
        tmp_path,
        monkeypatch,
        runner=_ConversionRunner(b"A" * 80 + b"\f", pdfinfo_sha256="f" * 64),
    )

    assert second.fingerprint_sha256 != first.fingerprint_sha256
    assert second.conversion_id != first.conversion_id


def test_rights_are_rechecked_immediately_before_ocr(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    real_authorize = service.authorize_storage_operation
    ocr_called = False

    def revoke(active_authorizer, subject, *, operation):
        if operation == "convert_ocr":
            raise rights.RightsPolicyError("revoked before OCR")
        return real_authorize(active_authorizer, subject, operation=operation)

    def unexpected_ocr(*args, **kwargs):
        nonlocal ocr_called
        ocr_called = True
        raise AssertionError("OCR darf nach Widerruf nicht starten")

    monkeypatch.setattr(service, "_authorize", revoke)
    monkeypatch.setattr(service, "extract_ocr", unexpected_ocr)

    with pytest.raises(rights.RightsPolicyError, match="revoked before OCR"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=ledger,
            authorizer=authorizer,
            runtime=_runtime(),
            runner=_ConversionRunner(b"\f"),
        )

    assert ocr_called is False
    assert ledger.events == []
    assert tuple(tmp_path.rglob("document.md")) == ()


def test_tampered_existing_output_fails_closed_without_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, first, ledger, intent = _materialize(tmp_path, monkeypatch)
    first.output_path.write_text("tampered", encoding="utf-8")
    tampered_mtime = first.output_path.stat().st_mtime_ns

    with pytest.raises(service.ConversionIntegrityError, match="Output"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=ledger.temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=ledger.temp_root),
            authorizer=_authorizer_and_intent(tmp_path, monkeypatch)[0],
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )

    assert first.output_path.read_text(encoding="utf-8") == "tampered"
    assert first.output_path.stat().st_mtime_ns == tampered_mtime


def test_paired_output_and_manifest_tamper_cannot_claim_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, first, ledger, intent = _materialize(tmp_path, monkeypatch)
    tampered = b"paired tamper\n"
    first.output_path.write_bytes(tampered)
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest["output_sha256"] = hashlib.sha256(tampered).hexdigest()
    first.manifest_path.write_text(
        service.stable_json_dumps(manifest),
        encoding="utf-8",
    )

    with pytest.raises(service.ConversionIntegrityError, match="frisch abgeleitet"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=ledger.temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=ledger.temp_root),
            authorizer=_authorizer_and_intent(tmp_path, monkeypatch)[0],
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )

    assert first.output_path.read_bytes() == tampered


def test_manifest_quality_tamper_cannot_claim_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, first, ledger, intent = _materialize(tmp_path, monkeypatch)
    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    manifest.update(state="skipped_unchanged", quality="needs_review", ocr_used=False)
    first.manifest_path.write_text(
        service.stable_json_dumps(manifest),
        encoding="utf-8",
    )

    with pytest.raises(service.ConversionIntegrityError, match="Qualität/OCR"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=ledger.temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=ledger.temp_root),
            authorizer=_authorizer_and_intent(tmp_path, monkeypatch)[0],
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )


def test_existing_bundle_swap_during_fd_read_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, first, ledger, intent = _materialize(tmp_path, monkeypatch)
    target = first.output_path.parent
    moved = target.with_name(target.name + "-moved")
    real_read = service._read_regular_bounded_with_identity_at
    swapped = False

    def swap_after_manifest(directory_fd, name, *, maximum):
        nonlocal swapped
        payload = real_read(directory_fd, name, maximum=maximum)
        if name == "conversion-manifest.json" and not swapped:
            target.rename(moved)
            target.symlink_to(moved.name, target_is_directory=True)
            swapped = True
        return payload

    monkeypatch.setattr(
        service,
        "_read_regular_bounded_with_identity_at",
        swap_after_manifest,
    )

    with pytest.raises(service.ConversionIntegrityError, match="ausgetauscht"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=ledger.temp_root,
            ledger=EffectLedger(RunMode.MATERIALIZE, temp_root=ledger.temp_root),
            authorizer=_authorizer_and_intent(tmp_path, monkeypatch)[0],
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )

    assert swapped is True
    assert moved.joinpath("document.md").read_bytes() == first.output_path.resolve().read_bytes()


def test_peer_publish_between_check_and_publish_is_revalidated_and_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    service, seed, _seed_ledger, _seed_intent = _materialize(seed_dir, monkeypatch)
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    staging = import_module("scripts.rki_pipeline.staging")
    real_rename_noreplace = staging._rename_noreplace
    peer_state: dict[str, int] = {}

    def publish_peer_before_atomic_rename(parent_fd, source, target_name):
        parent = Path(f"/proc/self/fd/{parent_fd}")
        target = parent / target_name
        shutil.copytree(seed.output_path.parent, target, copy_function=shutil.copy2)
        peer_state["inode"] = target.stat().st_ino
        peer_state["mtime"] = (target / "document.md").stat().st_mtime_ns
        return real_rename_noreplace(parent_fd, source, target_name)

    monkeypatch.setattr(staging, "_rename_noreplace", publish_peer_before_atomic_rename)

    result = service.materialize_conversion(
        intent,
        bitstream_id=BITSTREAM_ID,
        temp_root=temp_root,
        ledger=ledger,
        authorizer=authorizer,
        runtime=_runtime(),
        runner=_ConversionRunner(b"A" * 80 + b"\f"),
    )

    assert result.state == "skipped_unchanged"
    assert result.output_path.parent.stat().st_ino == peer_state["inode"]
    assert result.output_path.stat().st_mtime_ns == peer_state["mtime"]
    assert ledger.events == []
    assert tuple(result.output_path.parent.parent.glob(".*.backup")) == ()
    assert tuple(result.output_path.parent.parent.glob(".*.staging-*")) == ()


def test_ledger_failure_occurs_before_bundle_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)

    def reject(*args, **kwargs):
        raise RuntimeError("ledger rejected")

    monkeypatch.setattr(EffectLedger, "record", reject)

    with pytest.raises(RuntimeError, match="ledger rejected"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=ledger,
            authorizer=authorizer,
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )

    assert ledger.events == []
    assert tuple(tmp_path.rglob("document.md")) == ()


def test_post_commit_interrupt_keeps_ledger_for_published_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    real_staged_directory = service.staged_directory

    @contextmanager
    def interrupt_after_commit(target, **kwargs):
        with real_staged_directory(target, **kwargs) as stage:
            yield stage
        raise KeyboardInterrupt("after commit")

    monkeypatch.setattr(service, "staged_directory", interrupt_after_commit)

    with pytest.raises(KeyboardInterrupt, match="after commit"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=ledger,
            authorizer=authorizer,
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )

    assert len(ledger.events) == 2
    assert len(tuple(temp_root.rglob("document.md"))) == 1
    assert len(tuple(temp_root.rglob("conversion-manifest.json"))) == 1


def test_each_generated_file_obeys_file_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")

    with pytest.raises(service.ConversionIntegrityError, match="Dateigrößenlimit"):
        _materialize(
            tmp_path,
            monkeypatch,
            limits=PdfLimits(generated_file_bytes=64),
        )

    assert tuple(tmp_path.rglob("document.md")) == ()


def test_revocation_before_first_temp_write_rolls_back_bundle_and_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    real_authorize = service.authorize_storage_operation
    calls = 0

    def revoke(active_authorizer, subject, *, operation):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise rights.RightsPolicyError("revoked")
        return real_authorize(active_authorizer, subject, operation=operation)

    monkeypatch.setattr(service, "_authorize", revoke)

    with pytest.raises(rights.RightsPolicyError, match="revoked"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=ledger,
            authorizer=authorizer,
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )

    assert ledger.events == []
    assert not temp_root.exists() or tuple(temp_root.rglob("document.md")) == ()


def test_base_exception_during_pair_write_removes_owned_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = import_module("scripts.rki_pipeline.conversion.service")
    authorizer, intent = _authorizer_and_intent(tmp_path, monkeypatch)
    temp_root = tmp_path / "materialized"
    ledger = EffectLedger(RunMode.MATERIALIZE, temp_root=temp_root)
    real_write = service._write_stage_file
    calls = 0

    def interrupt(stage, name, payload):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt("cancelled")
        return real_write(stage, name, payload)

    monkeypatch.setattr(service, "_write_stage_file", interrupt)

    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        service.materialize_conversion(
            intent,
            bitstream_id=BITSTREAM_ID,
            temp_root=temp_root,
            ledger=ledger,
            authorizer=authorizer,
            runtime=_runtime(),
            runner=_ConversionRunner(b"A" * 80 + b"\f"),
        )

    assert ledger.events == []
    assert not temp_root.exists() or tuple(temp_root.rglob("document.md")) == ()
    assert intent.source_path.read_bytes() == PDF
