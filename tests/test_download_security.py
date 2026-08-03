"""Secure PDF download and resume regressions."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from scripts.rki_grabber.download import (
    PdfContentTypeError,
    PdfIntegrityError,
    download_pdf,
)
from scripts.rki_grabber.http import PoliteClient, ResponseTooLargeError
from scripts.rki_grabber.models import PdfCandidate, RecordState, SourceConfig
from scripts.rki_pipeline.io_utils import UnsafePathError
from scripts.rki_pipeline.pdf_validation import validate_pdf_fd
from tests.fakes import FakeResponse, FakeTransport

PDF_URL = "https://edoc.rki.de/bitstream/handle/176904/12345.2/minimal.pdf"
PDF = (Path(__file__).parent / "fixtures" / "pdf" / "minimal.pdf").read_bytes()
MD5 = hashlib.md5(PDF, usedforsecurity=False).hexdigest()


def build_client(transport: FakeTransport) -> PoliteClient:
    """Create a same-origin client without robots or delay for download tests."""

    return PoliteClient(
        SourceConfig(delay_seconds=0, timeout_seconds=1, respect_robots=False),
        transport=transport,
        respect_robots=False,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )


def candidate() -> PdfCandidate:
    """Return a PDF candidate with the fixture checksum."""

    return PdfCandidate(PDF_URL, "minimal.pdf", MD5)


def test_download_validates_hash_and_existing_file_without_second_request(tmp_path: Path) -> None:
    transport = FakeTransport(
        {PDF_URL: FakeResponse(200, PDF_URL, PDF, {"content-type": "application/pdf"})}
    )
    client = build_client(transport)
    target = tmp_path / "issues" / "1996" / "minimal.pdf"
    first = download_pdf(
        client,
        candidate(),
        target,
        allowed_root=tmp_path,
        force=False,
        max_bytes=1024 * 1024,
    )
    assert first.state is RecordState.DOWNLOADED
    assert first.bytes == len(PDF)
    assert target.read_bytes() == PDF
    second = download_pdf(
        client,
        candidate(),
        target,
        allowed_root=tmp_path,
        force=False,
        max_bytes=1024 * 1024,
    )
    assert second.state is RecordState.EXISTING
    assert transport.counts[PDF_URL] == 1


def test_download_result_matches_shared_descriptor_validation(tmp_path: Path) -> None:
    """Downloader and P06 must derive identical byte evidence."""

    source = tmp_path / "source.pdf"
    source.write_bytes(PDF)
    descriptor = os.open(source, os.O_RDONLY)
    try:
        shared = validate_pdf_fd(descriptor, max_bytes=1024 * 1024, expected_md5=MD5)
    finally:
        os.close(descriptor)
    target = tmp_path / "downloaded.pdf"
    transport = FakeTransport(
        {PDF_URL: FakeResponse(200, PDF_URL, PDF, {"content-type": "application/pdf"})}
    )

    downloaded = download_pdf(
        build_client(transport),
        candidate(),
        target,
        allowed_root=tmp_path,
        force=False,
        max_bytes=1024 * 1024,
    )

    assert (downloaded.bytes, downloaded.md5, downloaded.sha256) == (
        shared.size,
        shared.md5,
        shared.sha256,
    )


def test_resume_requires_matching_content_range(tmp_path: Path) -> None:
    target = tmp_path / "issues" / "1996" / "minimal.pdf"
    target.parent.mkdir(parents=True)
    part = target.parent / f".{target.name}.part"
    split = 100
    part.write_bytes(PDF[:split])
    transport = FakeTransport(
        {
            PDF_URL: FakeResponse(
                206,
                PDF_URL,
                PDF[split:],
                {
                    "content-type": "application/pdf",
                    "content-range": f"bytes {split}-{len(PDF)-1}/{len(PDF)}",
                },
            )
        }
    )
    result = download_pdf(
        build_client(transport),
        candidate(),
        target,
        allowed_root=tmp_path,
        force=False,
        max_bytes=1024 * 1024,
    )
    assert result.state is RecordState.RESUMED
    assert target.read_bytes() == PDF
    assert transport.calls[0][2]["Range"] == f"bytes={split}-"


def test_wrong_content_type_truncation_and_oversize_leave_no_partial(tmp_path: Path) -> None:
    target = tmp_path / "minimal.pdf"
    html_transport = FakeTransport(
        {PDF_URL: FakeResponse(200, PDF_URL, PDF, {"content-type": "text/html"})}
    )
    with pytest.raises(PdfContentTypeError):
        download_pdf(
            build_client(html_transport),
            candidate(),
            target,
            allowed_root=tmp_path,
            force=False,
            max_bytes=1024 * 1024,
        )
    assert not (tmp_path / ".minimal.pdf.part").exists()

    truncated_transport = FakeTransport(
        {
            PDF_URL: FakeResponse(
                200,
                PDF_URL,
                PDF[:-6],
                {"content-type": "application/pdf"},
            )
        }
    )
    with pytest.raises(PdfIntegrityError):
        download_pdf(
            build_client(truncated_transport),
            PdfCandidate(PDF_URL, "minimal.pdf"),
            target,
            allowed_root=tmp_path,
            force=False,
            max_bytes=1024 * 1024,
        )
    assert not (tmp_path / ".minimal.pdf.part").exists()

    oversize_transport = FakeTransport(
        {
            PDF_URL: FakeResponse(
                200,
                PDF_URL,
                PDF,
                {"content-type": "application/pdf", "content-length": "2000000"},
            )
        }
    )
    with pytest.raises(ResponseTooLargeError):
        download_pdf(
            build_client(oversize_transport),
            candidate(),
            target,
            allowed_root=tmp_path,
            force=False,
            max_bytes=1000,
        )
    assert not (tmp_path / ".minimal.pdf.part").exists()


def test_zero_byte_existing_pdf_is_integrity_failure(tmp_path: Path) -> None:
    target = tmp_path / "minimal.pdf"
    target.write_bytes(b"")

    with pytest.raises(PdfIntegrityError):
        download_pdf(
            build_client(FakeTransport({})),
            candidate(),
            target,
            allowed_root=tmp_path,
            force=False,
            max_bytes=1024 * 1024,
        )


def test_symlinked_output_component_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "issues").symlink_to(outside, target_is_directory=True)
    transport = FakeTransport(
        {PDF_URL: FakeResponse(200, PDF_URL, PDF, {"content-type": "application/pdf"})}
    )
    with pytest.raises((UnsafePathError, OSError)):
        download_pdf(
            build_client(transport),
            candidate(),
            tmp_path / "issues" / "1996" / "minimal.pdf",
            allowed_root=tmp_path,
            force=False,
            max_bytes=1024 * 1024,
        )
    assert not (outside / "1996" / "minimal.pdf").exists()
