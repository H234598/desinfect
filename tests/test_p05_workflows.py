"""Repository-level contracts for P05 dispatcher, pipeline, and backfill workflows."""
from __future__ import annotations

from pathlib import Path
import re

import yaml

from scripts.validate_ci_mutation_safety import validate_repository

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def load(name: str):
    path = WORKFLOWS / name
    return path, yaml.safe_load(path.read_text(encoding="utf-8"))


def triggers(data):
    return data.get("on", data.get(True, {}))


def test_repository_has_exactly_one_schedule() -> None:
    scheduled: list[str] = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        if "schedule" in triggers(data):
            scheduled.append(path.name)
    assert scheduled == ["rki-dispatcher.yml"]


def test_dispatcher_is_read_only_and_calls_reusable_pipeline() -> None:
    path, data = load("rki-dispatcher.yml")
    assert set(triggers(data)) == {"schedule", "workflow_dispatch"}
    assert data["permissions"] == {"contents": "read"}
    pipeline = data["jobs"]["pipeline"]
    assert pipeline["uses"] == "./.github/workflows/rki-pipeline.yml"
    assert pipeline["secrets"] == {
        "WACHHUND_APP_PRIVATE_KEY": "${{ secrets.WACHHUND_APP_PRIVATE_KEY }}",
    }
    text = path.read_text(encoding="utf-8")
    assert "git commit" not in text
    assert "git push" not in text


def test_pipeline_owns_the_only_writer_concurrency_and_never_cancels() -> None:
    path, data = load("rki-pipeline.yml")
    assert set(triggers(data)) == {"workflow_call", "workflow_dispatch"}
    assert data["permissions"] == {"contents": "read"}
    assert triggers(data)["workflow_call"]["secrets"] == {
        "WACHHUND_APP_PRIVATE_KEY": {"required": True},
    }
    assert data["concurrency"] == {
        "group": "desinfect-repository-writer",
        "cancel-in-progress": False,
    }
    text = path.read_text(encoding="utf-8")
    assert "--force" not in text
    assert "persist-credentials: false" in text


def test_app_token_is_repository_scoped_and_created_after_validation() -> None:
    _path, data = load("rki-pipeline.yml")
    steps = data["jobs"]["pipeline"]["steps"]
    names = [step["name"] for step in steps]
    validation = names.index("Run one global blocking validation")
    token = names.index("Create repository-scoped Wachhund token")
    writer = names.index("Commit and push safely")
    assert validation < token < writer
    token_step = steps[token]
    assert re.fullmatch(r"actions/create-github-app-token@[0-9a-f]{40}", token_step["uses"].split(" #", 1)[0])
    assert token_step["with"]["owner"] == "H234598"
    assert token_step["with"]["repositories"] == "desinfect"
    assert token_step["with"]["permission-contents"] == "write"
    assert "GITHUB_TOKEN" not in str(steps[writer])


def test_writer_email_is_passed_via_environment() -> None:
    _path, data = load("rki-pipeline.yml")
    writer = next(
        step for step in data["jobs"]["pipeline"]["steps"]
        if step["name"] == "Commit and push safely"
    )
    assert writer["env"]["BOT_EMAIL"] == (
        "${{ vars.WACHHUND_BOT_EMAIL || "
        "'41898282+github-actions[bot]@users.noreply.github.com' }}"
    )
    assert "vars.WACHHUND_BOT_EMAIL" not in writer["run"]
    assert 'git config user.email "$BOT_EMAIL"' in writer["run"]


def test_backfill_is_manual_bounded_and_requires_literal_apply_confirmation() -> None:
    path, data = load("rki-backfill.yml")
    assert set(triggers(data)) == {"workflow_dispatch"}
    inputs = triggers(data)["workflow_dispatch"]["inputs"]
    assert {"from_year", "to_year", "max_tasks", "run_mode", "confirm_apply"} <= set(inputs)
    text = path.read_text(encoding="utf-8")
    assert '[[ "$CONFIRM_APPLY" != "APPLY" ]]' in text
    assert "schedule:" not in text
    pipeline = data["jobs"]["pipeline"]
    assert pipeline["uses"] == "./.github/workflows/rki-pipeline.yml"
    assert pipeline["secrets"] == {
        "WACHHUND_APP_PRIVATE_KEY": "${{ secrets.WACHHUND_APP_PRIVATE_KEY }}",
    }


def test_variant_b_validator_accepts_repository_workflows() -> None:
    assert validate_repository(ROOT) == []


def test_pipeline_always_renders_and_uploads_redacted_diagnostics() -> None:
    _path, data = load("rki-pipeline.yml")
    steps = data["jobs"]["pipeline"]["steps"]
    summary = next(step for step in steps if step["name"] == "Render redacted job summary")
    upload = next(step for step in steps if step["name"] == "Upload redacted transaction evidence")

    assert summary["id"] == "observability"
    assert summary["if"] == "always()"
    assert summary["continue-on-error"] is True
    assert "scripts.rki_pipeline.ci_summary" in summary["run"]
    assert "GITHUB_STEP_SUMMARY" in summary["run"]
    assert '--job-status "${{ job.status }}"' in summary["run"]
    assert steps.index(summary) < steps.index(upload)
    assert upload["if"] == "always()"
    assert upload["continue-on-error"] is True
    assert "build/pipeline/job-summary.md" in upload["with"]["path"]
    assert upload["with"]["retention-days"] == (
        "${{ steps.observability.outputs.retention_days || '90' }}"
    )


def test_incident_issue_uses_separate_minimal_app_token_when_enabled() -> None:
    _path, data = load("rki-pipeline.yml")
    steps = data["jobs"]["pipeline"]["steps"]
    token = next(
        step for step in steps
        if step["name"] == "Create repository-scoped incident issue token"
    )
    maintain = next(step for step in steps if step["name"] == "Maintain rolling incident issue")

    enabled = "always() && vars.ROLLING_ISSUE_ENABLED == 'true'"
    assert token["id"] == "incident-token"
    assert token["if"] == enabled
    assert re.fullmatch(
        r"actions/create-github-app-token@[0-9a-f]{40}",
        token["uses"].split(" #", 1)[0],
    )
    assert token["with"] == {
        "client-id": "${{ vars.WACHHUND_APP_CLIENT_ID }}",
        "private-key": "${{ secrets.WACHHUND_APP_PRIVATE_KEY }}",
        "owner": "H234598",
        "repositories": "desinfect",
        "permission-issues": "write",
    }
    assert maintain["if"] == enabled
    assert maintain["env"] == {
        "GH_TOKEN": "${{ steps.incident-token.outputs.token }}",
        "INCIDENT_FAILURE_THRESHOLD": (
            "${{ vars.INCIDENT_FAILURE_THRESHOLD || '2' }}"
        ),
    }
    assert "scripts.rki_pipeline.incident_issue" in maintain["run"]
    assert "--mode apply" in maintain["run"]
    assert "--status status.json" in maintain["run"]
    assert "--run-manifest build/pipeline/transaction-result.json" in maintain["run"]
    assert "if [[ -f build/pipeline/transaction-result.json ]]" in maintain["run"]
    assert '--job-status "${{ job.status }}"' in maintain["run"]
