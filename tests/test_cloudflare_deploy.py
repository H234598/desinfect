"""P09.4 contracts for bounded Cloudflare validation and deployment."""
from __future__ import annotations

from pathlib import Path
import json
import re
import shutil

import yaml

from scripts.validate_cloudflare_config import validate_repository

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "cloudflare-deploy.yml"


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


def test_pull_requests_and_main_pushes_validate_without_deploying() -> None:
    data = workflow()
    assert set(triggers(data)) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers(data)["push"] == {"branches": ["main"]}
    assert data["permissions"] == {"contents": "read"}
    validate = data["jobs"]["validate"]  # type: ignore[index]
    assert "environment" not in validate
    assert "secrets." not in str(validate)


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
        health = named_step(jobs[name], "Verify deployed health and version")
        assert health["env"]["EXPECTED_VERSION"] == "${{ github.sha }}"
        assert health["env"]["WATCHDOG_HEALTH_URL"] == "${{ secrets.CLOUDFLARE_WATCHDOG_HEALTH_URL }}"
        assert "--proto '=https'" in health["run"]
        assert "payload.version !== expected" in health["run"]


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


def test_validator_rejects_staging_cron(tmp_path: Path) -> None:
    root = contract_copy(tmp_path)
    path = root / "cloudflare" / "watchdog" / "wrangler.jsonc"
    path.write_text(
        path.read_text(encoding="utf-8").replace('"crons": []', '"crons": ["0 2 * * *"]', 1),
        encoding="utf-8",
    )
    assert any(issue.code == "CFD010" for issue in validate_repository(root))


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
            "payload.version !== expected",
            "false",
            1,
        ),
        encoding="utf-8",
    )
    assert any(issue.code == "CFD009" for issue in validate_repository(root))
