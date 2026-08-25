"""P09.4 contracts for bounded Cloudflare validation and deployment."""
from __future__ import annotations

from pathlib import Path
import json
import multiprocessing
import os
import re
import shutil
import signal
import socket
import ssl
import subprocess
import time

import pytest
import yaml

from scripts import verify_cloudflare_health as health
from scripts.validate_cloudflare_config import validate_repository

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "cloudflare-deploy.yml"
VPN_HEALTH_SCRIPT = ROOT / "scripts" / "verify_cloudflare_health_via_vpn.sh"


def triggers(data: dict[str, object]) -> dict[str, object]:
    return data.get("on", data.get(True, {}))  # type: ignore[return-value]


def workflow() -> dict[str, object]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def named_step(job: dict[str, object], name: str) -> dict[str, object]:
    return next(step for step in job["steps"] if step.get("name") == name)  # type: ignore[index,union-attr,return-value]


def contract_copy(tmp_path: Path) -> Path:
    paths = (
        ".github/workflows/cloudflare-deploy.yml",
        "cloudflare/watchdog/package.json",
        "cloudflare/watchdog/package-lock.json",
        "cloudflare/watchdog/wrangler.jsonc",
        "cloudflare/watchdog/src/index.ts",
        "runbooks/CLOUDFLARE-WATCHDOG.md",
        "scripts/verify_cloudflare_health.py",
        "scripts/verify_cloudflare_health_via_vpn.sh",
    )
    for relative in paths:
        source = ROOT / relative
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return tmp_path


def test_repository_cloudflare_deploy_contract_is_valid() -> None:
    assert validate_repository(ROOT) == []
    package = json.loads(
        (ROOT / "cloudflare" / "watchdog" / "package.json").read_text(encoding="utf-8")
    )
    assert package["scripts"]["deploy:dry-run"] == 'wrangler deploy --dry-run --env=""'
    wrangler = json.loads(
        (ROOT / "cloudflare" / "watchdog" / "wrangler.jsonc").read_text(encoding="utf-8")
    )
    assert wrangler.get("workers_dev") is False
    assert wrangler.get("routes") == [
        {
            "pattern": "production.workers.desinfect.telacore.org",
            "custom_domain": True,
        }
    ]
    staging = wrangler["env"]["staging"]
    assert staging.get("workers_dev") is False
    assert staging.get("routes") == [
        {
            "pattern": "staging.workers.desinfect.telacore.org",
            "custom_domain": True,
        }
    ]


def test_pull_requests_and_main_pushes_validate_without_deploying() -> None:
    data = workflow()
    assert set(triggers(data)) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers(data)["push"] == {"branches": ["main"]}
    assert data["permissions"] == {"contents": "read"}
    validate = data["jobs"]["validate"]  # type: ignore[index]
    assert "environment" not in validate
    assert "secrets." not in str(validate)
    assert "concurrency" not in data
    assert "concurrency" not in validate


def test_only_deploy_jobs_share_the_fixed_serialization_group() -> None:
    jobs = workflow()["jobs"]
    expected = {
        "group": "cloudflare-watchdog-deploy",
        "cancel-in-progress": False,
    }
    assert jobs["deploy_staging"]["concurrency"] == expected
    assert jobs["deploy_production"]["concurrency"] == expected


def test_manual_main_deploy_runs_staging_before_production() -> None:
    jobs = workflow()["jobs"]
    staging = jobs["deploy_staging"]
    production = jobs["deploy_production"]
    assert staging["if"] == "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'"
    assert staging["environment"] == {"name": "cloudflare-watchdog-staging"}
    assert production["needs"] == ["validate", "deploy_staging"]
    assert production["if"] == "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' && inputs.deploy_production"
    assert production["environment"] == {"name": "cloudflare-watchdog-production"}
    assert "--env staging" in named_step(staging, "Deploy staging without cron")["run"]
    assert '--env=""' in named_step(production, "Deploy production with cron")["run"]


def test_deploy_uses_locked_cli_and_checks_exact_health_version() -> None:
    jobs = workflow()["jobs"]
    for name in ("validate", "deploy_staging", "deploy_production"):
        job = jobs[name]
        for step in job["steps"]:
            uses = step.get("uses")
            if uses:
                assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", uses.split(" #", 1)[0])
        assert named_step(job, "Install locked Worker dependencies")["run"] == (
            "npm --prefix cloudflare/watchdog ci --ignore-scripts"
        )
    for name in ("deploy_staging", "deploy_production"):
        health = named_step(jobs[name], "Verify deployed health and version through VPN")
        assert health["env"]["EXPECTED_VERSION"] == "${{ github.sha }}"
        assert health["env"]["WATCHDOG_HEALTH_URL"] == "${{ secrets.CLOUDFLARE_WATCHDOG_HEALTH_URL }}"
        assert health["run"] == (
            'sh scripts/verify_cloudflare_health_via_vpn.sh --url "$WATCHDOG_HEALTH_URL" '
            '--expected-version "$EXPECTED_VERSION"'
        )


def test_deploy_runs_vpn_only_for_exact_version_health_verification() -> None:
    """Catches VPN exposure outside health, missing fallback secrets, or a bypassed verifier."""

    jobs = workflow()["jobs"]
    expected_install = (
        "if ! command -v openvpn >/dev/null 2>&1; then\n"
        "  sudo apt-get update\n"
        "  sudo apt-get install --no-install-recommends --yes openvpn\n"
        "fi"
    )
    expected_env = {
        "EXPECTED_VERSION": "${{ github.sha }}",
        "WATCHDOG_HEALTH_URL": "${{ secrets.CLOUDFLARE_WATCHDOG_HEALTH_URL }}",
        "VPN_CONFIG_DE": "${{ secrets.CLOUDFLARE_HEALTH_VPN_DE_CONFIG }}",
        "VPN_CONFIG_NL": "${{ secrets.CLOUDFLARE_HEALTH_VPN_NL_CONFIG }}",
        "VPN_CONFIG_CH": "${{ secrets.CLOUDFLARE_HEALTH_VPN_CH_CONFIG }}",
        "VPN_AUTH": "${{ secrets.CLOUDFLARE_HEALTH_VPN_AUTH }}",
    }
    expected_verify = (
        'sh scripts/verify_cloudflare_health_via_vpn.sh --url "$WATCHDOG_HEALTH_URL" '
        '--expected-version "$EXPECTED_VERSION"'
    )
    for name in ("deploy_staging", "deploy_production"):
        steps = jobs[name]["steps"]
        install = named_step(jobs[name], "Install OpenVPN for VPN health verification")
        verify = named_step(jobs[name], "Verify deployed health and version through VPN")
        assert install["run"].strip() == expected_install
        assert verify["env"] == expected_env
        assert verify["run"] == expected_verify
        assert steps.index(install) > steps.index(named_step(jobs[name], "Deploy staging without cron") if name == "deploy_staging" else named_step(jobs[name], "Deploy production with cron"))


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)


def _vpn_test_environment(
    tmp_path: Path,
    *,
    openvpn_failures: str = "",
    health_failures: str = "",
    routed_families: str = "4,6",
    de_config: str | None = None,
    ignore_term: bool = False,
) -> dict[str, str]:
    """Create process doubles below the script's OpenVPN and health boundaries."""

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "vpn-state"
    attempts = tmp_path / "openvpn-attempts"
    health_attempts = tmp_path / "health-attempts"
    route_attempts = tmp_path / "route-attempts"
    overlap = tmp_path / "vpn-overlap"
    timeout_args = tmp_path / "timeout-args"
    daemon_pids = tmp_path / "openvpn-daemon-pids"
    _write_executable(
        fake_bin / "sudo",
        "#!/bin/sh\nif test \"${1:-}\" = -n; then shift; fi\nexec \"$@\"\n",
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/bin/sh\nexit 0\n",
    )
    _write_executable(
        fake_bin / "timeout",
        """#!/bin/sh
set -eu
printf '%s\\n' "$*" >> "$VPN_TIMEOUT_ARGS"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --signal=*|--kill-after=*|--foreground) shift ;;
    *) break ;;
  esac
done
shift
exec "$@"
""",
    )
    _write_executable(
        fake_bin / "openvpn-daemon",
        """#!/bin/sh
if test "${VPN_IGNORE_TERM:-}" = 1; then trap '' TERM; fi
while :; do /bin/sleep 60; done
""",
    )
    _write_executable(
        fake_bin / "openvpn",
        """#!/bin/sh
set -eu
config=''
pidfile=''
while [ "$#" -gt 0 ]; do
  case "$1" in
    --config) config="$2"; shift 2 ;;
    --writepid) pidfile="$2"; shift 2 ;;
    *) shift ;;
  esac
done
name=$(basename "$config")
printf '%s\\n' "$name" >> "$VPN_ATTEMPTS"
if test -e "$VPN_STATE"; then
  printf '%s\\n' "$name" >> "$VPN_OVERLAP"
fi
case ",${VPN_OPENVPN_FAILURES}," in
  *",${name},"*) exit 1 ;;
esac
"$(dirname "$0")/openvpn-daemon" --config "$config" --writepid "$pidfile" &
printf '%s\\n' "$!" > "$pidfile"
printf '%s\\n' "$!" >> "$VPN_DAEMON_PIDS"
printf '%s\\n' "$name" > "$VPN_STATE"
""",
    )
    _write_executable(
        fake_bin / "ip",
        """#!/bin/sh
set -eu
family=''
if test "$1" = '-4' || test "$1" = '-6'; then
  family=${1#-}
  shift
fi
case "$1:$2" in
  link:show) test -f "$VPN_STATE" ;;
  route:get)
    printf '%s:%s\\n' "$family" "$3" >> "$VPN_ROUTE_ATTEMPTS"
    test -f "$VPN_STATE"
    case ",${VPN_ROUTED_FAMILIES}," in
      *",${family},"*) printf '%s\\n' "$3 dev tun-health" ;;
      *) printf '%s\\n' "$3 dev eth0" ;;
    esac
    ;;
  link:delete)
    test "${VPN_DELETE_TUN_FAIL:-}" != 1
    rm -f "$VPN_STATE"
    ;;
  *) exit 1 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "fake-python",
        """#!/bin/sh
set -eu
case "$*" in
  *--resolve-addresses*)
    printf '%s\\n' "$VPN_RESOLVED_ADDRESSES"
    exit 0
    ;;
esac
if test "${VPN_HEALTH_HANG:-}" = 1; then
  /bin/sleep 60
fi
printf '%s\n' "$*" >> "$VPN_HEALTH_ARGS"
name=$(cat "$VPN_STATE")
printf '%s\\n' "$name" >> "$VPN_HEALTH_ATTEMPTS"
case ",${VPN_HEALTH_FAILURES}," in
  *",${name},"*) exit 1 ;;
esac
""",
    )
    return {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "PYTHON_BIN": str(fake_bin / "fake-python"),
        "VPN_STATE": str(state),
        "VPN_ATTEMPTS": str(attempts),
        "VPN_HEALTH_ATTEMPTS": str(health_attempts),
        "VPN_HEALTH_ARGS": str(tmp_path / "health-args"),
        "VPN_ROUTE_ATTEMPTS": str(route_attempts),
        "VPN_OVERLAP": str(overlap),
        "VPN_TIMEOUT_ARGS": str(timeout_args),
        "VPN_DAEMON_PIDS": str(daemon_pids),
        "VPN_OPENVPN_FAILURES": openvpn_failures,
        "VPN_HEALTH_FAILURES": health_failures,
        "VPN_IGNORE_TERM": "1" if ignore_term else "",
        "VPN_ROUTED_FAMILIES": routed_families,
        "VPN_RESOLVED_ADDRESSES": "4 198.51.100.17\n6 2001:db8::17",
        "VPN_CONFIG_DE": de_config
        or "client\nremote endpoint.invalid 1194\nproto udp\nauth-user-pass\n<ca>\ncertificate-data\n</ca>\n",
        "VPN_CONFIG_NL": "client\nremote endpoint.invalid 1194\nproto udp\nauth-user-pass\n<ca>\ncertificate-data\n</ca>\n",
        "VPN_CONFIG_CH": "client\nremote endpoint.invalid 1194\nproto udp\nauth-user-pass\n<ca>\ncertificate-data\n</ca>\n",
        "VPN_AUTH": "vpn-user-secret\\nvpn-password-secret",
    }


def _run_vpn_health(tmp_path: Path, **kwargs: str) -> subprocess.CompletedProcess[str]:
    environment = _vpn_test_environment(tmp_path, **kwargs)
    return subprocess.run(
        [
            "sh",
            str(VPN_HEALTH_SCRIPT),
            "--url",
            "https://watchdog.example/healthz",
            "--expected-version",
            "abc123",
        ],
        check=False,
        capture_output=True,
        env=environment,
        text=True,
    )


def test_vpn_health_falls_back_de_nl_ch_and_redacts_temporary_secrets(tmp_path: Path) -> None:
    """Catches reordering, skipped fallback, retained temp files, or leaked secret data."""

    result = _run_vpn_health(
        tmp_path,
        openvpn_failures="vpn-de.premiumize.me.ovpn",
        health_failures="vpn-nl.premiumize.me.ovpn",
    )

    assert result.returncode == 0
    assert (tmp_path / "openvpn-attempts").read_text(encoding="utf-8").splitlines() == [
        "vpn-de.premiumize.me.ovpn",
        "vpn-nl.premiumize.me.ovpn",
        "vpn-ch.premiumize.me.ovpn",
    ]
    assert not (tmp_path / "vpn-state").exists()
    assert not list(tmp_path.glob("cloudflare-health-vpn.*"))
    assert not (tmp_path / "vpn-overlap").exists()
    for pid in (tmp_path / "openvpn-daemon-pids").read_text(encoding="utf-8").splitlines():
        assert not Path(f"/proc/{pid}").exists()
    output = result.stdout + result.stderr
    assert "config-secret" not in output
    assert "vpn-user-secret" not in output
    assert "vpn-password-secret" not in output


def test_vpn_health_stops_after_de_health_success_and_cleans_up(tmp_path: Path) -> None:
    """Catches unnecessary fallback after a successful public health verification."""

    result = _run_vpn_health(tmp_path)

    assert result.returncode == 0
    assert (tmp_path / "openvpn-attempts").read_text(encoding="utf-8").splitlines() == [
        "vpn-de.premiumize.me.ovpn"
    ]
    assert (tmp_path / "health-attempts").read_text(encoding="utf-8").splitlines() == [
        "vpn-de.premiumize.me.ovpn"
    ]
    assert not (tmp_path / "vpn-state").exists()


def test_vpn_health_fails_closed_after_all_country_health_failures(tmp_path: Path) -> None:
    """Catches a false positive when all three VPN egress health checks reject."""

    result = _run_vpn_health(
        tmp_path,
        health_failures="vpn-de.premiumize.me.ovpn,vpn-nl.premiumize.me.ovpn,vpn-ch.premiumize.me.ovpn",
    )

    assert result.returncode == 1
    assert (tmp_path / "health-attempts").read_text(encoding="utf-8").splitlines() == [
        "vpn-de.premiumize.me.ovpn",
        "vpn-nl.premiumize.me.ovpn",
        "vpn-ch.premiumize.me.ovpn",
    ]
    assert not (tmp_path / "vpn-state").exists()


def test_vpn_health_kills_term_ignoring_daemon_before_next_country(tmp_path: Path) -> None:
    """Catches a fallback that starts another country before an uncooperative daemon dies."""

    result = _run_vpn_health(
        tmp_path,
        health_failures="vpn-de.premiumize.me.ovpn",
        ignore_term=True,
    )

    assert result.returncode == 0
    assert not (tmp_path / "vpn-overlap").exists()
    for pid in (tmp_path / "openvpn-daemon-pids").read_text(encoding="utf-8").splitlines():
        assert not Path(f"/proc/{pid}").exists()


def test_vpn_health_routes_resolved_ipv4_and_ipv6_target_addresses_through_tun(tmp_path: Path) -> None:
    """Catches readiness that verifies a surrogate address instead of the health target."""

    result = _run_vpn_health(tmp_path)

    assert result.returncode == 0
    assert (tmp_path / "route-attempts").read_text(encoding="utf-8").splitlines() == [
        "4:198.51.100.17",
        "6:2001:db8::17",
    ]
    timeout_arguments = (tmp_path / "timeout-args").read_text(encoding="utf-8").split()
    assert timeout_arguments[:5] == [
        "--foreground",
        "--signal=TERM",
        "--kill-after=5s",
        "45s",
        "openvpn",
    ]
    assert "--auth-nocache" in timeout_arguments
    assert "--auth-retry" in timeout_arguments
    assert "nointeract" in timeout_arguments
    health_arguments = (tmp_path / "health-args").read_text(encoding="utf-8").split()
    assert "--resolved-address" in health_arguments
    assert "198.51.100.17" in health_arguments
    assert "--bind-device" in health_arguments
    assert "tun-health" in health_arguments


@pytest.mark.parametrize(
    ("routed_families", "expected_address"),
    (("4", "198.51.100.17"), ("6", "2001:db8::17")),
    ids=("ipv4-only", "ipv6-only"),
)
def test_vpn_health_uses_only_tunnel_routed_target_address(
    tmp_path: Path, routed_families: str, expected_address: str
) -> None:
    """Catches a verifier that falls back to a directly routed address family."""

    result = _run_vpn_health(tmp_path, routed_families=routed_families)

    assert result.returncode == 0
    health_arguments = (tmp_path / "health-args").read_text(encoding="utf-8").split()
    assert health_arguments[health_arguments.index("--resolved-address") + 1] == expected_address
    assert health_arguments[health_arguments.index("--bind-device") + 1] == "tun-health"


def test_vpn_health_rejects_when_no_resolved_target_address_routes_through_tunnel(
    tmp_path: Path,
) -> None:
    """Catches a direct health fallback when neither target address family uses the tunnel."""

    result = _run_vpn_health(tmp_path, routed_families="")

    assert result.returncode == 1
    assert not (tmp_path / "health-attempts").exists()


@pytest.mark.parametrize(
    "de_config",
    (
        "client\nremote endpoint.invalid 1194\nup /bin/false\n",
        "client\nremote endpoint.invalid 1194\naskpass /tmp/prompt\n",
        "client\nremote endpoint.invalid 1194\n<key>\n-----BEGIN ENCRYPTED PRIVATE KEY-----\n</key>\n",
    ),
    ids=("script", "prompt", "encrypted-key"),
)
def test_vpn_health_rejects_privileged_or_prompting_config_before_openvpn(
    tmp_path: Path, de_config: str
) -> None:
    """Catches privileged OpenVPN execution of secret-provided scripts or prompts."""

    result = _run_vpn_health(
        tmp_path,
        de_config=de_config,
    )

    assert result.returncode == 0
    assert (tmp_path / "openvpn-attempts").read_text(encoding="utf-8").splitlines() == [
        "vpn-nl.premiumize.me.ovpn"
    ]
    assert "/bin/false" not in result.stdout + result.stderr
    assert "PRIVATE KEY" not in result.stdout + result.stderr


def test_vpn_health_signal_exit_reaps_openvpn_daemon(tmp_path: Path) -> None:
    """Catches TERM exit that leaves a VPN daemon or tunnel behind."""

    environment = _vpn_test_environment(tmp_path)
    environment["VPN_HEALTH_HANG"] = "1"
    process = subprocess.Popen(
        [
            "sh",
            str(VPN_HEALTH_SCRIPT),
            "--url",
            "https://watchdog.example/healthz",
            "--expected-version",
            "abc123",
        ],
        env=environment,
        start_new_session=True,
    )
    deadline = time.monotonic() + 5
    while not (tmp_path / "vpn-state").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (tmp_path / "vpn-state").exists()
    os.killpg(process.pid, signal.SIGTERM)
    assert process.wait(timeout=5) == 143
    assert not (tmp_path / "vpn-state").exists()
    for pid in (tmp_path / "openvpn-daemon-pids").read_text(encoding="utf-8").splitlines():
        assert not Path(f"/proc/{pid}").exists()


class FakeResponse:
    def __init__(
        self,
        status: int,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}

    def read(self) -> bytes:
        return self.body

    def getheader(self, name: str) -> str | None:
        return self.headers.get(name)


class FakeConnection:
    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self.outcome = outcome

    def request(self, method: str, path: str, **_: object) -> None:
        assert (method, path) == ("GET", "/healthz")
        if isinstance(self.outcome, BaseException):
            raise self.outcome

    def getresponse(self) -> FakeResponse:
        assert isinstance(self.outcome, FakeResponse)
        return self.outcome

    def close(self) -> None:
        pass


class FakeConnections:
    def __init__(self, outcomes: list[FakeResponse | BaseException]) -> None:
        self.outcomes = outcomes
        self.calls = multiprocessing.Value("i", 0)

    def __call__(self, host: str, port: int | None = None, timeout: float | None = None) -> FakeConnection:
        assert host == "watchdog.example"
        assert port is None
        assert timeout == health.REQUEST_TIMEOUT_SECONDS
        with self.calls.get_lock():
            outcome = self.outcomes[self.calls.value]
            self.calls.value += 1
        return FakeConnection(outcome)


def healthy_response(version: str = "abc123") -> FakeResponse:
    return FakeResponse(
        200,
        json.dumps(
            {"service": "desinfect-watchdog", "status": "ok", "version": version}
        ).encode(),
    )


def test_health_check_rejects_non_https_or_non_healthz_url() -> None:
    for url in ("http://watchdog.example/healthz", "https://watchdog.example/other"):
        with pytest.raises(ValueError, match="HTTPS.*healthz"):
            health.verify_health(url, "abc123")


def test_health_resolves_distinct_ipv4_and_ipv6_target_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Catches target-route checks that omit an address family or use a surrogate."""

    def addresses(host: str, port: int, *, type: int) -> list[tuple[object, ...]]:
        assert (host, port, type) == ("watchdog.example", 443, socket.SOCK_STREAM)
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::17", 443, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.17", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.17", 443)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", addresses)

    assert health.resolve_target_addresses("https://watchdog.example/healthz") == (
        (4, "198.51.100.17"),
        (6, "2001:db8::17"),
    )


def test_health_check_connects_to_verified_address_without_changing_tls_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a verifier that resolves the hostname again after VPN route validation."""

    captured = multiprocessing.Value("i", 0)

    def connection(
        host: str,
        port: int | None = None,
        timeout: float | None = None,
        *,
        resolved_address: str | None = None,
        bind_device: str | None = None,
    ) -> FakeConnection:
        captured.value = int(
            (host, port, timeout, resolved_address, bind_device)
            == ("watchdog.example", None, health.REQUEST_TIMEOUT_SECONDS, "198.51.100.17", None)
        )
        return FakeConnection(healthy_response())

    monkeypatch.setattr(health, "_connection", connection)

    health.verify_health(
        "https://watchdog.example/healthz",
        "abc123",
        resolved_address="198.51.100.17",
        sleep=lambda _: None,
    )

    assert captured.value == 1


def test_resolved_health_socket_binds_to_tunnel_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a verified target connection that can still leave through another interface."""

    calls: list[tuple[object, ...]] = []

    class FakeSocket:
        def settimeout(self, timeout: float | None) -> None:
            calls.append(("timeout", timeout))

        def setsockopt(self, level: int, option: int, value: bytes) -> None:
            calls.append(("setsockopt", level, option, value))

        def connect(self, address: tuple[object, ...]) -> None:
            calls.append(("connect", address))

        def close(self) -> None:
            calls.append(("close",))

    class FakeContext:
        def wrap_socket(self, sock: FakeSocket, *, server_hostname: str) -> FakeSocket:
            calls.append(("tls", server_hostname))
            return sock

    fake_socket = FakeSocket()
    monkeypatch.setattr(socket, "socket", lambda *_: fake_socket)
    connection = health._ResolvedHTTPSConnection(
        "watchdog.example",
        resolved_address="198.51.100.17",
        bind_device="tun-health",
        port=443,
        timeout=health.REQUEST_TIMEOUT_SECONDS,
    )
    connection._context = FakeContext()  # type: ignore[assignment]

    connection.connect()

    assert calls == [
        ("timeout", health.REQUEST_TIMEOUT_SECONDS),
        ("setsockopt", socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"tun-health\0"),
        ("connect", ("198.51.100.17", 443)),
        ("tls", "watchdog.example"),
    ]


@pytest.mark.parametrize(
    "first_outcome",
    (
        socket.gaierror("unresolved"),
        ssl.SSLError("certificate not ready"),
        FakeResponse(503),
        FakeResponse(200, b"not json"),
        healthy_response("old-version"),
    ),
    ids=("dns", "tls", "http", "json", "version"),
)
def test_health_check_retries_transient_failures_until_exact_success(
    first_outcome: FakeResponse | BaseException,
) -> None:
    connections = FakeConnections([first_outcome, healthy_response()])
    health.verify_health(
        "https://watchdog.example/healthz",
        "abc123",
        connection_factory=connections,
        sleep=lambda _: None,
    )
    assert connections.calls.value == 2


def test_health_check_does_not_follow_redirect_and_reports_safe_details() -> None:
    redirect = FakeResponse(
        301,
        headers={"Location": "https://awsas.de/403-page.html?token=secret-token"},
    )
    connections = FakeConnections([redirect] * health.MAX_ATTEMPTS)
    with pytest.raises(health.HealthCheckError) as caught:
        health.verify_health(
            "https://watchdog.example/healthz",
            "abc123",
            connection_factory=connections,
            sleep=lambda _: None,
        )
    assert connections.calls.value == health.MAX_ATTEMPTS
    assert str(caught.value) == (
        "health check failed: category=http http_status=301 location_hostname=awsas.de"
    )
    assert "secret-token" not in str(caught.value)
    assert "403-page" not in str(caught.value)


def test_health_check_omits_malformed_redirect_location() -> None:
    redirect = FakeResponse(302, headers={"Location": "https://[bad?token=secret-token"})
    connections = FakeConnections([redirect] * health.MAX_ATTEMPTS)
    with pytest.raises(health.HealthCheckError) as caught:
        health.verify_health(
            "https://watchdog.example/healthz",
            "abc123",
            connection_factory=connections,
            sleep=lambda _: None,
        )
    assert str(caught.value) == "health check failed: category=http http_status=302"
    assert "secret-token" not in str(caught.value)


def test_health_check_exact_response_returns_without_retry() -> None:
    connections = FakeConnections([healthy_response(), healthy_response()])
    health.verify_health(
        "https://watchdog.example/healthz",
        "abc123",
        connection_factory=connections,
        sleep=lambda _: None,
    )
    assert connections.calls.value == 1


def test_health_check_retries_after_process_start_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_start = multiprocessing.context.ForkProcess.start
    starts = 0
    sleeps: list[float] = []

    def flaky_start(process: multiprocessing.context.ForkProcess) -> None:
        nonlocal starts
        starts += 1
        if starts == 1:
            raise OSError("secret resource detail")
        original_start(process)

    monkeypatch.setattr(multiprocessing.context.ForkProcess, "start", flaky_start)
    connections = FakeConnections([healthy_response()])

    health.verify_health(
        "https://watchdog.example/healthz",
        "abc123",
        connection_factory=connections,
        sleep=sleeps.append,
    )

    assert starts == 2
    assert connections.calls.value == 1
    assert sleeps == [health.RETRY_DELAY_SECONDS]


def test_health_check_cli_safely_reports_process_start_oserror(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail_start(_: multiprocessing.context.ForkProcess) -> None:
        raise OSError("secret resource detail")

    monkeypatch.setattr(health, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(multiprocessing.context.ForkProcess, "start", fail_start)

    assert (
        health.main(["--url", "https://watchdog.example/healthz", "--expected-version", "abc123"])
        == 1
    )
    assert capsys.readouterr().err == "health check failed: category=http\n"


def test_health_check_cli_logs_only_safe_failure_details(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    error = health.HealthCheckError(
        "health check failed: category=http http_status=301 location_hostname=awsas.de"
    )

    def fail(*_: object, **__: object) -> None:
        raise error

    monkeypatch.setattr(health, "verify_health", fail)
    assert health.main(["--url", "https://watchdog.example/healthz", "--expected-version", "abc123"]) == 1
    assert capsys.readouterr().err == f"{error}\n"


def test_health_check_deadline_covers_blocking_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(health, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(health, "ATTEMPT_DEADLINE_SECONDS", 0.15, raising=False)

    def blocking_dns(*_: object, **__: object) -> object:
        time.sleep(0.75)
        raise socket.gaierror("unresolved")

    monkeypatch.setattr(socket, "getaddrinfo", blocking_dns)
    started = time.monotonic()
    with pytest.raises(health.HealthCheckError, match="category=deadline"):
        health.verify_health("https://watchdog.example/healthz", "abc123")
    assert time.monotonic() - started < 0.5


def test_health_check_deadline_covers_trickling_response_read(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowResponse(FakeResponse):
        def read(self) -> bytes:
            time.sleep(0.75)
            return healthy_response().read()

    monkeypatch.setattr(health, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(health, "ATTEMPT_DEADLINE_SECONDS", 0.15, raising=False)
    started = time.monotonic()
    with pytest.raises(health.HealthCheckError, match="category=deadline"):
        health.verify_health(
            "https://watchdog.example/healthz",
            "abc123",
            connection_factory=FakeConnections([SlowResponse(200)]),
        )
    assert time.monotonic() - started < 0.5


def test_health_check_reaps_term_ignoring_child_before_return(monkeypatch: pytest.MonkeyPatch) -> None:
    child_pid = multiprocessing.Value("i", 0)

    class TermIgnoringResponse(FakeResponse):
        def read(self) -> bytes:
            child_pid.value = os.getpid()
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            time.sleep(0.75)
            return healthy_response().read()

    monkeypatch.setattr(health, "MAX_ATTEMPTS", 1)
    monkeypatch.setattr(health, "ATTEMPT_DEADLINE_SECONDS", 0.5)
    monkeypatch.setattr(health, "CLEANUP_GRACE_SECONDS", 0.1, raising=False)
    started = time.monotonic()
    with pytest.raises(health.HealthCheckError, match="category=deadline"):
        health.verify_health(
            "https://watchdog.example/healthz",
            "abc123",
            connection_factory=FakeConnections([TermIgnoringResponse(200)]),
        )
    assert child_pid.value
    assert time.monotonic() - started < 0.65
    assert not Path(f"/proc/{child_pid.value}").exists()
    assert all(process.pid != child_pid.value for process in multiprocessing.active_children())


def test_health_check_cli_redacts_malformed_port(capsys: pytest.CaptureFixture[str]) -> None:
    secret = "secret-token"
    assert health.main(
        ["--url", f"https://watchdog.example:{secret}/healthz", "--expected-version", "abc123"]
    ) == 1
    assert capsys.readouterr().err == "health check failed: category=url\n"


def test_validator_rejects_tagged_action(tmp_path: Path) -> None:
    root = contract_copy(tmp_path)
    path = root / ".github" / "workflows" / "cloudflare-deploy.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
            "actions/checkout@v7",
            1,
        ),
        encoding="utf-8",
    )
    assert any(issue.code == "CFD006" for issue in validate_repository(root))


@pytest.mark.parametrize(
    "secret_expression",
    ("${{ secrets['TOKEN'] }}", '${{ secrets["TOKEN"] }}'),
)
def test_validator_rejects_bracket_notation_secret_in_validate_job(
    tmp_path: Path,
    secret_expression: str,
) -> None:
    root = contract_copy(tmp_path)
    assert validate_repository(root) == []
    path = root / ".github" / "workflows" / "cloudflare-deploy.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["jobs"]["validate"]["env"] = {"LEAKED_TOKEN": secret_expression}
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert [issue.code for issue in validate_repository(root)] == ["CFD004"]


def test_validator_rejects_staging_cron(tmp_path: Path) -> None:
    root = contract_copy(tmp_path)
    path = root / "cloudflare" / "watchdog" / "wrangler.jsonc"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"crons": []', '"crons": ["0 2 * * *"]', 1),
        encoding="utf-8",
    )
    assert any(issue.code == "CFD010" for issue in validate_repository(root))


@pytest.mark.parametrize(
    ("environment", "replacement"),
    (
        ("production", {"workers_dev": True}),
        (
            "production",
            {
                "routes": [
                    {
                        "pattern": "invalid.example.org",
                        "custom_domain": True,
                    }
                ]
            },
        ),
        ("staging", {"workers_dev": True}),
        (
            "staging",
            {
                "routes": [
                    {
                        "pattern": "production.workers.desinfect.telacore.org",
                        "custom_domain": True,
                    }
                ]
            },
        ),
    ),
)
def test_validator_rejects_workers_dev_or_wrong_custom_domain(
    tmp_path: Path,
    environment: str,
    replacement: dict[str, object],
) -> None:
    root = contract_copy(tmp_path)
    path = root / "cloudflare" / "watchdog" / "wrangler.jsonc"
    config = json.loads(path.read_text(encoding="utf-8"))
    config.update(
        {
            "workers_dev": False,
            "routes": [
                {
                    "pattern": "production.workers.desinfect.telacore.org",
                    "custom_domain": True,
                }
            ],
        }
    )
    config["env"]["staging"].update(
        {
            "workers_dev": False,
            "routes": [
                {
                    "pattern": "staging.workers.desinfect.telacore.org",
                    "custom_domain": True,
                }
            ],
        }
    )
    path.write_text(json.dumps(config), encoding="utf-8")
    assert validate_repository(root) == []

    target = config if environment == "production" else config["env"]["staging"]
    target.update(replacement)
    path.write_text(json.dumps(config), encoding="utf-8")

    assert [issue.code for issue in validate_repository(root)] == ["CFD012"]


def test_validator_rejects_unguarded_or_unstaged_production(tmp_path: Path) -> None:
    root = contract_copy(tmp_path)
    path = root / ".github" / "workflows" / "cloudflare-deploy.yml"
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        "needs: [validate, deploy_staging]",
        "needs: [validate]",
        1,
    ).replace(
        "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' && inputs.deploy_production",
        "github.ref == 'refs/heads/main'",
        1,
    )
    path.write_text(text, encoding="utf-8")
    codes = {issue.code for issue in validate_repository(root)}
    assert {"CFD004", "CFD005"} <= codes


def test_validator_rejects_missing_health_version_check(tmp_path: Path) -> None:
    root = contract_copy(tmp_path)
    path = root / ".github" / "workflows" / "cloudflare-deploy.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            '--expected-version "$EXPECTED_VERSION"',
            '--expected-version "$OTHER_VERSION"',
            1,
        ),
        encoding="utf-8",
    )
    assert any(issue.code == "CFD009" for issue in validate_repository(root))


def test_validator_rejects_non_shared_health_readiness_command(tmp_path: Path) -> None:
    root = contract_copy(tmp_path)
    path = root / ".github" / "workflows" / "cloudflare-deploy.yml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            'sh scripts/verify_cloudflare_health_via_vpn.sh --url "$WATCHDOG_HEALTH_URL" --expected-version "$EXPECTED_VERSION"',
            "curl https://watchdog.example/healthz",
            1,
        ),
        encoding="utf-8",
    )
    assert any(issue.code == "CFD009" for issue in validate_repository(root))


def test_validator_rejects_vpn_health_before_openvpn_install(tmp_path: Path) -> None:
    """Catches a workflow that opens the tunnel before its dependencies are installed."""

    root = contract_copy(tmp_path)
    path = root / ".github" / "workflows" / "cloudflare-deploy.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for name in ("deploy_staging", "deploy_production"):
        steps = data["jobs"][name]["steps"]
        install_index = next(
            index
            for index, step in enumerate(steps)
            if step["name"] == "Install OpenVPN for VPN health verification"
        )
        health_index = next(
            index
            for index, step in enumerate(steps)
            if step["name"] == "Verify deployed health and version through VPN"
        )
        steps[install_index], steps[health_index] = steps[health_index], steps[install_index]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    assert any(issue.code == "CFD009" for issue in validate_repository(root))


def test_validator_rejects_missing_vpn_hardening_invariant(tmp_path: Path) -> None:
    """Catches removal of the noninteractive OpenVPN hard timeout from the wrapper."""

    root = contract_copy(tmp_path)
    path = root / "scripts" / "verify_cloudflare_health_via_vpn.sh"
    path.write_text(
        path.read_text(encoding="utf-8").replace("--auth-retry nointeract", "--auth-retry interact"),
        encoding="utf-8",
    )

    assert any(issue.code == "CFD009" for issue in validate_repository(root))


def test_validator_rejects_missing_tunnel_socket_binding(tmp_path: Path) -> None:
    """Catches a wrapper flag that no longer forces the health socket through the VPN."""

    root = contract_copy(tmp_path)
    path = root / "scripts" / "verify_cloudflare_health.py"
    path.write_text(
        path.read_text(encoding="utf-8").replace("socket.SO_BINDTODEVICE", "socket.SO_RCVBUF"),
        encoding="utf-8",
    )

    assert any(issue.code == "CFD009" for issue in validate_repository(root))


@pytest.mark.parametrize(
    ("original", "replacement"),
    (
        (
            'npm --prefix cloudflare/watchdog run deploy -- --env staging --var "DEPLOYMENT_VERSION:${GITHUB_SHA}"',
            "npm --prefix cloudflare/watchdog run deploy -- --env staging",
        ),
        (
            'npm --prefix cloudflare/watchdog run deploy -- --env staging --var "DEPLOYMENT_VERSION:${GITHUB_SHA}"',
            'npm --prefix cloudflare/watchdog run deploy -- --env staging --var "RELEASE_VERSION:${GITHUB_SHA}"',
        ),
        (
            'npm --prefix cloudflare/watchdog run deploy -- --env="" --var "DEPLOYMENT_VERSION:${GITHUB_SHA}"',
            'npm --prefix cloudflare/watchdog run deploy -- --env=""',
        ),
        (
            'npm --prefix cloudflare/watchdog run deploy -- --env="" --var "DEPLOYMENT_VERSION:${GITHUB_SHA}"',
            'npm --prefix cloudflare/watchdog run deploy -- --env="" --var "RELEASE_VERSION:${GITHUB_SHA}"',
        ),
    ),
)
def test_validator_requires_exact_deployment_version_argument(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    root = contract_copy(tmp_path)
    path = root / ".github" / "workflows" / "cloudflare-deploy.yml"
    text = path.read_text(encoding="utf-8")
    assert original in text
    path.write_text(text.replace(original, replacement, 1), encoding="utf-8")
    assert any(issue.code == "CFD008" for issue in validate_repository(root))
