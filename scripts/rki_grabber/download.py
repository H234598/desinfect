#!/usr/bin/env python3
"""Secure resumable PDF download beneath a held caller-owned output root."""
from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import os
from pathlib import Path
import stat
from typing import Mapping

from scripts.rki_grabber.http import (
    PoliteClient,
    ResponseLike,
    ResponseTooLargeError,
)
from scripts.rki_grabber.models import PdfCandidate, RecordState
from scripts.rki_pipeline.io_utils import (
    fsync_directory_fd,
    open_directory_beneath,
    open_root_directory,
    relative_path_beneath,
)
from scripts.rki_pipeline.pdf_validation import (
    PdfByteValidationError,
    PdfSizeLimitError,
    validate_pdf_fd,
)

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class PdfDownloadError(RuntimeError):
    """Base class for deterministic PDF download and validation failures."""

    code = "download.error"
    retryable = False


class PdfDownloadBusyError(PdfDownloadError):
    """Another writer already owns the complete target download transaction."""

    code = "download.busy"
    retryable = True


class PdfContentTypeError(PdfDownloadError):
    """The server returned a content type that is clearly not a PDF."""

    code = "download.content_type"


class PdfIntegrityError(PdfDownloadError):
    """Downloaded or existing bytes fail PDF or checksum validation."""

    code = "download.integrity"


class PdfRangeError(PdfDownloadError):
    """A resumable response does not match the requested byte range."""

    code = "download.range"
    retryable = True


@dataclass(frozen=True, slots=True)
class DownloadResult:
    """Validated final PDF metadata returned to the grabber API."""

    state: RecordState
    bytes: int
    md5: str
    sha256: str
    relative_path: str
    etag: str | None
    last_modified: str | None
    content_type: str | None


def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Normalize response header names."""

    return {
        str(key).lower(): str(value)
        for key, value in headers.items()
    }


def _regular_entry_size(parent_fd: int, name: str) -> int | None:
    """Return the regular-file size or reject a non-regular existing entry."""

    try:
        metadata = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(metadata.st_mode):
        raise PdfDownloadError(
            f"Ziel oder Partialdatei ist keine reguläre Datei: {name}"
        )
    return metadata.st_size


def _acquire_target_lock(parent_fd: int, target_name: str) -> int:
    """Acquire a non-blocking per-target lock for the whole transaction."""

    lock_name = f".{target_name}.lock"
    descriptor = os.open(
        lock_name,
        os.O_RDWR | os.O_CREAT | _CLOEXEC | _NOFOLLOW,
        0o644,
        dir_fd=parent_fd,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PdfDownloadError(
                f"Download-Lock ist keine reguläre Datei: {lock_name}"
            )
        try:
            fcntl.flock(
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise PdfDownloadBusyError(
                    f"PDF-Ziel ist bereits gesperrt: {target_name}"
                ) from exc
            raise
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release_target_lock(descriptor: int) -> None:
    """Release and close one held per-target transaction lock."""

    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _hash_and_validate_fd(
    descriptor: int,
    *,
    max_bytes: int,
    expected_md5: str | None,
) -> tuple[int, str, str]:
    """Map shared PDF byte evidence to stable downloader exceptions."""

    try:
        result = validate_pdf_fd(
            descriptor,
            max_bytes=max_bytes,
            expected_md5=expected_md5,
        )
    except PdfSizeLimitError as exc:
        raise ResponseTooLargeError(str(exc)) from exc
    except PdfByteValidationError as exc:
        raise PdfIntegrityError(str(exc)) from exc
    return result.size, result.md5, result.sha256


def _validate_content_type(
    headers: Mapping[str, str],
) -> str | None:
    """Accept PDF/octet-stream or an absent content type."""

    content_type = headers.get("content-type")
    if content_type is None:
        return None
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type not in {
        "application/pdf",
        "application/octet-stream",
    }:
        raise PdfContentTypeError(
            f"Unerwarteter PDF Content-Type: {content_type}"
        )
    return media_type


def _validate_content_range(
    value: str | None,
    *,
    expected_start: int,
) -> None:
    """Require a matching ``bytes start-end/total`` range response."""

    if value is None:
        raise PdfRangeError("206-Antwort ohne Content-Range")
    unit, separator, remainder = value.partition(" ")
    span, slash, _total = remainder.partition("/")
    start, dash, _end = span.partition("-")
    if (
        unit.lower() != "bytes"
        or not separator
        or not slash
        or not dash
    ):
        raise PdfRangeError(
            f"Ungültiger Content-Range: {value}"
        )
    try:
        parsed_start = int(start)
    except ValueError as exc:
        raise PdfRangeError(
            f"Ungültiger Content-Range: {value}"
        ) from exc
    if parsed_start != expected_start:
        raise PdfRangeError(
            f"Content-Range beginnt bei {parsed_start}, "
            f"erwartet {expected_start}"
        )


def _declared_length(
    headers: Mapping[str, str],
) -> int | None:
    """Parse a non-negative streaming content length."""

    raw = headers.get("content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise PdfDownloadError(
            "Ungültiger Content-Length-Header"
        ) from exc
    if value < 0:
        raise PdfDownloadError(
            "Negativer Content-Length-Header"
        )
    return value


def _open_response(
    client: PoliteClient,
    candidate: PdfCandidate,
    *,
    resume_size: int,
) -> ResponseLike:
    """Open the first response, optionally requesting a byte range."""

    headers = (
        {"Range": f"bytes={resume_size}-"}
        if resume_size
        else {}
    )
    return client.open_stream(
        candidate.url,
        accept=(
            "application/pdf,"
            "application/octet-stream;q=0.9,*/*;q=0.1"
        ),
        allowed_statuses=frozenset({200, 206, 416}),
        headers=headers,
    )


def download_pdf(
    client: PoliteClient,
    candidate: PdfCandidate,
    target: Path,
    *,
    allowed_root: Path,
    force: bool,
    max_bytes: int,
) -> DownloadResult:
    """Download, resume, validate, hash, and atomically publish one PDF."""

    relative = relative_path_beneath(
        target,
        allowed_root,
    )
    with open_root_directory(
        allowed_root,
        create=True,
    ) as root_fd:
        parent_fd = open_directory_beneath(
            root_fd,
            relative.parts[:-1],
            create=True,
        )
        try:
            lock_fd = _acquire_target_lock(
                parent_fd,
                relative.name,
            )
            try:
                existing_size = _regular_entry_size(
                    parent_fd,
                    relative.name,
                )
                if existing_size is not None and not force:
                    existing_fd = os.open(
                        relative.name,
                        os.O_RDONLY | _CLOEXEC | _NOFOLLOW,
                        dir_fd=parent_fd,
                    )
                    try:
                        size, md5, sha256 = _hash_and_validate_fd(
                            existing_fd,
                            max_bytes=max_bytes,
                            expected_md5=candidate.expected_md5,
                        )
                    finally:
                        os.close(existing_fd)
                    return DownloadResult(
                        state=RecordState.EXISTING,
                        bytes=size,
                        md5=md5,
                        sha256=sha256,
                        relative_path=relative.as_posix(),
                        etag=None,
                        last_modified=None,
                        content_type=None,
                    )

                part_name = f".{relative.name}.part"
                if force:
                    try:
                        os.unlink(
                            part_name,
                            dir_fd=parent_fd,
                        )
                    except FileNotFoundError:
                        pass
                partial_size = (
                    _regular_entry_size(
                        parent_fd,
                        part_name,
                    )
                    or 0
                )
                if partial_size > max_bytes:
                    os.unlink(
                        part_name,
                        dir_fd=parent_fd,
                    )
                    raise ResponseTooLargeError(
                        f"Partialdatei überschreitet "
                        f"{max_bytes} Bytes"
                    )

                flags = (
                    os.O_RDWR
                    | os.O_CREAT
                    | _CLOEXEC
                    | _NOFOLLOW
                )
                descriptor = os.open(
                    part_name,
                    flags,
                    0o644,
                    dir_fd=parent_fd,
                )
                response: ResponseLike | None = None
                try:
                    metadata = os.fstat(descriptor)
                    if not stat.S_ISREG(metadata.st_mode):
                        raise PdfDownloadError(
                            "Partialdatei ist keine reguläre Datei"
                        )
                    response = _open_response(
                        client,
                        candidate,
                        resume_size=partial_size,
                    )
                    if (
                        response.status_code == 416
                        and partial_size
                    ):
                        response.close()
                        response = None
                        os.ftruncate(descriptor, 0)
                        partial_size = 0
                        response = _open_response(
                            client,
                            candidate,
                            resume_size=0,
                        )
                    headers = _lower_headers(
                        response.headers
                    )
                    content_type = _validate_content_type(
                        headers
                    )
                    if response.status_code == 206:
                        _validate_content_range(
                            headers.get("content-range"),
                            expected_start=partial_size,
                        )
                        os.lseek(
                            descriptor,
                            partial_size,
                            os.SEEK_SET,
                        )
                        state = (
                            RecordState.RESUMED
                            if partial_size
                            else RecordState.DOWNLOADED
                        )
                    elif response.status_code == 200:
                        os.ftruncate(descriptor, 0)
                        os.lseek(
                            descriptor,
                            0,
                            os.SEEK_SET,
                        )
                        partial_size = 0
                        state = RecordState.DOWNLOADED
                    else:
                        raise PdfRangeError(
                            "Server akzeptiert den PDF-Abruf nicht"
                        )

                    declared = _declared_length(headers)
                    if (
                        declared is not None
                        and partial_size + declared > max_bytes
                    ):
                        raise ResponseTooLargeError(
                            f"PDF würde "
                            f"{partial_size + declared} Bytes "
                            f"erreichen; Grenze ist {max_bytes}"
                        )
                    total = partial_size
                    for chunk in response.iter_content(
                        1024 * 1024
                    ):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise ResponseTooLargeError(
                                "PDF überschreitet die Grenze "
                                f"von {max_bytes} Bytes"
                            )
                        view = memoryview(chunk)
                        while view:
                            written = os.write(
                                descriptor,
                                view,
                            )
                            view = view[written:]
                    os.fsync(descriptor)
                    size, md5, sha256 = _hash_and_validate_fd(
                        descriptor,
                        max_bytes=max_bytes,
                        expected_md5=candidate.expected_md5,
                    )
                    os.close(descriptor)
                    descriptor = -1
                    os.replace(
                        part_name,
                        relative.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    fsync_directory_fd(parent_fd)
                    return DownloadResult(
                        state=state,
                        bytes=size,
                        md5=md5,
                        sha256=sha256,
                        relative_path=relative.as_posix(),
                        etag=headers.get("etag"),
                        last_modified=headers.get(
                            "last-modified"
                        ),
                        content_type=content_type,
                    )
                except BaseException:
                    try:
                        os.unlink(
                            part_name,
                            dir_fd=parent_fd,
                        )
                    except FileNotFoundError:
                        pass
                    raise
                finally:
                    if response is not None:
                        response.close()
                    if descriptor >= 0:
                        os.close(descriptor)
            finally:
                _release_target_lock(lock_fd)
        finally:
            os.close(parent_fd)
