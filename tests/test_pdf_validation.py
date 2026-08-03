"""Shared PDF validation and bounded subprocess regressions."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time

import pytest

from scripts.rki_pipeline.pdf_validation import (
    DEFAULT_PDF_LIMITS,
    PdfByteValidationError,
    PdfEncryptedError,
    PdfLimits,
    PdfPageLimitError,
    PdfParserError,
    ProcessOutputLimitError,
    ProcessResult,
    ProcessRunner,
    ProcessTimeoutError,
    validate_pdf,
    validate_pdf_fd,
)


PDF = (Path(__file__).parent / "fixtures" / "pdf" / "minimal.pdf").read_bytes()


def test_default_limits_pin_reviewed_p06_boundaries() -> None:
    assert DEFAULT_PDF_LIMITS == PdfLimits(
        source_bytes=256 * 1024 * 1024,
        pages=2_000,
        raster_pixels=100_000_000,
        wall_seconds=120,
        cpu_seconds=120,
        address_space_bytes=2 * 1024 * 1024 * 1024,
        open_files=256,
        generated_file_bytes=512 * 1024 * 1024,
        stdout_bytes=512 * 1024 * 1024,
        stderr_bytes=1024 * 1024,
        total_output_bytes=512 * 1024 * 1024,
    )


@pytest.mark.parametrize("field", tuple(PdfLimits.__dataclass_fields__))
@pytest.mark.parametrize("invalid", (0, -1, True))
def test_limits_reject_nonpositive_or_boolean_values(field: str, invalid: object) -> None:
    with pytest.raises(ValueError):
        replace(DEFAULT_PDF_LIMITS, **{field: invalid})


def test_descriptor_validation_streams_hashes_and_preserves_offset(tmp_path: Path) -> None:
    payload = PDF[:5] + b"x" * (1024 * 1024 + 17) + b"\n%%EOF\n"
    source = tmp_path / "large.pdf"
    source.write_bytes(payload)
    descriptor = os.open(source, os.O_RDONLY)
    try:
        os.lseek(descriptor, 11, os.SEEK_SET)
        result = validate_pdf_fd(descriptor, max_bytes=len(payload))
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 11
    finally:
        os.close(descriptor)

    assert result.size == len(payload)
    assert result.md5 == hashlib.md5(payload, usedforsecurity=False).hexdigest()
    assert result.sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    "payload",
    (
        b"",
        b"not-a-pdf\n%%EOF\n",
        b"%PDF-1.4\nmissing eof",
        b"%PDF-1.4\n%%EOF\ntrailing-data",
    ),
)
def test_descriptor_validation_rejects_invalid_pdf_envelopes(
    tmp_path: Path, payload: bytes
) -> None:
    source = tmp_path / "invalid.pdf"
    source.write_bytes(payload)
    descriptor = os.open(source, os.O_RDONLY)
    try:
        with pytest.raises(PdfByteValidationError):
            validate_pdf_fd(descriptor, max_bytes=max(1, len(payload)))
    finally:
        os.close(descriptor)


def test_descriptor_validation_rejects_nonregular_and_oversized_inputs(tmp_path: Path) -> None:
    directory_fd = os.open(tmp_path, os.O_RDONLY)
    try:
        with pytest.raises(PdfByteValidationError):
            validate_pdf_fd(directory_fd, max_bytes=1024)
    finally:
        os.close(directory_fd)

    source = tmp_path / "oversized.pdf"
    source.write_bytes(PDF)
    descriptor = os.open(source, os.O_RDONLY)
    try:
        with pytest.raises(PdfByteValidationError):
            validate_pdf_fd(descriptor, max_bytes=len(PDF) - 1)
    finally:
        os.close(descriptor)


class InspectingRunner:
    def __init__(self, source: Path, stdout: bytes) -> None:
        self.source = source
        self.stdout = stdout
        self.calls = 0

    def run(
        self,
        executable: str | Path,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        limits: PdfLimits,
    ) -> ProcessResult:
        self.calls += 1
        assert executable == "pdfinfo"
        assert arguments[:2] == ("-enc", "UTF-8")
        copied_source = Path(arguments[2])
        assert copied_source != self.source
        assert copied_source.parent == cwd
        assert copied_source.read_bytes() == self.source.read_bytes()
        assert (cwd / ".desinfect-generated-root").is_file()
        assert limits.pages == 2_000
        return ProcessResult(
            argv=("/usr/bin/pdfinfo", *arguments),
            executable_sha256="a" * 64,
            returncode=0,
            stdout=self.stdout,
            stderr=b"",
        )


def test_pdfinfo_validates_stable_copy_and_owned_temp_is_cleaned(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    caller_owned = temp_root / "keep.txt"
    caller_owned.write_text("keep", encoding="utf-8")
    runner = InspectingRunner(source, b"Pages: 1\nEncrypted: no\n")

    result = validate_pdf(source, temp_root=temp_root, runner=runner)

    assert result.pages == 1
    assert result.bytes.sha256 == hashlib.sha256(PDF).hexdigest()
    assert runner.calls == 1
    assert tuple(temp_root.iterdir()) == (caller_owned,)
    assert source.read_bytes() == PDF


@pytest.mark.parametrize(
    ("stdout", "error"),
    (
        (b"Pages: 0\nEncrypted: no\n", PdfParserError),
        (b"Pages: 2001\nEncrypted: no\n", PdfPageLimitError),
        (b"Pages: 1\nEncrypted: yes\n", PdfEncryptedError),
        (b"Pages: one\nEncrypted: no\n", PdfParserError),
        (b"Pages: 1\n", PdfParserError),
    ),
)
def test_pdfinfo_failures_are_explicit_and_leave_source_unchanged(
    tmp_path: Path, stdout: bytes, error: type[Exception]
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    temp_root = tmp_path / "temp"
    temp_root.mkdir()

    with pytest.raises(error):
        validate_pdf(source, temp_root=temp_root, runner=InspectingRunner(source, stdout))

    assert source.read_bytes() == PDF
    assert tuple(temp_root.iterdir()) == ()


def test_symlink_source_is_rejected_before_tool_execution(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    link = tmp_path / "link.pdf"
    link.symlink_to(source)
    runner = InspectingRunner(link, b"Pages: 1\nEncrypted: no\n")

    with pytest.raises(PdfByteValidationError):
        validate_pdf(link, temp_root=tmp_path / "temp", runner=runner)

    assert runner.calls == 0


def test_owned_temp_is_cleaned_on_base_exception(tmp_path: Path) -> None:
    class InterruptingRunner:
        def run(self, *args: object, **kwargs: object) -> ProcessResult:
            raise KeyboardInterrupt("cancelled")

    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    temp_root = tmp_path / "temp"

    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        validate_pdf(source, temp_root=temp_root, runner=InterruptingRunner())

    assert tuple(temp_root.iterdir()) == ()
    assert source.read_bytes() == PDF


def test_owned_temp_unlinks_tool_symlink_without_touching_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")

    class SymlinkingRunner:
        def run(
            self,
            executable: str | Path,
            arguments: tuple[str, ...],
            *,
            cwd: Path,
            limits: PdfLimits,
        ) -> ProcessResult:
            del executable, arguments, limits
            (cwd / "tool-link").symlink_to(outside)
            raise KeyboardInterrupt("cancelled")

    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    temp_root = tmp_path / "temp"

    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        validate_pdf(source, temp_root=temp_root, runner=SymlinkingRunner())

    assert outside.read_text(encoding="utf-8") == "keep"
    assert tuple(temp_root.iterdir()) == ()


def test_process_runner_uses_fixed_env_absolute_tool_hash_and_rlimits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DESINFECT_SECRET_SHOULD_NOT_LEAK", "secret")
    code = (
        "import json,os,resource,sys;"
        "print(json.dumps({'args':sys.argv[1:],'env':dict(os.environ),"
        "'limits':{n:resource.getrlimit(getattr(resource,n))[0] for n in "
        "('RLIMIT_CPU','RLIMIT_AS','RLIMIT_FSIZE','RLIMIT_NOFILE')}}))"
    )
    result = ProcessRunner().run(
        sys.executable,
        ("-c", code, "literal argument"),
        cwd=tmp_path,
        limits=DEFAULT_PDF_LIMITS,
    )
    payload = json.loads(result.stdout)

    resolved = Path(sys.executable).resolve(strict=True)
    assert result.argv[0] == resolved.as_posix()
    assert result.executable_sha256 == hashlib.sha256(resolved.read_bytes()).hexdigest()
    assert payload["args"] == ["literal argument"]
    assert payload["env"] == {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "TZ": "UTC",
    }
    assert payload["limits"] == {
        "RLIMIT_CPU": 120,
        "RLIMIT_AS": 2 * 1024 * 1024 * 1024,
        "RLIMIT_FSIZE": 512 * 1024 * 1024,
        "RLIMIT_NOFILE": 256,
    }


@pytest.mark.parametrize(
    ("stream", "code"),
    (
        ("stdout", "import sys;sys.stdout.write('12345');sys.stdout.flush()"),
        ("stderr", "import sys;sys.stderr.write('12345');sys.stderr.flush()"),
    ),
)
def test_process_runner_kills_on_bounded_capture(
    tmp_path: Path, stream: str, code: str
) -> None:
    limits = replace(DEFAULT_PDF_LIMITS, stdout_bytes=4, stderr_bytes=4)
    with pytest.raises(ProcessOutputLimitError, match=stream):
        ProcessRunner().run(sys.executable, ("-c", code), cwd=tmp_path, limits=limits)


def test_process_runner_enforces_generated_file_limit(tmp_path: Path) -> None:
    code = "open('too-large.bin','wb').write(b'12345')"
    limits = replace(
        DEFAULT_PDF_LIMITS,
        generated_file_bytes=4,
        total_output_bytes=16,
    )

    with pytest.raises(ProcessOutputLimitError, match="Datei"):
        ProcessRunner().run(sys.executable, ("-c", code), cwd=tmp_path, limits=limits)


def test_process_runner_enforces_total_output_limit(tmp_path: Path) -> None:
    code = "open('a.bin','wb').write(b'1234');open('b.bin','wb').write(b'5678')"
    limits = replace(
        DEFAULT_PDF_LIMITS,
        generated_file_bytes=16,
        total_output_bytes=7,
    )

    with pytest.raises(ProcessOutputLimitError, match="Gesamtausgabe"):
        ProcessRunner().run(sys.executable, ("-c", code), cwd=tmp_path, limits=limits)


def test_process_runner_timeout_kills_entire_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-survived"
    grandchild = (
        "import pathlib,time;time.sleep(0.4);"
        f"pathlib.Path({str(marker)!r}).write_text('leaked')"
    )
    parent = (
        "import subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
        "time.sleep(10)"
    )
    limits = replace(DEFAULT_PDF_LIMITS, wall_seconds=0.1)

    with pytest.raises(ProcessTimeoutError):
        ProcessRunner().run(sys.executable, ("-c", parent), cwd=tmp_path, limits=limits)

    time.sleep(0.5)
    assert not marker.exists()
