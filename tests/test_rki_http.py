"""Offline security and robots tests for the RKI HTTP boundary."""
from __future__ import annotations

import pytest

from scripts.rki_grabber.http import (
    PoliteClient,
    ResponseTooLargeError,
    RobotsUnavailableError,
    UnsafeSourceUrlError,
)
from scripts.rki_grabber.models import SourceConfig
from tests.fakes import FakeResponse, FakeTransport

BASE = "https://edoc.rki.de"
ROBOTS = f"{BASE}/robots.txt"
ROOT = f"{BASE}/handle/176904/10"


def client(transport: FakeTransport) -> PoliteClient:
    """Create a no-delay test client."""

    return PoliteClient(
        SourceConfig(delay_seconds=0, timeout_seconds=1),
        transport=transport,
        clock=lambda: 0.0,
        sleep=lambda _seconds: None,
    )


def test_robots_ambiguous_status_fails_closed() -> None:
    transport = FakeTransport(
        {ROBOTS: FakeResponse(500, ROBOTS, b"failure", {"content-type": "text/plain"})}
    )
    with pytest.raises(RobotsUnavailableError):
        client(transport).get_bytes(ROOT, max_bytes=1024, accept="text/html")
    assert transport.counts[ROOT] == 0


def test_robots_404_allows_but_foreign_redirect_is_rejected_before_follow() -> None:
    transport = FakeTransport(
        {
            ROBOTS: FakeResponse(404, ROBOTS),
            ROOT: FakeResponse(302, ROOT, headers={"location": "https://evil.example/x"}),
        }
    )
    with pytest.raises(UnsafeSourceUrlError):
        client(transport).get_bytes(ROOT, max_bytes=1024, accept="text/html")
    assert all(call[1] != "https://evil.example/x" for call in transport.calls)


def test_same_origin_redirect_and_bounded_body_succeed() -> None:
    final = f"{BASE}/handle/176904/10/"
    transport = FakeTransport(
        {
            ROBOTS: FakeResponse(404, ROBOTS),
            ROOT: FakeResponse(302, ROOT, headers={"location": final}),
            final: FakeResponse(
                200,
                final,
                b"<html>ok</html>",
                {"content-type": "text/html; charset=utf-8"},
            ),
        }
    )
    payload = client(transport).get_bytes(ROOT, max_bytes=1024, accept="text/html")
    assert payload.text() == "<html>ok</html>"
    assert payload.url == final


def test_declared_response_over_limit_is_rejected() -> None:
    transport = FakeTransport(
        {
            ROBOTS: FakeResponse(404, ROBOTS),
            ROOT: FakeResponse(
                200,
                ROOT,
                b"tiny",
                {"content-type": "text/html", "content-length": "10000"},
            ),
        }
    )
    with pytest.raises(ResponseTooLargeError):
        client(transport).get_bytes(ROOT, max_bytes=100, accept="text/html")
