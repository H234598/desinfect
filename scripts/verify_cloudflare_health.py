#!/usr/bin/env python3
"""Verify exact Worker health with bounded Linux GitHub Actions process isolation."""
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import multiprocessing
import os
import re
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
CLEANUP_GRACE_SECONDS = 0.25
DEVICE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")


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


class _ResolvedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection retaining hostname SNI while connecting to one verified IP."""

    def __init__(
        self,
        host: str,
        *,
        resolved_address: str,
        bind_device: str | None,
        port: int | None,
        timeout: float | None,
    ) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._resolved_address = resolved_address
        self._bind_device = bind_device

    def connect(self) -> None:
        resolved_ip = ipaddress.ip_address(self._resolved_address)
        family = socket.AF_INET if resolved_ip.version == 4 else socket.AF_INET6
        address = (self._resolved_address, self.port)
        if family == socket.AF_INET6:
            address = (self._resolved_address, self.port, 0, 0)
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.settimeout(self.timeout)
            if self._bind_device is not None:
                sock.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_BINDTODEVICE,
                    self._bind_device.encode("ascii") + b"\0",
                )
            if self.source_address:
                sock.bind(self.source_address)
            sock.connect(address)
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        if self._tunnel_host:
            self._tunnel()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _connection(
    host: str,
    port: int | None = None,
    timeout: float | None = None,
    *,
    resolved_address: str | None = None,
    bind_device: str | None = None,
) -> Connection:
    if bind_device is not None and not DEVICE_NAME.fullmatch(bind_device):
        raise ValueError("bind device must be a Linux interface name")
    if resolved_address is not None:
        ipaddress.ip_address(resolved_address)
        return _ResolvedHTTPSConnection(
            host,
            resolved_address=resolved_address,
            bind_device=bind_device,
            port=port,
            timeout=timeout,
        )
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


def resolve_target_addresses(url: str) -> tuple[tuple[int, str], ...]:
    """Return each IPv4/IPv6 address currently resolved for exact HTTPS health."""

    host, port = _target(url)
    addresses: set[tuple[int, str]] = set()
    for family, _, _, _, sockaddr in socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM):
        if family not in (socket.AF_INET, socket.AF_INET6):
            continue
        address = str(ipaddress.ip_address(sockaddr[0]))
        addresses.add((4 if family == socket.AF_INET else 6, address))
    if not addresses:
        raise HealthCheckError(Failure("dns").render())
    return tuple(sorted(addresses))


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


def _remaining(deadline: float) -> float:
    return max(0, deadline - time.monotonic())


def _hard_abort_cleanup() -> None:
    os.write(2, b"health check failed: category=deadline_cleanup\n")
    os._exit(1)


def _reap_or_abort(process: multiprocessing.Process, deadline: float, grace: float) -> None:
    if process.is_alive():
        process.terminate()
        process.join(min(grace, _remaining(deadline)))
    if process.is_alive():
        process.kill()
        process.join(min(grace, _remaining(deadline)))
    if process.is_alive():
        _hard_abort_cleanup()
    process.join(0)
    process.close()


def _check_with_deadline(
    host: str,
    port: int | None,
    expected_version: str,
    connection_factory: ConnectionFactory,
) -> Failure | None:
    """Run one attempt in Linux fork isolation and reap it within its total deadline."""

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
    deadline = started + ATTEMPT_DEADLINE_SECONDS
    grace = min(CLEANUP_GRACE_SECONDS, ATTEMPT_DEADLINE_SECONDS / 4)
    process_started = False
    reaped = False
    try:
        try:
            process.start()
            process_started = True
        except OSError:
            return Failure("http")
        finally:
            send.close()
        process.join(max(0, _remaining(deadline) - 2 * grace))
        if process.is_alive():
            _reap_or_abort(process, deadline, grace)
            reaped = True
            return Failure("deadline")
        if receive.poll(0):
            failure = receive.recv()
        else:
            failure = Failure("http")
        _reap_or_abort(process, deadline, grace)
        reaped = True
        return failure
    finally:
        receive.close()
        if process_started and not reaped:
            _reap_or_abort(process, deadline, grace)


def verify_health(
    url: str,
    expected_version: str,
    *,
    connection_factory: ConnectionFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
    resolved_address: str | None = None,
    bind_device: str | None = None,
) -> None:
    """Return only after exact health success; otherwise raise safe final failure."""

    host, port = _target(url)
    if connection_factory is None:
        def connection_factory(
            requested_host: str,
            port: int | None = None,
            timeout: float | None = None,
        ) -> Connection:
            return _connection(
                requested_host,
                port=port,
                timeout=timeout,
                resolved_address=resolved_address,
                bind_device=bind_device,
            )
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
    parser.add_argument("--expected-version")
    parser.add_argument("--resolve-addresses", action="store_true")
    parser.add_argument("--resolved-address")
    parser.add_argument("--bind-device")
    args = parser.parse_args(argv)
    if args.resolve_addresses:
        if args.expected_version is not None or args.resolved_address is not None or args.bind_device is not None:
            parser.error("--resolve-addresses cannot be combined with health verification options")
        try:
            for family, address in resolve_target_addresses(args.url):
                print(f"{family} {address}")
        except (HealthCheckError, ValueError, socket.gaierror) as exc:
            print(Failure("dns").render() if isinstance(exc, socket.gaierror) else exc, file=sys.stderr)
            return 1
        return 0
    if args.expected_version is None:
        parser.error("--expected-version is required for health verification")
    try:
        verify_health(
            args.url,
            args.expected_version,
            resolved_address=args.resolved_address,
            bind_device=args.bind_device,
        )
    except (HealthCheckError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
