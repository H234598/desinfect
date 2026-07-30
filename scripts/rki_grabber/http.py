#!/usr/bin/env python3
"""Bounded, same-origin, rate-limited HTTP client with fail-closed robots handling."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
import logging
import time
from typing import Protocol, runtime_checkable
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from scripts.rki_grabber.models import SourceConfig

try:  # Parser-only imports must still work without HTTP dependencies.
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except ImportError:  # pragma: no cover - exercised only in minimal installations.
    requests = None  # type: ignore[assignment]
    HTTPAdapter = None  # type: ignore[assignment]
    Retry = None  # type: ignore[assignment]


class GrabberHttpError(RuntimeError):
    """Base class for structured source transport failures."""

    code = "http.error"
    retryable = False


class HttpConfigurationError(GrabberHttpError):
    """Required HTTP dependency or setting is unavailable."""

    code = "http.configuration"


class UnsafeSourceUrlError(GrabberHttpError):
    """A source or redirect URL crosses the fixed RKI trust boundary."""

    code = "http.unsafe_url"


class TransportRequestError(GrabberHttpError):
    """The low-level transport failed before producing an HTTP response."""

    code = "http.transport"
    retryable = True


class TooManyRedirectsError(GrabberHttpError):
    """The response exceeded the configured redirect limit."""

    code = "http.too_many_redirects"
    retryable = True


class UnexpectedStatusError(GrabberHttpError):
    """The final HTTP status is not accepted by the caller."""

    code = "http.unexpected_status"
    retryable = True

    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"Unerwarteter HTTP-Status {status_code} für {url}")
        self.status_code = status_code
        self.url = url


class ResponseTooLargeError(GrabberHttpError):
    """The response exceeds a configured byte limit."""

    code = "http.response_too_large"


class RobotsUnavailableError(GrabberHttpError):
    """robots.txt could not be evaluated safely."""

    code = "robots.unavailable"
    retryable = True


class RobotsDeniedError(GrabberHttpError):
    """robots.txt explicitly disallows the requested URL."""

    code = "robots.denied"


@runtime_checkable
class ResponseLike(Protocol):
    """Small internal streaming response surface used by requests and tests."""

    status_code: int
    url: str
    headers: Mapping[str, str]

    def iter_content(self, chunk_size: int) -> Iterable[bytes]:
        """Yield response bytes."""

    def close(self) -> None:
        """Release response resources."""


class HttpTransport(Protocol):
    """Injectable low-level transport that never follows redirects automatically."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
    ) -> ResponseLike:
        """Perform one HTTP request with redirects disabled."""


class RequestsTransport:
    """Production transport backed by a retry-configured requests session."""

    def __init__(self, *, user_agent: str, contact: str | None = None) -> None:
        """Create a session without making a network request."""

        if requests is None or HTTPAdapter is None or Retry is None:
            raise HttpConfigurationError(
                "Fehlende HTTP-Abhängigkeiten: requests und urllib3 werden benötigt"
            )
        self.session = requests.Session()
        full_agent = user_agent + (f" (contact: {contact})" if contact else "")
        self.session.headers.update(
            {
                "User-Agent": full_agent,
                "Accept-Language": "de,en;q=0.8",
            }
        )
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=5,
            backoff_factor=1.0,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            respect_retry_after_header=True,
            raise_on_status=False,
            redirect=0,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
    ) -> ResponseLike:
        """Perform one request and preserve redirect control for ``PoliteClient``."""

        return self.session.request(
            method,
            url,
            headers=dict(headers),
            timeout=timeout,
            stream=stream,
            allow_redirects=False,
        )


@dataclass(frozen=True, slots=True)
class HttpPayload:
    """Bounded in-memory response used for HTML and robots data."""

    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def text(self) -> str:
        """Decode HTML-like payloads using a bounded UTF-8 replacement strategy."""

        content_type = self.headers.get("content-type", "")
        encoding = "utf-8"
        for part in content_type.split(";")[1:]:
            key, separator, value = part.strip().partition("=")
            if separator and key.lower() == "charset" and value.strip():
                encoding = value.strip().strip('"')
                break
        try:
            return self.body.decode(encoding, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


class PoliteClient:
    """Same-origin RKI client with delay, manual redirects, limits, and robots gate."""

    _REDIRECTS = frozenset({301, 302, 303, 307, 308})

    def __init__(
        self,
        config: SourceConfig,
        *,
        transport: HttpTransport | None = None,
        contact: str | None = None,
        user_agent: str | None = None,
        delay_seconds: float | None = None,
        timeout_seconds: float | None = None,
        respect_robots: bool | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Create a client without source access; robots are loaded lazily."""

        self.config = config
        self.user_agent = user_agent or config.user_agent
        self.delay_seconds = (
            config.delay_seconds if delay_seconds is None else delay_seconds
        )
        self.timeout_seconds = (
            config.timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        self.respect_robots = (
            config.respect_robots if respect_robots is None else respect_robots
        )
        if self.delay_seconds < 0 or self.timeout_seconds <= 0:
            raise HttpConfigurationError("Ungültige Delay-/Timeout-Konfiguration")
        self.transport = transport or RequestsTransport(
            user_agent=self.user_agent,
            contact=contact,
        )
        self._clock = clock
        self._sleep = sleep
        self._last_request_at: float | None = None
        self._robots: RobotFileParser | None = None
        self._robots_loaded = False

    def validate_url(self, url: str) -> str:
        """Validate and normalize one source URL against the fixed trust boundary."""

        parsed = urlsplit(url)
        if (
            parsed.scheme.lower() != "https"
            or parsed.hostname is None
            or parsed.hostname.lower() not in {host.lower() for host in self.config.allowed_hosts}
            or parsed.port not in {None, 443}
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise UnsafeSourceUrlError(f"URL außerhalb der erlaubten RKI-Grenze: {url}")
        return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))

    def _sleep_if_needed(self) -> None:
        """Enforce the minimum delay between every low-level request."""

        if self._last_request_at is None:
            return
        remaining = self.delay_seconds - (self._clock() - self._last_request_at)
        if remaining > 0:
            self._sleep(remaining)

    def _single_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        stream: bool,
    ) -> ResponseLike:
        """Perform exactly one validated and rate-limited request."""

        clean_url = self.validate_url(url)
        self._sleep_if_needed()
        try:
            return self.transport.request(
                method,
                clean_url,
                headers=headers,
                timeout=self.timeout_seconds,
                stream=stream,
            )
        except GrabberHttpError:
            raise
        except Exception as exc:  # requests and test transports share no exception base.
            raise TransportRequestError(
                f"HTTP-Anfrage fehlgeschlagen: {type(exc).__name__}"
            ) from exc
        finally:
            self._last_request_at = self._clock()

    def _follow_redirects(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        stream: bool,
    ) -> ResponseLike:
        """Follow only validated HTTPS redirects and close intermediate responses."""

        current = self.validate_url(url)
        for redirect_count in range(self.config.max_redirects + 1):
            response = self._single_request(
                method,
                current,
                headers=headers,
                stream=stream,
            )
            if response.status_code not in self._REDIRECTS:
                return response
            location = response.headers.get("location")
            response.close()
            if not location:
                raise UnexpectedStatusError(response.status_code, current)
            if redirect_count >= self.config.max_redirects:
                raise TooManyRedirectsError(current)
            current = self.validate_url(urljoin(current, location))
        raise TooManyRedirectsError(current)  # pragma: no cover - loop is exhaustive.

    @staticmethod
    def _content_length(headers: Mapping[str, str]) -> int | None:
        """Parse a non-negative Content-Length when present."""

        raw = headers.get("content-length")
        if raw is None:
            return None
        try:
            value = int(raw)
        except ValueError as exc:
            raise GrabberHttpError("Ungültiger Content-Length-Header") from exc
        if value < 0:
            raise GrabberHttpError("Negativer Content-Length-Header")
        return value

    @staticmethod
    def _lower_headers(headers: Mapping[str, str]) -> dict[str, str]:
        """Normalize response header names for deterministic callers."""

        return {str(key).lower(): str(value) for key, value in headers.items()}

    def _read_limited(self, response: ResponseLike, *, max_bytes: int) -> bytes:
        """Read and bound a response body before returning it to a parser."""

        headers = self._lower_headers(response.headers)
        declared = self._content_length(headers)
        if declared is not None and declared > max_bytes:
            raise ResponseTooLargeError(
                f"Antwort deklariert {declared} Bytes; Grenze ist {max_bytes}"
            )
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLargeError(
                    f"Antwort überschreitet die Grenze von {max_bytes} Bytes"
                )
            chunks.append(bytes(chunk))
        return b"".join(chunks)

    def _load_robots(self) -> None:
        """Load robots.txt once and fail closed for any ambiguous response."""

        if self._robots_loaded or not self.respect_robots:
            self._robots_loaded = True
            return
        robots_url = f"{self.config.base_url}/robots.txt"
        response: ResponseLike | None = None
        try:
            response = self._follow_redirects(
                "GET",
                robots_url,
                headers={"Accept": "text/plain,*/*;q=0.1"},
                stream=True,
            )
            if response.status_code == 404:
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse([])
                self._robots = parser
            elif response.status_code == 200:
                body = self._read_limited(
                    response,
                    max_bytes=self.config.robots_max_bytes,
                )
                parser = RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
                self._robots = parser
            else:
                raise RobotsUnavailableError(
                    f"robots.txt lieferte HTTP {response.status_code}"
                )
            self._robots_loaded = True
        except RobotsUnavailableError:
            raise
        except Exception as exc:
            raise RobotsUnavailableError(
                f"robots.txt konnte nicht sicher ausgewertet werden: {type(exc).__name__}"
            ) from exc
        finally:
            if response is not None:
                response.close()

    def _assert_robots_allowed(self, url: str) -> None:
        """Require a loaded policy and explicit permission for the current user agent."""

        if not self.respect_robots:
            return
        self._load_robots()
        if self._robots is None:
            raise RobotsUnavailableError("robots.txt-Zustand ist unbekannt")
        if not self._robots.can_fetch(self.user_agent, url):
            raise RobotsDeniedError(f"robots.txt untersagt den Abruf: {url}")

    def get_bytes(
        self,
        url: str,
        *,
        max_bytes: int,
        accept: str,
        allowed_statuses: frozenset[int] = frozenset({200}),
        check_robots: bool = True,
        headers: Mapping[str, str] | None = None,
    ) -> HttpPayload:
        """Return one bounded response body after all trust-boundary checks."""

        clean_url = self.validate_url(url)
        if check_robots:
            self._assert_robots_allowed(clean_url)
        request_headers = {"Accept": accept, **dict(headers or {})}
        response = self._follow_redirects(
            "GET",
            clean_url,
            headers=request_headers,
            stream=True,
        )
        try:
            if response.status_code not in allowed_statuses:
                raise UnexpectedStatusError(response.status_code, response.url)
            normalized_headers = self._lower_headers(response.headers)
            return HttpPayload(
                url=self.validate_url(response.url),
                status_code=response.status_code,
                headers=normalized_headers,
                body=self._read_limited(response, max_bytes=max_bytes),
            )
        finally:
            response.close()

    def open_stream(
        self,
        url: str,
        *,
        accept: str,
        allowed_statuses: frozenset[int],
        headers: Mapping[str, str] | None = None,
        check_robots: bool = True,
    ) -> ResponseLike:
        """Open a validated streaming response; the caller must close it."""

        clean_url = self.validate_url(url)
        if check_robots:
            self._assert_robots_allowed(clean_url)
        response = self._follow_redirects(
            "GET",
            clean_url,
            headers={"Accept": accept, **dict(headers or {})},
            stream=True,
        )
        if response.status_code not in allowed_statuses:
            try:
                raise UnexpectedStatusError(response.status_code, response.url)
            finally:
                response.close()
        return response

    def log_policy(self) -> None:
        """Emit a concise source-access policy summary without contact information."""

        logging.info(
            "RKI HTTP policy: hosts=%s robots=%s delay=%.2fs timeout=%.1fs",
            ",".join(self.config.allowed_hosts),
            self.respect_robots,
            self.delay_seconds,
            self.timeout_seconds,
        )
