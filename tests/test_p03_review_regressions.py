"""Regression tests for the actionable PR #7 review findings."""
from __future__ import annotations

import fcntl
import math
import os
from pathlib import Path

import pytest

from scripts.rki_grabber.config import load_source_config
from scripts.rki_grabber.download import PdfDownloadError, download_pdf
from scripts.rki_grabber.models import (
    GrabberRequest,
    PdfCandidate,
    SourceConfig,
)
from scripts.rki_grabber.rki_epidbull_grabber import (
    _request_from_args,
    build_parser,
)
from scripts.rki_grabber.service import RkiGrabberService
from tests.fakes import FakeResponse, FakeTransport

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://edoc.rki.de"
PDF_URL = f"{BASE}/bitstream/handle/176904/12345.2/minimal.pdf"
PDF = (ROOT / "tests" / "fixtures" / "pdf" / "minimal.pdf").read_bytes()


def write_config(
    tmp_path: Path,
    *,
    source: str = "",
    network: str = "",
    limits: str = "",
) -> Path:
    """Write one complete schema-v1 source configuration fixture."""

    path = tmp_path / "rki-source.toml"
    path.write_text(
        "schema_version = 1\n"
        "[source]\n"
        'allowed_hosts = ["edoc.rki.de"]\n'
        f"{source}"
        "[network]\n"
        f"{network}"
        "[limits]\n"
        f"{limits}",
        encoding="utf-8",
    )
    return path


def test_source_config_rejects_every_network_boundary_override() -> None:
    """The RKI endpoint and host tuple are application constants, not TOML policy."""

    with pytest.raises(ValueError, match="kanonische RKI-Basis-URL"):
        SourceConfig(base_url="https://example.org")
    with pytest.raises(ValueError, match="kanonischen RKI-Host"):
        SourceConfig(allowed_hosts=("example.org",))


def test_grabber_request_rejects_wrong_runtime_types_and_nonfinite_numbers() -> None:
    """Public API construction must fail before values reach the result schema."""

    invalid_requests = (
        {"scope": "issues"},
        {"from_year": 1994.5},
        {"to_year": True},
        {"max_items": True},
        {"dry_run": 1},
        {"force": 0},
        {"respect_robots": "false"},
        {"delay_seconds": math.nan},
        {"delay_seconds": math.inf},
        {"timeout_seconds": -math.inf},
        {"max_html_bytes": 1.5},
        {"max_pdf_bytes": False},
    )
    for values in invalid_requests:
        with pytest.raises(ValueError):
            GrabberRequest(**values)


def test_config_normalizes_oversized_numeric_errors(tmp_path: Path) -> None:
    """Huge TOML integers must follow the public ValueError contract."""

    enormous = "9" * 400
    path = write_config(
        tmp_path,
        network=f"delay_seconds = {enormous}\n",
    )
    with pytest.raises(
        ValueError,
        match="delay_seconds muss eine endliche Zahl sein",
    ):
        load_source_config(path)


def test_config_rejects_non_string_handles_and_user_agent(tmp_path: Path) -> None:
    """Lists and numbers may not be silently converted into URLs or headers."""

    cases = (
        {"source": 'issues_root_handle = ["176904/10"]\n'},
        {"source": "articles_handle = 176904\n"},
        {"network": "user_agent = 123\n"},
    )
    for index, sections in enumerate(cases):
        case_root = tmp_path / str(index)
        case_root.mkdir()
        path = write_config(case_root, **sections)
        with pytest.raises(ValueError, match="muss eine Zeichenkette sein"):
            load_source_config(path)


def test_cli_inherits_robots_policy_unless_no_robots_is_explicit() -> None:
    """An absent CLI override must preserve the selected TOML policy."""

    parser = build_parser()
    inherited = _request_from_args(parser.parse_args([]))
    disabled = _request_from_args(parser.parse_args(["--no-robots"]))
    assert inherited.respect_robots is None
    assert disabled.respect_robots is False


def test_filtered_listing_link_does_not_create_false_pagination_failure() -> None:
    """DSpace raw totals cannot be compared with the post-filter item count."""

    listing = """<!doctype html><html><body>
      <p>Now showing items 1-2 of 2</p>
      <a href="/handle/176904/12345.2">Bulletin 12/1996</a>
      <a href="/handle/176904/1996">1996 collection</a>
    </body></html>"""

    class ListingService(RkiGrabberService):
        def _html(self, url: str):  # type: ignore[override]
            return listing, {}, url

    service = ListingService(
        SourceConfig(delay_seconds=0),
        object(),  # type: ignore[arg-type]
    )
    items = list(
        service.iter_submission_items(
            collection_handle="176904/1996",
            excluded_handles={"176904/1996"},
        )
    )
    assert items == [
        ("176904/12345.2", f"{BASE}/handle/176904/12345.2")
    ]


def test_download_fails_fast_when_target_lock_is_held(tmp_path: Path) -> None:
    """A second writer may not enter the resume/verify/replace sequence."""

    target = tmp_path / "minimal.pdf"
    lock_path = tmp_path / ".minimal.pdf.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        transport = FakeTransport(
            {
                PDF_URL: FakeResponse(
                    200,
                    PDF_URL,
                    PDF,
                    {"content-type": "application/pdf"},
                )
            }
        )
        from scripts.rki_grabber.http import PoliteClient

        client = PoliteClient(
            SourceConfig(
                delay_seconds=0,
                timeout_seconds=1,
                respect_robots=False,
            ),
            transport=transport,
            respect_robots=False,
            clock=lambda: 0.0,
            sleep=lambda _seconds: None,
        )
        with pytest.raises(PdfDownloadError, match="bereits gesperrt"):
            download_pdf(
                client,
                PdfCandidate(PDF_URL, "minimal.pdf"),
                target,
                allowed_root=tmp_path,
                force=False,
                max_bytes=1024 * 1024,
            )
        assert transport.counts[PDF_URL] == 0
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_api_documentation_assigns_schema_validation_to_callers() -> None:
    """Documentation must not claim that to_dict() performs validation."""

    api = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    assert (
        "to_dict()` liefert ausschließlich JSON-kompatible Werte und validiert"
        not in api
    )
    assert "Schema-Validierung" in api
