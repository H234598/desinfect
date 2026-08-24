#!/usr/bin/env python3
"""Validate fail-closed P09.4 Cloudflare deployment boundaries."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import NamedTuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
ACTION_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
DISPATCH_MAIN = "github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main'"


class ConfigIssue(NamedTuple):
    """One actionable Cloudflare deployment contract violation."""

    path: str
    code: str
    message: str

    def render(self) -> str:
        return f"{self.path}: {self.code} {self.message}"


def _issue(path: Path, code: str, message: str) -> ConfigIssue:
    return ConfigIssue(str(path), code, message)


def _triggers(workflow: dict[str, object]) -> object:
    return workflow.get("on", workflow.get(True))


def _step(job: dict[str, object], name: str) -> dict[str, object] | None:
    steps = job.get("steps")
    if not isinstance(steps, list):
        return None
    return next(
        (step for step in steps if isinstance(step, dict) and step.get("name") == name),
        None,
    )


def _load(path: Path, loader) -> tuple[object | None, ConfigIssue | None]:
    try:
        return loader(path.read_text(encoding="utf-8")), None
    except (OSError, ValueError, yaml.YAMLError) as exc:
        return None, _issue(path, "CFD001", f"nicht lesbar oder ungültig: {exc}")


def validate_repository(root: Path = ROOT) -> list[ConfigIssue]:
    """Return every P09.4 deployment boundary violation below ``root``."""

    workflow_path = root / ".github" / "workflows" / "cloudflare-deploy.yml"
    package_path = root / "cloudflare" / "watchdog" / "package.json"
    lock_path = root / "cloudflare" / "watchdog" / "package-lock.json"
    wrangler_path = root / "cloudflare" / "watchdog" / "wrangler.jsonc"
    worker_path = root / "cloudflare" / "watchdog" / "src" / "index.ts"
    runbook_path = root / "runbooks" / "CLOUDFLARE-WATCHDOG.md"
    issues: list[ConfigIssue] = []

    workflow_value, error = _load(workflow_path, yaml.safe_load)
    if error:
        return [error]
    if not isinstance(workflow_value, dict):
        return [_issue(workflow_path, "CFD002", "Workflowwurzel muss ein Mapping sein")]
    workflow = workflow_value
    triggers = _triggers(workflow)
    if not isinstance(triggers, dict) or set(triggers) != {"pull_request", "push", "workflow_dispatch"}:
        issues.append(_issue(workflow_path, "CFD002", "nur PR, main-Push und manueller Start sind erlaubt"))
    elif triggers.get("push") != {"branches": ["main"]}:
        issues.append(_issue(workflow_path, "CFD002", "Push-Validierung muss auf main begrenzt sein"))
    if workflow.get("permissions") != {"contents": "read"}:
        issues.append(_issue(workflow_path, "CFD003", "Workflowrechte müssen exakt contents: read sein"))

    jobs = workflow.get("jobs")
    if not isinstance(jobs, dict) or set(jobs) != {"validate", "deploy_staging", "deploy_production"}:
        issues.append(_issue(workflow_path, "CFD003", "Validate-, Staging- und Production-Job sind exakt erforderlich"))
        return issues
    validate = jobs["validate"]
    staging = jobs["deploy_staging"]
    production = jobs["deploy_production"]
    if not all(isinstance(job, dict) for job in (validate, staging, production)):
        return [*issues, _issue(workflow_path, "CFD003", "Jobs müssen Mappings sein")]

    if "environment" in validate or "secrets." in str(validate):
        issues.append(_issue(workflow_path, "CFD004", "PR-/Push-Validierung darf keine Umgebung oder Secrets verwenden"))
    if staging.get("if") != DISPATCH_MAIN or staging.get("needs") != ["validate"]:
        issues.append(_issue(workflow_path, "CFD004", "Staging muss manuell auf main und nach Validate laufen"))
    if staging.get("environment") != {"name": "cloudflare-watchdog-staging"}:
        issues.append(_issue(workflow_path, "CFD005", "Staging braucht geschützte feste Umgebung"))
    if production.get("if") != f"{DISPATCH_MAIN} && inputs.deploy_production":
        issues.append(_issue(workflow_path, "CFD004", "Production braucht manuelle main-Freigabe"))
    if production.get("needs") != ["validate", "deploy_staging"]:
        issues.append(_issue(workflow_path, "CFD005", "Production muss denselben Staging-Lauf abwarten"))
    if production.get("environment") != {"name": "cloudflare-watchdog-production"}:
        issues.append(_issue(workflow_path, "CFD005", "Production braucht geschützte feste Umgebung"))

    for name, job in jobs.items():
        for step in job.get("steps", []):
            if not isinstance(step, dict) or "uses" not in step:
                continue
            action = str(step["uses"]).split(" #", 1)[0]
            if not ACTION_SHA.fullmatch(action):
                issues.append(_issue(workflow_path, "CFD006", f"{name}: Action nicht mit Voll-SHA gepinnt: {action}"))
        install = _step(job, "Install locked Worker dependencies")
        if install is None or install.get("run") != "npm --prefix cloudflare/watchdog ci --ignore-scripts":
            issues.append(_issue(workflow_path, "CFD007", f"{name}: gelocktes npm ci fehlt"))

    workflow_text = workflow_path.read_text(encoding="utf-8")
    if re.search(r"\bnpx\s+wrangler\b|curl[^\n|]*(?:\||>)\s*(?:sh|bash)\b", workflow_text):
        issues.append(_issue(workflow_path, "CFD007", "ungepinnte Wrangler-/Installer-Ausführung verboten"))
    staging_deploy = _step(staging, "Deploy staging without cron")
    production_deploy = _step(production, "Deploy production with cron")
    if staging_deploy is None or "npm --prefix cloudflare/watchdog run deploy -- --env staging" not in str(staging_deploy.get("run", "")):
        issues.append(_issue(workflow_path, "CFD008", "Staging muss gelockten Wrangler mit --env staging verwenden"))
    production_run = str(production_deploy.get("run", "")) if production_deploy else ""
    if not production_run.startswith('npm --prefix cloudflare/watchdog run deploy -- --env="" --var'):
        issues.append(_issue(workflow_path, "CFD008", "Production muss kanonische Wrangler-Umgebung verwenden"))
    for label, job in (("staging", staging), ("production", production)):
        health = _step(job, "Verify deployed health and version") or {}
        health_run = str(health.get("run", ""))
        health_env = health.get("env", {})
        if not isinstance(health_env, dict) or "CLOUDFLARE_WATCHDOG_HEALTH_URL" not in str(health_env.get("WATCHDOG_HEALTH_URL", "")) or not all(token in health_run for token in ("--proto '=https'", "payload.version !== expected")):
            issues.append(_issue(workflow_path, "CFD009", f"{label}: exakte HTTPS-Health-/Versionsprüfung fehlt"))

    package_value, error = _load(package_path, json.loads)
    if error:
        issues.append(error)
    elif not isinstance(package_value, dict) or package_value.get("scripts", {}).get("deploy") != "wrangler deploy" or package_value.get("scripts", {}).get("deploy:dry-run") != 'wrangler deploy --dry-run --env=""' or package_value.get("devDependencies", {}).get("wrangler") != "4.118.0":
        issues.append(_issue(package_path, "CFD007", "Deployskript und Wrangler müssen exakt gelockt sein"))
    lock_value, error = _load(lock_path, json.loads)
    if error:
        issues.append(error)
    elif not isinstance(lock_value, dict) or lock_value.get("packages", {}).get("node_modules/wrangler", {}).get("version") != "4.118.0":
        issues.append(_issue(lock_path, "CFD007", "package-lock muss Wrangler 4.118.0 auflösen"))

    wrangler_value, error = _load(wrangler_path, json.loads)
    if error:
        issues.append(error)
    elif isinstance(wrangler_value, dict):
        production_crons = wrangler_value.get("triggers", {}).get("crons")
        staging_config = wrangler_value.get("env", {}).get("staging", {})
        staging_crons = staging_config.get("triggers", {}).get("crons") if isinstance(staging_config, dict) else None
        if production_crons != ["0 2 * * *"] or staging_crons != []:
            issues.append(_issue(wrangler_path, "CFD010", "nur Production darf täglichen Cron enthalten"))
        production_binding = wrangler_value.get("durable_objects", {}).get("bindings")
        staging_binding = staging_config.get("durable_objects", {}).get("bindings") if isinstance(staging_config, dict) else None
        if staging_binding != production_binding:
            issues.append(_issue(wrangler_path, "CFD010", "Staging braucht dieselbe feste DO-Bindungsform"))
        if wrangler_value.get("vars", {}).get("DEPLOYMENT_VERSION") != "local" or staging_config.get("vars", {}).get("DEPLOYMENT_VERSION") != "local":
            issues.append(_issue(wrangler_path, "CFD009", "Healthversion muss in beiden Umgebungen deklariert sein"))

    worker_value, error = _load(worker_path, lambda value: value)
    if error:
        issues.append(error)
    elif not all(token in str(worker_value) for token in ('request.method === "GET"', 'url.pathname === "/healthz"', "DEPLOYMENT_VERSION", "status: 404")):
        issues.append(_issue(worker_path, "CFD009", "read-only /healthz oder 404-Schreibgrenze fehlt"))
    runbook_value, error = _load(runbook_path, lambda value: value)
    if error:
        issues.append(error)
    elif not all(token in str(runbook_value) for token in ("GitHub-Gesamtausfall", "wrangler rollback", "cloudflare-watchdog-staging", "cloudflare-watchdog-production")):
        issues.append(_issue(runbook_path, "CFD011", "Betriebs-, Ausfall- oder Rollbackvertrag fehlt"))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    issues = validate_repository(args.root.resolve())
    if issues:
        for issue in issues:
            print(issue.render(), file=sys.stderr)
        return 1
    print("P09.4 Cloudflare config: ok; pr_dry_run=1; staging_before_production=1; health_version=1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
