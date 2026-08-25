#!/usr/bin/env python3
"""Verify exact Worker health with bounded Linux GitHub Actions process isolation."""
from __future__ import annotations

import argparse
import http.client
import json
import multiprocessing
import socket
import ssl
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit

MAX_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 3
REQUEST_TIMEOUT_SECONDS = 5
ATTEMPT_DEADLINE_SECONDS = 10


class Response(Protocol):
    status: int

    def read(self) -> bytes: ...

    def getheader(self, name: str) -> str | None: ...


class Connection(Protocol):
    def request(self, method: str, url: str, **kwargs: object) -> None: ...

    def getresponse(self) -> Response: ...

    def close(self) -> None: ...


class HealthCheckError(RuntimeError):
    """A health check exhausted its bounded attempts."""


@dataclass(frozen=True)
class Failure:
    category: str
    status: int | None = None
    location_hostname: str | None = None

    def render(self) -> str:
        details = [f"category={self.category}"]
        if self.status is not None:
            details.append(f"http_status={self.status}")
        if self.location_hostname:
            details.append(f"location_hostname={self.location_hostname}")
        return "health check failed: " + " ".join(details)


ConnectionFactory = Callable[..., Connection]


def _connection(host: str, port: int | None = None, timeout: float | None = None) -> Connection:
    return http.client.HTTPSConnection(host, port=port, timeout=timeout)


def _target(url: str) -> tuple[str, int | None]:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise HealthCheckError(Failure("url").render()) from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.path != "/healthz"
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise ValueError("health URL must use HTTPS and exact /healthz endpoint")
    return parsed.hostname, port


def _location_hostname(response: Response) -> str | None:
    location = response.getheader("Location")
    if not location:
        return None
    try:
        return urlsplit(location).hostname
    except ValueError:
        return None


def _check_once(
    host: str,
    port: int | None,
    expected_version: str,
    connection_factory: ConnectionFactory,
) -> Failure | None:
    connection: Connection | None = None
    try:
        connection = connection_factory(host, port=port, timeout=REQUEST_TIMEOUT_SECONDS)
        connection.request("GET", "/healthz", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return Failure("http", response.status, _location_hostname(response))
        try:
            payload = json.loads(response.read())
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return Failure("json", response.status)
        if not isinstance(payload, dict) or payload.get("service") != "desinfect-watchdog" or payload.get("status") != "ok":
            return Failure("json", response.status)
        if payload.get("version") != expected_version:
            return Failure("version", response.status)
        return None
    except socket.gaierror:
        return Failure("dns")
    except ssl.SSLError:
        return Failure("tls")
    except (OSError, http.client.HTTPException):
        return Failure("http")
    finally:
        if connection is not None:
            connection.close()


def _child_check(
    result: object,
    host: str,
    port: int | None,
    expected_version: str,
    connection_factory: ConnectionFactory,
) -> None:
    try:
        failure = _check_once(host, port, expected_version, connection_factory)
    except BaseException:
        failure = Failure("http")
    try:
        result.send(failure)  # type: ignore[attr-defined]
    finally:
        result.close()  # type: ignore[attr-defined]


def _check_with_deadline(
    host: str,
    port: int | None,
    expected_version: str,
    connection_factory: ConnectionFactory,
) -> Failure | None:
    """Run one attempt in a Linux child so DNS and body reads cannot outlive its deadline."""

    if sys.platform != "linux":
        raise RuntimeError("Cloudflare health readiness requires Linux fork process isolation")
    context = multiprocessing.get_context("fork")
    receive, send = context.Pipe(duplex=False)
    process = context.Process(
        target=_child_check,
        args=(send, host, port, expected_version, connection_factory),
        daemon=True,
    )
    started = time.monotonic()
    process.start()
    send.close()
    try:
        process.join(max(0, ATTEMPT_DEADLINE_SECONDS - (time.monotonic() - started)))
        if process.is_alive():
            process.terminate()
            process.join(0)
            if process.is_alive():
                process.kill()
                process.join(0)
            return Failure("deadline")
        if receive.poll(0):
            return receive.recv()
        return Failure("http")
    finally:
        receive.close()


def verify_health(
    url: str,
    expected_version: str,
    *,
    connection_factory: ConnectionFactory = _connection,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Return only after exact health success; otherwise raise safe final failure."""

    host, port = _target(url)
    failure: Failure | None = None
    for attempt in range(MAX_ATTEMPTS):
        failure = _check_with_deadline(host, port, expected_version, connection_factory)
        if failure is None:
            return
        if attempt + 1 < MAX_ATTEMPTS:
            sleep(RETRY_DELAY_SECONDS)
    raise HealthCheckError(failure.render())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args(argv)
    try:
        verify_health(args.url, args.expected_version)
    except (HealthCheckError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
