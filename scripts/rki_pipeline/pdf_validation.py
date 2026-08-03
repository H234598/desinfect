#!/usr/bin/env python3
"""Shared PDF byte validation and bounded no-shell tool execution."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import hashlib
import os
from pathlib import Path
import resource
import selectors
import shutil
import signal
import stat
import subprocess
import time
from typing import Iterator, Protocol
import uuid

from scripts.rki_pipeline.io_utils import (
    assert_generated_root_fd,
    fd_directory_path,
    fsync_directory_fd,
    mark_generated_root_fd,
    open_directory_beneath,
    open_root_directory,
)


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_READ_CHUNK = 1024 * 1024
_CAPTURE_CHUNK = 64 * 1024
_FIXED_ENV = {
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PATH": os.defpath,
    "TZ": "UTC",
}


class PdfValidationError(ValueError):
    """A PDF cannot be validated within the reviewed contract."""


class PdfByteValidationError(PdfValidationError):
    """PDF bytes, descriptor type, or checksum are invalid."""


class PdfSizeLimitError(PdfByteValidationError):
    """PDF byte size lies outside the configured boundary."""


class PdfParserError(PdfValidationError):
    """Poppler cannot parse the PDF or returned malformed evidence."""


class PdfEncryptedError(PdfParserError):
    """Encrypted PDFs are not accepted for unattended conversion."""


class PdfPageLimitError(PdfParserError):
    """PDF page count exceeds the reviewed boundary."""


class ProcessRunnerError(RuntimeError):
    """A bounded tool process cannot complete safely."""


class ProcessTimeoutError(ProcessRunnerError):
    """A tool exceeded its wall-clock deadline."""


class ProcessOutputLimitError(ProcessRunnerError):
    """A tool exceeded a captured or generated output boundary."""


class ProcessExecutionError(ProcessRunnerError):
    """A tool exited unsuccessfully."""

    def __init__(self, result: ProcessResult) -> None:
        super().__init__(f"Tool-Prozess endete mit Status {result.returncode}")
        self.result = result


@dataclass(frozen=True, slots=True)
class PdfLimits:
    """Reviewed hard ceilings for one PDF and one tool invocation."""

    source_bytes: int = 256 * 1024 * 1024
    pages: int = 2_000
    raster_pixels: int = 100_000_000
    wall_seconds: float = 120
    cpu_seconds: int = 120
    address_space_bytes: int = 2 * 1024 * 1024 * 1024
    open_files: int = 256
    generated_file_bytes: int = 512 * 1024 * 1024
    stdout_bytes: int = 512 * 1024 * 1024
    stderr_bytes: int = 1024 * 1024
    total_output_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            valid = (
                type(value) in {int, float}
                if name == "wall_seconds"
                else type(value) is int
            )
            if not valid or value <= 0:
                raise ValueError(f"{name} muss eine positive Zahl sein")


DEFAULT_PDF_LIMITS = PdfLimits()


@dataclass(frozen=True, slots=True)
class PdfByteValidation:
    """Streaming byte evidence shared by downloader and converter."""

    size: int
    md5: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ProcessResult:
    """Bounded process output plus immutable executable evidence."""

    argv: tuple[str, ...]
    executable_sha256: str
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True, slots=True)
class PdfValidation:
    """Parser and byte evidence for one unencrypted PDF."""

    bytes: PdfByteValidation
    pages: int
    encrypted: bool
    parser: ProcessResult


@dataclass(frozen=True, slots=True)
class ValidatedPdfCopy:
    """Validated private source copy that exists only inside its context."""

    path: Path
    validation: PdfValidation


class Runner(Protocol):
    """Injected bounded-process port used by PDF validation tests."""

    def run(
        self,
        executable: str | Path,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        limits: PdfLimits,
    ) -> ProcessResult: ...


def _validate_positive_limit(value: int, name: str) -> None:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} muss eine positive Ganzzahl sein")


def validate_pdf_fd(
    descriptor: int,
    *,
    max_bytes: int,
    expected_md5: str | None = None,
) -> PdfByteValidation:
    """Validate and hash one regular PDF descriptor without changing its offset."""

    _validate_positive_limit(max_bytes, "max_bytes")
    try:
        before = os.fstat(descriptor)
    except OSError as exc:
        raise PdfByteValidationError("PDF-Deskriptor ist nicht lesbar") from exc
    if not stat.S_ISREG(before.st_mode):
        raise PdfByteValidationError(
            "PDF-Deskriptor verweist nicht auf eine reguläre Datei"
        )
    if before.st_size <= 0 or before.st_size > max_bytes:
        raise PdfSizeLimitError(
            f"PDF-Größe {before.st_size} liegt außerhalb 1..{max_bytes} Bytes"
        )
    try:
        prefix = os.pread(descriptor, 5, 0)
    except OSError as exc:
        raise PdfByteValidationError("PDF-Deskriptor ist nicht seekbar") from exc
    if prefix != b"%PDF-":
        raise PdfByteValidationError(f"Ungültige PDF-Magic: {prefix!r}")

    md5 = hashlib.md5(usedforsecurity=False)
    sha256 = hashlib.sha256()
    offset = 0
    tail = b""
    while offset < before.st_size:
        chunk = os.pread(
            descriptor,
            min(_READ_CHUNK, before.st_size - offset),
            offset,
        )
        if not chunk:
            raise PdfByteValidationError("PDF wurde während der Prüfung verkürzt")
        offset += len(chunk)
        md5.update(chunk)
        sha256.update(chunk)
        tail = (tail + chunk)[-4096:]
    after = os.fstat(descriptor)
    if (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise PdfByteValidationError("PDF änderte sich während der Byteprüfung")
    if not tail.rstrip().endswith(b"%%EOF"):
        raise PdfByteValidationError("PDF besitzt keinen abschließenden %%EOF-Marker")
    md5_hex = md5.hexdigest()
    if expected_md5 is not None and md5_hex != expected_md5.lower():
        raise PdfByteValidationError(
            f"MD5-Prüfung fehlgeschlagen: {md5_hex} != {expected_md5.lower()}"
        )
    return PdfByteValidation(before.st_size, md5_hex, sha256.hexdigest())


def _resolve_executable(executable: str | Path) -> Path:
    raw = os.fspath(executable)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ProcessRunnerError("Executable muss ein nichtleerer Pfad oder Name sein")
    candidate = Path(raw)
    if not candidate.is_absolute():
        found = shutil.which(raw, path=os.defpath)
        if found is None:
            raise ProcessRunnerError(f"Tool fehlt: {raw}")
        candidate = Path(found)
    try:
        resolved = candidate.resolve(strict=True)
        metadata = resolved.stat()
    except OSError as exc:
        raise ProcessRunnerError(f"Tool ist nicht sicher auflösbar: {raw}") from exc
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise ProcessRunnerError(f"Tool ist keine ausführbare reguläre Datei: {raw}")
    return resolved


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_fd(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(descriptor, _READ_CHUNK, offset)
        if not chunk:
            return digest.hexdigest()
        offset += len(chunk)
        digest.update(chunk)


def _file_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    metadata = path.stat(follow_symlinks=False)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _fd_identity(descriptor: int) -> tuple[int, int, int, int, int, int]:
    metadata = os.fstat(descriptor)
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _set_child_limits(limits: PdfLimits) -> None:
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CPU, (limits.cpu_seconds, limits.cpu_seconds))
    resource.setrlimit(
        resource.RLIMIT_AS,
        (limits.address_space_bytes, limits.address_space_bytes),
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (limits.generated_file_bytes, limits.generated_file_bytes),
    )
    resource.setrlimit(
        resource.RLIMIT_NOFILE,
        (limits.open_files, limits.open_files),
    )


def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    finally:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as exc:
            raise ProcessRunnerError("Tool-Prozessgruppe konnte nicht beendet werden") from exc


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is not None and not pipe.closed:
            pipe.close()


def _capture_bounded(
    process: subprocess.Popen[bytes],
    *,
    limits: PdfLimits,
    cwd: Path,
    before: dict[str, tuple[int, int, int, int, int, int]],
) -> tuple[bytes, bytes]:
    if process.stdout is None or process.stderr is None:
        raise ProcessRunnerError("Tool-Capture wurde nicht initialisiert")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    ceilings = {"stdout": limits.stdout_bytes, "stderr": limits.stderr_bytes}
    deadline = time.monotonic() + limits.wall_seconds
    try:
        while selector.get_map() or process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _kill_process_group(process)
                raise ProcessTimeoutError(
                    f"Tool überschritt {limits.wall_seconds} Sekunden"
                )
            _validate_output_tree(
                cwd,
                limits,
                before=before,
                captured_bytes=sum(map(len, buffers.values())),
            )
            events = (
                selector.select(min(remaining, 0.05))
                if selector.get_map()
                else ()
            )
            if not events:
                if not selector.get_map() and process.poll() is None:
                    time.sleep(min(remaining, 0.05))
                continue
            for key, _mask in events:
                stream = str(key.data)
                chunk = os.read(key.fd, _CAPTURE_CHUNK)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                buffers[stream].extend(chunk)
                if len(buffers[stream]) > ceilings[stream]:
                    _kill_process_group(process)
                    raise ProcessOutputLimitError(
                        f"Tool-{stream} überschreitet {ceilings[stream]} Bytes"
                    )
                if sum(map(len, buffers.values())) > limits.total_output_bytes:
                    _kill_process_group(process)
                    raise ProcessOutputLimitError(
                        "Tool-Gesamtausgabe aus stdout/stderr überschreitet "
                        f"{limits.total_output_bytes} Bytes"
                    )
                _validate_output_tree(
                    cwd,
                    limits,
                    before=before,
                    captured_bytes=sum(map(len, buffers.values())),
                )
    except BaseException:
        _kill_process_group(process)
        raise
    finally:
        selector.close()
        _close_process_pipes(process)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        _kill_process_group(process)
        raise ProcessTimeoutError(f"Tool überschritt {limits.wall_seconds} Sekunden")
    try:
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as exc:
        _kill_process_group(process)
        raise ProcessTimeoutError(
            f"Tool überschritt {limits.wall_seconds} Sekunden"
        ) from exc
    return bytes(buffers["stdout"]), bytes(buffers["stderr"])


def _snapshot_output_tree(cwd: Path) -> dict[str, tuple[int, int, int, int, int, int]]:
    snapshot: dict[str, tuple[int, int, int, int, int, int]] = {}
    for root, directories, files in os.walk(cwd, followlinks=False):
        for name in (*directories, *files):
            path = Path(root) / name
            metadata = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ProcessOutputLimitError(f"Symlink im Tool-Arbeitsbaum: {path.name}")
            snapshot[path.relative_to(cwd).as_posix()] = _file_identity(path)
    return snapshot


def _validate_output_tree(
    cwd: Path,
    limits: PdfLimits,
    *,
    before: dict[str, tuple[int, int, int, int, int, int]],
    captured_bytes: int,
) -> None:
    total = captured_bytes
    if total > limits.total_output_bytes:
        raise ProcessOutputLimitError(
            f"Tool-Gesamtausgabe überschreitet {limits.total_output_bytes} Bytes"
        )
    for relative, expected in before.items():
        path = cwd / relative
        try:
            current = _file_identity(path)
        except FileNotFoundError as exc:
            raise ProcessOutputLimitError(
                f"Tool entfernte vorhandenen Arbeitsbaum-Eintrag: {relative}"
            ) from exc
        unchanged = (
            current[:3] == expected[:3]
            if stat.S_ISDIR(expected[2])
            else current == expected
        )
        if not unchanged:
            raise ProcessOutputLimitError(
                f"Tool veränderte vorhandenen Arbeitsbaum-Eintrag: {relative}"
            )
    for root, directories, files in os.walk(cwd, followlinks=False):
        for name in (*directories, *files):
            path = Path(root) / name
            metadata = path.stat(follow_symlinks=False)
            if stat.S_ISLNK(metadata.st_mode):
                raise ProcessOutputLimitError(f"Symlink im Tool-Output: {path.name}")
            if not stat.S_ISREG(metadata.st_mode):
                continue
            relative = path.relative_to(cwd).as_posix()
            if relative in before:
                continue
            if metadata.st_size >= limits.generated_file_bytes:
                raise ProcessOutputLimitError(
                    f"Tool-Datei erreicht Grenze von {limits.generated_file_bytes} Bytes"
                )
            total += metadata.st_size
            if total > limits.total_output_bytes:
                raise ProcessOutputLimitError(
                    f"Tool-Gesamtausgabe überschreitet {limits.total_output_bytes} Bytes"
                )


class ProcessRunner:
    """Run one local tool with fixed environment and hard POSIX boundaries."""

    def run(
        self,
        executable: str | Path,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        limits: PdfLimits = DEFAULT_PDF_LIMITS,
    ) -> ProcessResult:
        if not isinstance(arguments, tuple) or any(
            type(value) is not str or "\x00" in value for value in arguments
        ):
            raise ProcessRunnerError("Tool-Argumente müssen ein NUL-freies Tupel sein")
        workdir_path = Path(os.path.abspath(cwd))
        if workdir_path.is_symlink():
            raise ProcessRunnerError("Tool-Arbeitsverzeichnis darf kein Symlink sein")
        try:
            workdir_fd = os.open(
                workdir_path,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | _CLOEXEC
                | _NOFOLLOW,
            )
        except OSError as exc:
            raise ProcessRunnerError("Tool-Arbeitsverzeichnis fehlt") from exc
        try:
            if not stat.S_ISDIR(os.fstat(workdir_fd).st_mode):
                raise ProcessRunnerError("Tool-Arbeitsverzeichnis ist kein Verzeichnis")
            workdir = fd_directory_path(workdir_fd)
            resolved = _resolve_executable(executable)
            executable_identity = _file_identity(resolved)
            executable_sha256 = _sha256_path(resolved)
            if _file_identity(resolved) != executable_identity:
                raise ProcessRunnerError("Tool änderte sich während der Hashprüfung")
            try:
                executable_fd = os.open(resolved, os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
            except OSError as exc:
                raise ProcessRunnerError("Tool änderte sich vor dem Prozessstart") from exc
            try:
                if _fd_identity(executable_fd) != executable_identity:
                    raise ProcessRunnerError("Tool änderte sich vor dem Prozessstart")
                if _sha256_fd(executable_fd) != executable_sha256:
                    raise ProcessRunnerError("Toolinhalt änderte sich vor dem Prozessstart")
                if _fd_identity(executable_fd) != executable_identity:
                    raise ProcessRunnerError("Tool änderte sich vor dem Prozessstart")
                argv = (resolved.as_posix(), *arguments)
                before = _snapshot_output_tree(workdir)
                try:
                    process = subprocess.Popen(
                        argv,
                        executable=fd_directory_path(executable_fd),
                        cwd=workdir,
                        env=dict(_FIXED_ENV),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        shell=False,
                        close_fds=True,
                        pass_fds=(workdir_fd, executable_fd),
                        start_new_session=True,
                        preexec_fn=lambda: _set_child_limits(limits),
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    raise ProcessRunnerError(
                        f"Tool konnte nicht gestartet werden: {resolved.name}"
                    ) from exc
                try:
                    stdout, stderr = _capture_bounded(
                        process,
                        limits=limits,
                        cwd=workdir,
                        before=before,
                    )
                    _validate_output_tree(
                        workdir,
                        limits,
                        before=before,
                        captured_bytes=len(stdout) + len(stderr),
                    )
                    result = ProcessResult(
                        argv=argv,
                        executable_sha256=executable_sha256,
                        returncode=process.returncode,
                        stdout=stdout,
                        stderr=stderr,
                    )
                    if result.returncode != 0:
                        raise ProcessExecutionError(result)
                    _kill_process_group(process)
                    return result
                except BaseException:
                    _kill_process_group(process)
                    raise
                finally:
                    _close_process_pipes(process)
            finally:
                os.close(executable_fd)
        finally:
            os.close(workdir_fd)


@dataclass(frozen=True, slots=True)
class _OwnedDirectory:
    path: Path
    descriptor: int


def _remove_owned_contents(directory_fd: int) -> None:
    """Delete owned entries descriptor-relatively without following symlinks."""

    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child_fd = open_directory_beneath(directory_fd, (name,))
            try:
                _remove_owned_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _remove_owned_directory(parent_fd: int, name: str) -> None:
    directory_fd = open_directory_beneath(parent_fd, (name,))
    try:
        assert_generated_root_fd(directory_fd)
        _remove_owned_contents(directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)
    fsync_directory_fd(parent_fd)


@contextmanager
def _owned_directory(parent: Path) -> Iterator[_OwnedDirectory]:
    name = f".desinfect-pdf-{uuid.uuid4().hex}"
    with open_root_directory(parent, create=True) as parent_fd:
        descriptor: int | None = None
        created = False
        marked = False
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
            created = True
            descriptor = open_directory_beneath(parent_fd, (name,))
            mark_generated_root_fd(descriptor)
            marked = True
            yield _OwnedDirectory(Path(os.path.abspath(parent)) / name, descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if created:
                if marked:
                    _remove_owned_directory(parent_fd, name)
                else:
                    directory_fd = open_directory_beneath(parent_fd, (name,))
                    try:
                        _remove_owned_contents(directory_fd)
                    finally:
                        os.close(directory_fd)
                    os.rmdir(name, dir_fd=parent_fd)
                    fsync_directory_fd(parent_fd)


def _copy_descriptor(
    source_fd: int,
    target_directory_fd: int,
    name: str,
    *,
    expected_size: int,
) -> int:
    _validate_positive_limit(expected_size, "expected_size")
    target_fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW,
        0o600,
        dir_fd=target_directory_fd,
    )
    try:
        offset = 0
        while offset < expected_size:
            chunk = os.pread(
                source_fd,
                min(_READ_CHUNK, expected_size - offset),
                offset,
            )
            if not chunk:
                raise PdfByteValidationError("PDF wurde vor stabiler Tempkopie verkürzt")
            offset += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(target_fd, view)
                view = view[written:]
        os.fsync(target_fd)
        return target_fd
    except BaseException:
        os.close(target_fd)
        raise


def _open_pdf(path: Path) -> int:
    absolute = Path(os.path.abspath(path))
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise PdfByteValidationError(f"PDF ist nicht lesbar: {path}") from exc
    if resolved != absolute:
        raise PdfByteValidationError("PDF-Quellpfad enthält eine Symlink-Komponente")
    path = absolute
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise PdfByteValidationError(f"PDF ist nicht lesbar: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise PdfByteValidationError("PDF-Quellpfad darf kein Symlink sein")
    try:
        return os.open(path, os.O_RDONLY | _CLOEXEC | _NOFOLLOW)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise PdfByteValidationError("PDF-Quellpfad ist unsicher") from exc
        raise PdfByteValidationError(f"PDF ist nicht lesbar: {path}") from exc


def _parse_pdfinfo(stdout: bytes, *, max_pages: int) -> tuple[int, bool]:
    try:
        lines = stdout.decode("utf-8", errors="strict").splitlines()
    except UnicodeDecodeError as exc:
        raise PdfParserError("pdfinfo-Ausgabe ist nicht UTF-8") from exc
    values: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in {"Pages", "Encrypted"}:
            if key in values:
                raise PdfParserError(f"pdfinfo-Feld ist doppelt: {key}")
            values[key] = value.strip()
    if set(values) != {"Pages", "Encrypted"}:
        raise PdfParserError("pdfinfo-Ausgabe enthält keine vollständigen PDF-Grenzen")
    if values["Encrypted"].lower() not in {"yes", "no"}:
        raise PdfParserError("pdfinfo-Verschlüsselungswert ist ungültig")
    encrypted = values["Encrypted"].lower() == "yes"
    if encrypted:
        raise PdfEncryptedError("Verschlüsselte PDF ist nicht konvertierbar")
    try:
        pages = int(values["Pages"])
    except ValueError as exc:
        raise PdfParserError("pdfinfo-Seitenzahl ist ungültig") from exc
    if pages <= 0:
        raise PdfParserError("pdfinfo-Seitenzahl muss positiv sein")
    if pages > max_pages:
        raise PdfPageLimitError(f"PDF überschreitet {max_pages} Seiten")
    return pages, encrypted


@contextmanager
def validated_pdf(
    path: Path,
    *,
    temp_root: Path,
    runner: Runner | None = None,
    limits: PdfLimits = DEFAULT_PDF_LIMITS,
    pdfinfo_executable: str | Path = "pdfinfo",
) -> Iterator[ValidatedPdfCopy]:
    """Yield one parser-validated private copy and securely remove it afterward."""

    source = Path(path)
    source_fd = _open_pdf(source)
    try:
        byte_validation = validate_pdf_fd(source_fd, max_bytes=limits.source_bytes)
        with _owned_directory(Path(temp_root)) as owned:
            copied_fd = _copy_descriptor(
                source_fd,
                owned.descriptor,
                "source.pdf",
                expected_size=byte_validation.size,
            )
            try:
                copied_validation = validate_pdf_fd(
                    copied_fd,
                    max_bytes=limits.source_bytes,
                )
            finally:
                os.close(copied_fd)
            if copied_validation != byte_validation:
                raise PdfByteValidationError("Stabile PDF-Tempkopie driftet von der Quelle")
            if validate_pdf_fd(source_fd, max_bytes=limits.source_bytes) != byte_validation:
                raise PdfByteValidationError("PDF-Quelle änderte sich während der Tempkopie")
            arguments = ("-enc", "UTF-8", "source.pdf")
            active_runner = runner if runner is not None else ProcessRunner()
            try:
                parser = active_runner.run(
                    pdfinfo_executable,
                    arguments,
                    cwd=owned.path,
                    limits=limits,
                )
            except ProcessRunnerError as exc:
                raise PdfParserError("pdfinfo konnte PDF nicht parserisch öffnen") from exc
            try:
                post_tool_fd = os.open(
                    "source.pdf",
                    os.O_RDONLY | _CLOEXEC | _NOFOLLOW,
                    dir_fd=owned.descriptor,
                )
            except OSError as exc:
                raise PdfByteValidationError(
                    "Tool entfernte oder ersetzte stabile PDF-Tempkopie"
                ) from exc
            try:
                post_tool_validation = validate_pdf_fd(
                    post_tool_fd,
                    max_bytes=limits.source_bytes,
                )
            except PdfByteValidationError as exc:
                raise PdfByteValidationError(
                    "Tool veränderte stabile PDF-Tempkopie"
                ) from exc
            finally:
                os.close(post_tool_fd)
            if post_tool_validation != byte_validation:
                raise PdfByteValidationError("Tool veränderte stabile PDF-Tempkopie")
            pages, encrypted = _parse_pdfinfo(parser.stdout, max_pages=limits.pages)
            yield ValidatedPdfCopy(
                owned.path / "source.pdf",
                PdfValidation(byte_validation, pages, encrypted, parser),
            )
    finally:
        os.close(source_fd)


def validate_pdf(
    path: Path,
    *,
    temp_root: Path,
    runner: Runner | None = None,
    limits: PdfLimits = DEFAULT_PDF_LIMITS,
    pdfinfo_executable: str | Path = "pdfinfo",
) -> PdfValidation:
    """Return validation evidence while retaining no private source copy."""

    with validated_pdf(
        path,
        temp_root=temp_root,
        runner=runner,
        limits=limits,
        pdfinfo_executable=pdfinfo_executable,
    ) as value:
        return value.validation
