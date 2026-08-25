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


def _vpn_test_environment(tmp_path: Path, *, openvpn_failures: str = "", health_failures: str = "") -> dict[str, str]:
    """Create process doubles below the script's OpenVPN and health boundaries."""

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "vpn-state"
    attempts = tmp_path / "openvpn-attempts"
    health_attempts = tmp_path / "health-attempts"
    _write_executable(
        fake_bin / "sudo",
        "#!/bin/sh\nexec \"$@\"\n",
    )
    _write_executable(
        fake_bin / "sleep",
        "#!/bin/sh\nexit 0\n",
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
case ",${VPN_OPENVPN_FAILURES}," in
  *",${name},"*) exit 1 ;;
esac
/bin/sleep 60 &
printf '%s\\n' "$!" > "$pidfile"
printf '%s\\n' "$name" > "$VPN_STATE"
""",
    )
    _write_executable(
        fake_bin / "ip",
        """#!/bin/sh
set -eu
case "$1:$2" in
  link:show) test -f "$VPN_STATE" ;;
  route:get) test -f "$VPN_STATE" && printf '%s\\n' '1.1.1.1 dev tun-health' ;;
  link:delete) rm -f "$VPN_STATE" ;;
  *) exit 1 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "fake-python",
        """#!/bin/sh
set -eu
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
        "VPN_OPENVPN_FAILURES": openvpn_failures,
        "VPN_HEALTH_FAILURES": health_failures,
        "VPN_CONFIG_DE": "de-config-secret",
        "VPN_CONFIG_NL": "nl-config-secret",
        "VPN_CONFIG_CH": "ch-config-secret",
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
