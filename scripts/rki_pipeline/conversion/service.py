#!/usr/bin/env python3
"""Rights-bound, rollback-safe conversion bundle materialization."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
from pathlib import PurePosixPath
import posixpath
import stat

from scripts.rki_pipeline.conversion.base import (
    EnvironmentVariable,
    RuntimeEvidence,
    ToolEvidence,
    conversion_fingerprint,
    conversion_id,
)
from scripts.rki_pipeline.conversion.ocr import (
    OcrError,
    OcrUnavailableError,
    extract_text as extract_ocr,
)
from scripts.rki_pipeline.conversion.frontmatter import (
    MarkdownMetadata,
    render_frontmatter,
)
from scripts.rki_pipeline.conversion.pdftotext import (
    TextExtractionError,
    extract_text,
    render_page_markers,
)
from scripts.rki_pipeline.conversion.quality import (
    MAX_REPLACEMENT_RATIO,
    MIN_CHARACTERS_PER_PAGE,
    assess_quality,
)
from scripts.rki_pipeline.io_utils import (
    GENERATED_ROOT_SENTINEL,
    assert_generated_root_fd,
    open_directory_beneath,
    open_root_directory,
    sha256_bytes,
    stable_json_dumps,
)
from scripts.rki_pipeline.pdf_validation import (
    DEFAULT_PDF_LIMITS,
    PdfLimits,
    PdfValidationError,
    ProcessRunner,
    ProcessRunnerError,
    Runner,
    validated_pdf,
)
from scripts.rki_pipeline.paths import (
    DocumentPathError,
    canonical_document_paths,
)
from scripts.rki_pipeline.run_modes import EffectKind, EffectLedger, RunMode
from scripts.rki_pipeline.schema_registry import validate_document
from scripts.rki_pipeline.staging import (
    StagingConflictError,
    StagingError,
    StagingState,
    staged_directory,
)
from scripts.rki_pipeline.storage.base import (
    RightsStorageAuthorizer,
    StorageError,
    StorageIntent,
    authorize_storage_operation,
)


class ConversionError(RuntimeError):
    """Conversion cannot satisfy its deterministic materialization contract."""


class ConversionIntegrityError(ConversionError):
    """Existing or newly derived conversion evidence is inconsistent."""


class ConversionNeedsReview(ConversionError):
    """Conversion needs a human-visible intervention before materialization."""


@dataclass(frozen=True, slots=True)
class ConversionResult:
    state: str
    quality: str
    conversion_id: str
    fingerprint_sha256: str
    output_path: Path
    manifest_path: Path
    ocr_used: bool


_MAX_MANIFEST_BYTES = 1024 * 1024
_OPTIONS = {
    "page_marker": "<!-- rki-page: N -->",
    "pdftotext": ["-layout", "-enc", "UTF-8", "-eol", "unix"],
    "quality": {
        "minimum_characters_per_page": MIN_CHARACTERS_PER_PAGE,
        "maximum_replacement_ratio": MAX_REPLACEMENT_RATIO,
    },
    "ocr": {
        "dpi": 300,
        "color_mode": "gray",
        "languages": ["deu", "eng"],
        "psm": 3,
        "oem": 1,
    },
}
def _options_sha256(limits: PdfLimits, *, frontmatter_sha256: str) -> str:
    options = {
        **_OPTIONS,
        "frontmatter": {"schema_version": 1, "sha256": frontmatter_sha256},
        "limits": {
            name: getattr(limits, name)
            for name in limits.__dataclass_fields__
        },
    }
    return sha256_bytes(stable_json_dumps(options).encode("utf-8"))


def _authorize(
    authorizer: RightsStorageAuthorizer,
    subject: StorageIntent,
    *,
    operation: str,
) -> None:
    authorize_storage_operation(authorizer, subject, operation=operation)


def _converter_identity(toolchain: tuple[ToolEvidence, ...]) -> tuple[str, str]:
    converter = "+".join(tool.name for tool in toolchain)
    versions = ";".join(
        f"{tool.name}:{tool.version_output.splitlines()[0]}" for tool in toolchain
    )
    if len(converter) > 120:
        converter = "toolchain-" + sha256_bytes(converter.encode("utf-8"))
    if len(versions) > 120:
        versions = "toolchain-sha256:" + sha256_bytes(
            stable_json_dumps([tool.to_dict() for tool in toolchain]).encode("utf-8")
        )
    return converter, versions


def _read_regular_bounded_with_identity_at(
    directory_fd: int,
    name: str,
    *,
    maximum: int,
) -> tuple[bytes, tuple[int, int, int, int]]:
    if "/" in name or name in {"", ".", ".."}:
        raise ConversionIntegrityError("Ungültiger Conversion-Dateiname")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ConversionIntegrityError(f"Conversion-Datei ist nicht sicher lesbar: {name}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum:
            raise ConversionIntegrityError(
                f"Conversion-Datei verletzt Größen-/Dateitypgrenze: {name}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, maximum + 1 - total))
            if not chunk:
                return b"".join(chunks), (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                )
            total += len(chunk)
            if total > maximum:
                raise ConversionIntegrityError(
                    f"Conversion-Datei überschreitet {maximum} Bytes: {name}"
                )
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _write_stage_file(stage: Path, name: str, payload: bytes) -> None:
    """Write one exclusive file inside an unpublished descriptor-backed stage."""

    if "/" in name or name in {"", ".", ".."}:
        raise ConversionIntegrityError("Ungültiger Staging-Dateiname")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(stage / name, flags, 0o644)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Staging-Write machte keinen Fortschritt")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _existing_result(
    root: Path,
    *,
    expected_conversion_id: str,
    expected_fingerprint: str,
    expected_output_sha256: str,
    expected_output_size: int,
    expected_quality: str,
    expected_ocr_used: bool,
    maximum_output: int,
    maximum_manifest: int,
    maximum_total: int,
) -> ConversionResult | None:
    try:
        with open_root_directory(root) as root_fd:
            try:
                conversions_fd = open_directory_beneath(root_fd, ("conversions",))
            except FileNotFoundError:
                return None
            try:
                try:
                    descriptor = open_directory_beneath(
                        conversions_fd,
                        (expected_conversion_id,),
                    )
                except FileNotFoundError:
                    return None
                try:
                    assert_generated_root_fd(descriptor)
                    if set(os.listdir(descriptor)) != {
                        GENERATED_ROOT_SENTINEL,
                        "document.md",
                        "conversion-manifest.json",
                    }:
                        raise ConversionIntegrityError(
                            "Conversion-Bundle enthält unerwartete Einträge"
                        )
                    manifest_bytes, manifest_identity = _read_regular_bounded_with_identity_at(
                        descriptor,
                        "conversion-manifest.json",
                        maximum=maximum_manifest,
                    )
                    output, output_identity = _read_regular_bounded_with_identity_at(
                        descriptor,
                        "document.md",
                        maximum=maximum_output,
                    )
                    if len(manifest_bytes) + len(output) > maximum_total:
                        raise ConversionIntegrityError(
                            "Conversion-Bundle überschreitet Gesamtgrößenlimit"
                        )
                    for name, identity in (
                        ("conversion-manifest.json", manifest_identity),
                        ("document.md", output_identity),
                    ):
                        current_file = os.stat(
                            name,
                            dir_fd=descriptor,
                            follow_symlinks=False,
                        )
                        if (
                            current_file.st_dev,
                            current_file.st_ino,
                            current_file.st_mode,
                            current_file.st_size,
                        ) != identity:
                            raise ConversionIntegrityError(
                                f"Conversion-Datei wurde während der Prüfung ausgetauscht: {name}"
                            )
                    held = os.fstat(descriptor)
                    current = os.stat(
                        expected_conversion_id,
                        dir_fd=conversions_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(current.st_mode)
                        or (current.st_dev, current.st_ino) != (held.st_dev, held.st_ino)
                    ):
                        raise ConversionIntegrityError(
                            "Conversion-Bundle wurde während der Prüfung ausgetauscht"
                        )
                    held_conversions = os.fstat(conversions_fd)
                    current_conversions = os.stat(
                        "conversions",
                        dir_fd=root_fd,
                        follow_symlinks=False,
                    )
                    if (
                        not stat.S_ISDIR(current_conversions.st_mode)
                        or (current_conversions.st_dev, current_conversions.st_ino)
                        != (held_conversions.st_dev, held_conversions.st_ino)
                    ):
                        raise ConversionIntegrityError(
                            "Conversion-Verzeichnis wurde während der Prüfung ausgetauscht"
                        )
                    held_root = os.fstat(root_fd)
                    current_root = os.stat(root, follow_symlinks=False)
                    if (
                        not stat.S_ISDIR(current_root.st_mode)
                        or (current_root.st_dev, current_root.st_ino)
                        != (held_root.st_dev, held_root.st_ino)
                    ):
                        raise ConversionIntegrityError(
                            "Conversion-Wurzel wurde während der Prüfung ausgetauscht"
                        )
                finally:
                    os.close(descriptor)
            finally:
                os.close(conversions_fd)
    except (OSError, ValueError) as exc:
        raise ConversionIntegrityError("Conversion-Bundle besitzt keine gültige Markierung") from exc
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConversionIntegrityError("Conversion-Manifest ist kein gültiges JSON") from exc
    if not isinstance(manifest, dict):
        raise ConversionIntegrityError("Conversion-Manifest ist kein Objekt")
    try:
        validate_document("conversion-manifest", manifest)
    except ValueError as exc:
        raise ConversionIntegrityError("Conversion-Manifest verletzt Schema/Evidenz") from exc
    if (
        manifest["conversion_id"] != expected_conversion_id
        or manifest["fingerprint_sha256"] != expected_fingerprint
    ):
        raise ConversionIntegrityError("Conversion-Manifest driftet von Fingerprint/Identität")
    if (
        manifest["quality"] != expected_quality
        or manifest["ocr_used"] is not expected_ocr_used
    ):
        raise ConversionIntegrityError(
            "Conversion-Manifest driftet von frisch abgeleiteter Qualität/OCR-Nutzung"
        )
    measured_output = hashlib.sha256(output).hexdigest()
    if measured_output != manifest["output_sha256"]:
        raise ConversionIntegrityError("Conversion-Output stimmt nicht mit Manifest überein")
    if (measured_output, len(output)) != (expected_output_sha256, expected_output_size):
        raise ConversionIntegrityError(
            "Conversion-Output stimmt nicht mit frisch abgeleitetem Output überein"
        )
    target = root / "conversions" / expected_conversion_id
    output_path = target / "document.md"
    manifest_path = target / "conversion-manifest.json"
    return ConversionResult(
        state="skipped_unchanged",
        quality=manifest["quality"],
        conversion_id=expected_conversion_id,
        fingerprint_sha256=expected_fingerprint,
        output_path=output_path,
        manifest_path=manifest_path,
        ocr_used=manifest["ocr_used"],
    )


def _manifest(
    *,
    intent: StorageIntent,
    bitstream_id: str,
    page_count: int,
    toolchain: tuple[ToolEvidence, ...],
    runtime: RuntimeEvidence,
    converter: str,
    converter_version: str,
    fingerprint: str,
    identity: str,
    output_sha256: str,
    options_sha256: str,
    state: str,
    quality: str,
    ocr_used: bool,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.1.0",
        "conversion_id": identity,
        "document_id": intent.document_id,
        "bitstream_id": bitstream_id,
        "source_sha256": intent.source_sha256,
        "converter": converter,
        "converter_version": converter_version,
        "options_sha256": options_sha256,
        "page_count": page_count,
        "toolchain": [tool.to_dict() for tool in toolchain],
        "runtime": runtime.to_dict(),
        "fingerprint_sha256": fingerprint,
        "output_sha256": output_sha256,
        "storage_reference": None,
        "state": state,
        "quality": quality,
        "ocr_used": ocr_used,
        "provenance_state": "current",
    }
    validate_document("conversion-manifest", payload)
    return payload


def _source_pdf_path(
    intent: StorageIntent,
    bitstream_id: str,
    metadata: MarkdownMetadata,
) -> str:
    if type(metadata) is not MarkdownMetadata:
        raise TypeError("metadata muss ein exaktes MarkdownMetadata sein")
    try:
        paths = canonical_document_paths(
            document_id=intent.document_id or "",
            bitstream_id=bitstream_id,
            document_type=metadata.document_type,
            publication_date=metadata.publication_date.isoformat(),
        )
    except (DocumentPathError, ValueError) as exc:
        raise ConversionIntegrityError(
            "Metadaten erzeugen keinen kanonischen Dokumentpfad"
        ) from exc
    if intent.logical_key != paths.markdown:
        raise ConversionIntegrityError("StorageIntent besitzt keinen kanonischen Markdown-Pfad")
    return posixpath.relpath(
        paths.pdf,
        start=PurePosixPath(paths.markdown).parent.as_posix(),
    )


def _pdfinfo_evidence(parser, runner: Runner, *, cwd: Path, limits: PdfLimits) -> ToolEvidence:
    try:
        version = runner.run(parser.argv[0], ("-v",), cwd=cwd, limits=limits)
    except ProcessRunnerError as exc:
        raise ConversionError("pdfinfo-Version konnte nicht ermittelt werden") from exc
    if (
        parser.returncode != 0
        or version.returncode != 0
        or parser.executable_sha256 != version.executable_sha256
    ):
        raise ConversionIntegrityError("pdfinfo-Executable oder Rückgabestatus driftet")
    try:
        version_output = (version.stdout + version.stderr).decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ConversionIntegrityError("pdfinfo-Version ist nicht UTF-8") from exc
    if not version_output:
        raise ConversionIntegrityError("pdfinfo-Version fehlt")
    return ToolEvidence(
        name="pdfinfo",
        version_output=version_output,
        executable_sha256=parser.executable_sha256,
        argv=("pdfinfo", "-enc", "UTF-8", "$INPUT"),
        environment=(
            EnvironmentVariable("LANG", "C.UTF-8"),
            EnvironmentVariable("LC_ALL", "C.UTF-8"),
        ),
        ocr_settings=None,
    )


def _materialize_conversion(
    intent: StorageIntent,
    *,
    bitstream_id: str,
    temp_root: Path,
    ledger: EffectLedger,
    authorizer: RightsStorageAuthorizer,
    metadata: MarkdownMetadata,
    runtime: RuntimeEvidence,
    runner: Runner | None = None,
    limits: PdfLimits = DEFAULT_PDF_LIMITS,
    tessdata: tuple[Path, ...] | None = None,
) -> ConversionResult:
    """Materialize one page-marked conversion bundle under ``temp_root`` only."""

    if type(intent) is not StorageIntent:
        raise TypeError("intent muss ein exakter StorageIntent sein")
    if intent.document_id is None:
        raise ConversionIntegrityError("Conversion benötigt document_id")
    if intent.conversion_id is not None:
        raise ConversionIntegrityError("Quell-Intent darf keine conversion_id vorwegnehmen")
    if bitstream_id != intent.artifact_id:
        raise ConversionIntegrityError("Bitstream-ID stimmt nicht mit StorageIntent überein")
    if intent.sha256 != intent.source_sha256:
        raise ConversionIntegrityError("Payload-SHA stimmt nicht mit autorisierter Rechte-SHA überein")
    source_pdf = _source_pdf_path(intent, bitstream_id, metadata)
    try:
        render_frontmatter(
            metadata,
            document_id=intent.document_id,
            source_id=intent.source_id,
            source_pdf=source_pdf,
            source_sha256=intent.source_sha256,
            conversion_quality="good",
            ocr_used=False,
        )
    except ValueError as exc:
        raise ConversionIntegrityError(f"Frontmatter-Metadaten sind ungültig: {exc}") from exc
    root = Path(temp_root).resolve()
    if ledger.mode is not RunMode.MATERIALIZE or ledger.temp_root != root.resolve():
        raise ConversionIntegrityError("Conversion benötigt passendes Materialize-Ledger")
    if type(runtime) is not RuntimeEvidence:
        raise TypeError("runtime muss ein exaktes RuntimeEvidence sein")
    if type(limits) is not PdfLimits:
        raise TypeError("limits muss ein exaktes PdfLimits sein")

    active_runner = runner if runner is not None else ProcessRunner()
    _authorize(authorizer, intent, operation="convert")
    with validated_pdf(
        intent.source_path,
        temp_root=root,
        runner=active_runner,
        limits=limits,
        expected_sha256=intent.source_sha256,
        expected_size=intent.size,
    ) as validated:
        byte_evidence = validated.validation.bytes
        if (byte_evidence.sha256, byte_evidence.size) != (intent.sha256, intent.size):
            raise ConversionIntegrityError("PDF-Bytes stimmen nicht mit StorageIntent überein")
        pdfinfo_tool = _pdfinfo_evidence(
            validated.validation.parser,
            active_runner,
            cwd=validated.path.parent,
            limits=limits,
        )
        text = extract_text(
            validated.path,
            workdir=validated.path.parent,
            expected_page_count=validated.validation.pages,
            runner=active_runner,
            limits=limits,
        )
        assessment = assess_quality(
            text.pages,
            expected_page_count=validated.validation.pages,
        )
        if assessment.quality == "good":
            markdown = text.markdown
            pages = text.pages
            toolchain = (pdfinfo_tool, text.tool)
            quality = "good"
            state = "converted"
            ocr_used = False
        else:
            _authorize(authorizer, intent, operation="convert_ocr")
            try:
                ocr = extract_ocr(
                    validated.path,
                    workdir=validated.path.parent,
                    expected_page_count=validated.validation.pages,
                    tessdata=tessdata or (),
                    runner=active_runner,
                    limits=limits,
                )
            except (OcrUnavailableError, ValueError) as exc:
                raise ConversionNeedsReview(f"OCR benötigt Prüfung: {exc}") from exc
            markdown = ocr.markdown
            pages = ocr.pages
            toolchain = (pdfinfo_tool, text.tool, *ocr.toolchain)
            quality = "needs_review"
            state = "needs_review"
            ocr_used = True

        if len(pages) != validated.validation.pages or markdown != render_page_markers(pages):
            raise ConversionIntegrityError(
                "Conversion-Markdown stimmt nicht mit positionalen Seitenmarkern überein"
            )

        converter, converter_version = _converter_identity(toolchain)
        try:
            frontmatter = render_frontmatter(
                metadata,
                document_id=intent.document_id,
                source_id=intent.source_id,
                source_pdf=source_pdf,
                source_sha256=intent.source_sha256,
                conversion_quality=quality,
                ocr_used=ocr_used,
            )
        except ValueError as exc:
            raise ConversionIntegrityError(f"Frontmatter-Metadaten sind ungültig: {exc}") from exc
        frontmatter_sha256 = hashlib.sha256(frontmatter.encode("utf-8")).hexdigest()
        options_sha256 = _options_sha256(
            limits,
            frontmatter_sha256=frontmatter_sha256,
        )
        fingerprint = conversion_fingerprint(
            source_sha256=intent.source_sha256,
            converter=converter,
            converter_version=converter_version,
            options_sha256=options_sha256,
            toolchain=toolchain,
            runtime=runtime,
        )
        identity = conversion_id(intent.document_id, bitstream_id, fingerprint)
        target = root / "conversions" / identity
        output = (frontmatter + markdown).encode("utf-8")
        if len(output) > min(limits.generated_file_bytes, limits.total_output_bytes):
            raise ConversionIntegrityError("Conversion-Output überschreitet Dateigrößenlimit")
        output_sha256 = hashlib.sha256(output).hexdigest()
        existing_manifest_limit = min(
            _MAX_MANIFEST_BYTES,
            limits.generated_file_bytes,
            max(1, limits.total_output_bytes - len(output)),
        )
        _authorize(authorizer, intent, operation="convert_existing_read")
        existing = _existing_result(
            root,
            expected_conversion_id=identity,
            expected_fingerprint=fingerprint,
            expected_output_sha256=output_sha256,
            expected_output_size=len(output),
            expected_quality=quality,
            expected_ocr_used=ocr_used,
            maximum_output=min(limits.generated_file_bytes, limits.total_output_bytes),
            maximum_manifest=existing_manifest_limit,
            maximum_total=limits.total_output_bytes,
        )
        if existing is not None:
            _authorize(authorizer, intent, operation="convert_skip")
            return existing

        manifest = _manifest(
            intent=intent,
            bitstream_id=bitstream_id,
            page_count=validated.validation.pages,
            toolchain=toolchain,
            runtime=runtime,
            converter=converter,
            converter_version=converter_version,
            fingerprint=fingerprint,
            identity=identity,
            output_sha256=output_sha256,
            options_sha256=options_sha256,
            state=state,
            quality=quality,
            ocr_used=ocr_used,
        )
        manifest_bytes = stable_json_dumps(manifest).encode("utf-8")
        if len(manifest_bytes) > min(_MAX_MANIFEST_BYTES, limits.generated_file_bytes):
            raise ConversionIntegrityError("Conversion-Manifest überschreitet Dateigrößenlimit")
        if len(output) + len(manifest_bytes) > limits.total_output_bytes:
            raise ConversionIntegrityError("Conversion-Bundle überschreitet Gesamtgrößenlimit")

        event_count = len(ledger.events)
        output_path = target / "document.md"
        manifest_path = target / "conversion-manifest.json"
        staging_state = StagingState()
        try:
            ledger.record(
                EffectKind.TEMP_FILE,
                output_path.as_posix(),
                sha256=output_sha256,
                size=len(output),
            )
            ledger.record(
                EffectKind.TEMP_FILE,
                manifest_path.as_posix(),
                sha256=hashlib.sha256(manifest_bytes).hexdigest(),
                size=len(manifest_bytes),
            )
            with staged_directory(
                target,
                allowed_root=root,
                replace_existing=False,
                state=staging_state,
            ) as stage:
                _authorize(authorizer, intent, operation="convert_output")
                _write_stage_file(stage, "document.md", output)
                _authorize(authorizer, intent, operation="convert_manifest")
                _write_stage_file(stage, "conversion-manifest.json", manifest_bytes)
                _authorize(authorizer, intent, operation="convert_publish")
        except StagingConflictError as exc:
            del ledger.events[event_count:]
            _authorize(authorizer, intent, operation="convert_peer_skip")
            peer = _existing_result(
                root,
                expected_conversion_id=identity,
                expected_fingerprint=fingerprint,
                expected_output_sha256=output_sha256,
                expected_output_size=len(output),
                expected_quality=quality,
                expected_ocr_used=ocr_used,
                maximum_output=min(limits.generated_file_bytes, limits.total_output_bytes),
                maximum_manifest=existing_manifest_limit,
                maximum_total=limits.total_output_bytes,
            )
            if peer is None:
                raise ConversionIntegrityError(
                    "Parallel veröffentlichtes Conversion-Bundle fehlt bei Revalidierung"
                ) from exc
            return peer
        except BaseException:
            if not staging_state.published:
                del ledger.events[event_count:]
            raise
        return ConversionResult(
            state=state,
            quality=quality,
            conversion_id=identity,
            fingerprint_sha256=fingerprint,
            output_path=output_path,
            manifest_path=manifest_path,
            ocr_used=ocr_used,
        )


def materialize_conversion(
    intent: StorageIntent,
    *,
    bitstream_id: str,
    temp_root: Path,
    ledger: EffectLedger,
    authorizer: RightsStorageAuthorizer,
    metadata: MarkdownMetadata,
    runtime: RuntimeEvidence,
    runner: Runner | None = None,
    limits: PdfLimits = DEFAULT_PDF_LIMITS,
    tessdata: tuple[Path, ...] | None = None,
) -> ConversionResult:
    """Materialize one conversion while preserving public failure taxonomy."""

    try:
        return _materialize_conversion(
            intent,
            bitstream_id=bitstream_id,
            temp_root=temp_root,
            ledger=ledger,
            authorizer=authorizer,
            metadata=metadata,
            runtime=runtime,
            runner=runner,
            limits=limits,
            tessdata=tessdata,
        )
    except ConversionError:
        raise
    except PdfValidationError as exc:
        raise ConversionError(f"PDF-Validierung fehlgeschlagen: {exc}") from exc
    except TextExtractionError as exc:
        raise ConversionError(f"PDF-Textextraktion fehlgeschlagen: {exc}") from exc
    except OcrError as exc:
        raise ConversionError(f"PDF-OCR fehlgeschlagen: {exc}") from exc
    except StagingError as exc:
        raise ConversionError(f"Conversion-Staging fehlgeschlagen: {exc}") from exc
    except StorageError as exc:
        raise ConversionError(f"Conversion-Speicherzugriff fehlgeschlagen: {exc}") from exc
