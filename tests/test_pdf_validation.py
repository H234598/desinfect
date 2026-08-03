"""Shared PDF validation and bounded subprocess regressions."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import time

import pytest

from scripts.rki_pipeline import pdf_validation as pdf_validation_module
from scripts.rki_pipeline.pdf_validation import (
    DEFAULT_PDF_LIMITS,
    PdfByteValidationError,
    PdfEncryptedError,
    PdfLimits,
    PdfPageLimitError,
    PdfParserError,
    ProcessExecutionError,
    ProcessOutputLimitError,
    ProcessResult,
    ProcessRunner,
    ProcessRunnerError,
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
        assert arguments[2] == "source.pdf"
        copied_source = cwd / arguments[2]
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


def test_validated_pdf_exposes_copy_only_for_context_lifetime(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    temp_root = tmp_path / "temp"
    runner = InspectingRunner(source, b"Pages: 1\nEncrypted: no\n")

    with pdf_validation_module.validated_pdf(
        source,
        temp_root=temp_root,
        runner=runner,
    ) as validated:
        copy_path = validated.path
        owned_root = copy_path.parent
        assert copy_path.read_bytes() == PDF
        assert validated.validation.pages == 1

    assert not owned_root.exists()
    assert tuple(temp_root.iterdir()) == ()
    assert source.read_bytes() == PDF


@pytest.mark.parametrize("mutation", ("modify", "delete"))
def test_validated_pdf_rejects_tool_mutation_of_private_input(
    tmp_path: Path, mutation: str
) -> None:
    class MutatingRunner:
        def run(
            self,
            executable: str | Path,
            arguments: tuple[str, ...],
            *,
            cwd: Path,
            limits: PdfLimits,
        ) -> ProcessResult:
            del executable, limits
            copied = cwd / arguments[2]
            if mutation == "delete":
                copied.unlink()
            else:
                copied.write_bytes(PDF + b"\n")
            return ProcessResult(
                argv=("/usr/bin/pdfinfo", *arguments),
                executable_sha256="a" * 64,
                returncode=0,
                stdout=b"Pages: 1\nEncrypted: no\n",
                stderr=b"",
            )

    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    temp_root = tmp_path / "temp"

    with pytest.raises(PdfByteValidationError, match="Tempkopie|Tool"):
        validate_pdf(source, temp_root=temp_root, runner=MutatingRunner())

    assert source.read_bytes() == PDF
    assert tuple(temp_root.iterdir()) == ()


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


def test_symlinked_source_parent_is_rejected_before_tool_execution(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    source = real_parent / "source.pdf"
    source.write_bytes(PDF)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    linked_source = linked_parent / source.name
    runner = InspectingRunner(linked_source, b"Pages: 1\nEncrypted: no\n")

    with pytest.raises(PdfByteValidationError, match="Symlink"):
        validate_pdf(linked_source, temp_root=tmp_path / "temp", runner=runner)

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
        total_output_bytes=1024,
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


def test_process_runner_combines_files_and_captured_output_in_total_limit(
    tmp_path: Path,
) -> None:
    code = "import sys;open('a.bin','wb').write(b'1234');sys.stdout.write('5678')"
    limits = replace(
        DEFAULT_PDF_LIMITS,
        generated_file_bytes=16,
        stdout_bytes=16,
        stderr_bytes=16,
        total_output_bytes=7,
    )

    with pytest.raises(ProcessOutputLimitError, match="Gesamtausgabe"):
        ProcessRunner().run(sys.executable, ("-c", code), cwd=tmp_path, limits=limits)


def test_process_runner_reaps_child_when_capture_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[object] = []

    def interrupt_capture(
        process: object,
        *,
        limits: PdfLimits,
        cwd: Path,
        before: dict[str, tuple[int, int, int, int, int, int]],
    ) -> tuple[bytes, bytes]:
        del limits, cwd, before
        started.append(process)
        raise KeyboardInterrupt("cancelled")

    monkeypatch.setattr(pdf_validation_module, "_capture_bounded", interrupt_capture)
    try:
        with pytest.raises(KeyboardInterrupt, match="cancelled"):
            ProcessRunner().run(
                sys.executable,
                ("-c", "import time;time.sleep(10)"),
                cwd=tmp_path,
                limits=DEFAULT_PDF_LIMITS,
            )
        process = started[0]
        assert process.poll() is not None
    finally:
        if started and started[0].poll() is None:
            os.killpg(started[0].pid, 9)
            started[0].wait(timeout=5)


def test_process_runner_rejects_executable_identity_drift_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "replacement-ran"
    executable = tmp_path / "tool"
    replacement = tmp_path / "replacement"
    executable.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    replacement.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    replacement.chmod(0o700)
    original_hash = pdf_validation_module._sha256_path

    def hash_then_swap(path: Path) -> str:
        digest = original_hash(path)
        os.replace(replacement, path)
        return digest

    monkeypatch.setattr(pdf_validation_module, "_sha256_path", hash_then_swap)

    with pytest.raises(ProcessRunnerError, match="änderte"):
        ProcessRunner().run(executable, (), cwd=tmp_path, limits=DEFAULT_PDF_LIMITS)

    assert not marker.exists()


def test_process_runner_wall_deadline_survives_early_pipe_eof(tmp_path: Path) -> None:
    code = "import os,time;os.close(1);os.close(2);time.sleep(1)"
    limits = replace(DEFAULT_PDF_LIMITS, wall_seconds=0.1)
    started = time.monotonic()

    with pytest.raises(ProcessTimeoutError):
        ProcessRunner().run(sys.executable, ("-c", code), cwd=tmp_path, limits=limits)

    assert time.monotonic() - started < 0.8


def test_descriptor_copy_reads_exact_validated_size(tmp_path: Path) -> None:
    source = tmp_path / "growing.pdf"
    source.write_bytes(PDF + b"unvalidated-growth")
    target = tmp_path / "copy"
    source_fd = os.open(source, os.O_RDONLY)
    directory_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    copied_fd = -1
    try:
        copied_fd = pdf_validation_module._copy_descriptor(
            source_fd,
            directory_fd,
            "copy",
            expected_size=len(PDF),
        )
    finally:
        os.close(source_fd)
        os.close(directory_fd)
        if copied_fd >= 0:
            os.close(copied_fd)

    assert target.read_bytes() == PDF


def test_process_runner_enforces_disk_total_before_tool_can_continue(tmp_path: Path) -> None:
    marker = tmp_path / "continued-after-limit"
    code = (
        "import pathlib,time;"
        "pathlib.Path('a.bin').write_bytes(b'1234');"
        "pathlib.Path('b.bin').write_bytes(b'5678');"
        "time.sleep(0.3);"
        f"pathlib.Path({str(marker)!r}).touch()"
    )
    limits = replace(
        DEFAULT_PDF_LIMITS,
        wall_seconds=1,
        generated_file_bytes=16,
        total_output_bytes=7,
    )

    with pytest.raises(ProcessOutputLimitError, match="Gesamtausgabe"):
        ProcessRunner().run(sys.executable, ("-c", code), cwd=tmp_path, limits=limits)

    assert not marker.exists()


def test_process_runner_nonzero_exit_kills_remaining_group(tmp_path: Path) -> None:
    marker = tmp_path / "nonzero-grandchild-survived"
    grandchild = (
        "import pathlib,time;time.sleep(0.3);"
        f"pathlib.Path({str(marker)!r}).touch()"
    )
    parent = (
        "import os,subprocess,sys;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "sys.exit(7)"
    )

    with pytest.raises(ProcessExecutionError):
        ProcessRunner().run(
            sys.executable,
            ("-c", parent),
            cwd=tmp_path,
            limits=DEFAULT_PDF_LIMITS,
        )

    time.sleep(0.5)
    assert not marker.exists()


def test_process_runner_output_failure_kills_remaining_group(tmp_path: Path) -> None:
    marker = tmp_path / "output-grandchild-survived"
    grandchild = (
        "import pathlib,time;time.sleep(0.3);"
        f"pathlib.Path({str(marker)!r}).touch()"
    )
    parent = (
        "import pathlib,subprocess,sys,time;"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}],"
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);"
        "pathlib.Path('a.bin').write_bytes(b'12345678');time.sleep(0.1)"
    )
    limits = replace(
        DEFAULT_PDF_LIMITS,
        generated_file_bytes=16,
        total_output_bytes=7,
    )

    with pytest.raises(ProcessOutputLimitError):
        ProcessRunner().run(sys.executable, ("-c", parent), cwd=tmp_path, limits=limits)

    time.sleep(0.5)
    assert not marker.exists()


def test_process_runner_cwd_stays_on_held_directory_after_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "parent"
    workdir = parent / "work"
    moved = parent / "work-real"
    outside = tmp_path / "outside"
    workdir.mkdir(parents=True)
    outside.mkdir()
    original_popen = pdf_validation_module.subprocess.Popen

    def swap_then_popen(*args: object, **kwargs: object):
        workdir.rename(moved)
        workdir.symlink_to(outside, target_is_directory=True)
        return original_popen(*args, **kwargs)

    monkeypatch.setattr(pdf_validation_module.subprocess, "Popen", swap_then_popen)

    ProcessRunner().run(
        sys.executable,
        ("-c", "from pathlib import Path;Path('marker').touch()"),
        cwd=workdir,
        limits=DEFAULT_PDF_LIMITS,
    )

    assert (moved / "marker").is_file()
    assert not (outside / "marker").exists()


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
