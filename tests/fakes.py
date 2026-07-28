"""Offline fake HTTP transport used by P03 parser, API, and download tests."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


@dataclass(slots=True)
class FakeResponse:
    """Small streaming response compatible with ``ResponseLike``."""

    status_code: int
    url: str
    body: bytes = b""
    headers: Mapping[str, str] | None = None
    closed: bool = False

    def __post_init__(self) -> None:
        normalized = {str(k).lower(): str(v) for k, v in (self.headers or {}).items()}
        if "content-length" not in normalized:
            normalized["content-length"] = str(len(self.body))
        self.headers = normalized

    def iter_content(self, chunk_size: int) -> Iterable[bytes]:
        """Yield deterministic chunks."""

        for offset in range(0, len(self.body), max(1, chunk_size)):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        """Mark the response closed."""

        self.closed = True


class FakeTransport:
    """URL-keyed finite response queue with request recording."""

    def __init__(self, responses: Mapping[str, list[FakeResponse] | FakeResponse]) -> None:
        """Normalize every URL to a queue."""

        self.responses: dict[str, list[FakeResponse]] = {}
        for url, value in responses.items():
            self.responses[url] = list(value) if isinstance(value, list) else [value]
        self.calls: list[tuple[str, str, dict[str, str], float, bool]] = []
        self.counts: defaultdict[str, int] = defaultdict(int)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
    ) -> FakeResponse:
        """Return the next queued response for the exact URL."""

        self.calls.append((method, url, dict(headers), timeout, stream))
        self.counts[url] += 1
        queue = self.responses.get(url)
        if not queue:
            raise AssertionError(f"Unerwartete Fake-HTTP-Anfrage: {method} {url}")
        response = queue.pop(0)
        response.url = url if not response.url else response.url
        return response
