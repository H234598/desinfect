from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.rki_pipeline.runtime_status import (
    InvalidTransition,
    RevisionConflict,
    new_run,
    project_public_status,
    redact_text,
    update_run,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-07-28T07:00:00Z"
LATER = "2026-07-28T07:05:00Z"


def initial_status() -> dict:
    return json.loads((ROOT / "status.json").read_text(encoding="utf-8"))


def running_run() -> dict:
    created = new_run(
        workflow="manual", trigger_source="test", run_mode="apply", storage_backend="lfs",
        tasks=["weekly_ingest"], run_id="test-1", now=NOW,
    )
    return update_run(
        created, expected_revision=1, status="running", phase="validate", now=NOW,
        completed_phases=["initialize", "plan", "materialize"],
    )


def test_revision_conflict_and_invalid_transition_are_blocked() -> None:
    created = new_run(
        workflow="manual", trigger_source="test", run_mode="plan", storage_backend="lfs",
        run_id="test-2", now=NOW,
    )
    with pytest.raises(RevisionConflict):
        update_run(created, expected_revision=2, status="running", phase="plan", now=NOW)
    with pytest.raises(InvalidTransition):
        update_run(created, expected_revision=1, status="success", phase="complete", now=NOW)


@pytest.mark.parametrize("trigger_source", ["schedule", "workflow_dispatch"])
def test_new_run_preserves_dispatch_trigger_and_canonical_task_ids(trigger_source: str) -> None:
    run = new_run(
        workflow="rki-dispatcher", trigger_source=trigger_source, run_mode="apply",
        storage_backend="lfs", run_id=f"dispatch-{trigger_source}", now=NOW,
        tasks=[
            "week:2026-W30",
            "month:2026-07",
            "year:2025",
            "reconciliation:2026-07-31T12:00:00Z",
        ],
    )

    assert run["context"]["trigger_source"] == trigger_source
    assert run["tasks"] == [
        "month:2026-07",
        "reconciliation:2026-07-31T12:00:00Z",
        "week:2026-W30",
        "year:2025",
    ]


def test_failed_run_does_not_change_success_clocks_and_redacts_secret() -> None:
    current = initial_status()
    current["pipeline"]["last_successful_run_at"] = "2026-07-20T04:31:12Z"
    current["pipeline"]["last_successful_write_at"] = "2026-07-19T04:31:12Z"
    run = running_run()
    failed = update_run(
        run, expected_revision=2, status="failed", phase="validate", now=LATER,
        completed_phases=run["completed_phases"],
        error={
            "class": "validation", "code": "validation.failed",
            "message": "token=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 and person@example.org",
            "retryable": False,
        },
        recovery={
            "level": "manual_intervention", "action": "Prüfe secret=very-secret-value",
            "resume_phase": "validate", "block_next_run": True,
        },
    )
    projected = project_public_status(current, failed, content_changed=False)
    assert projected["pipeline"]["last_successful_run_at"] == "2026-07-20T04:31:12Z"
    assert projected["pipeline"]["last_successful_write_at"] == "2026-07-19T04:31:12Z"
    assert "ghp_" not in failed["error"]["message"]
    assert "example.org" not in failed["error"]["message"]
    assert "very-secret-value" not in failed["recovery"]["action"]
    assert projected["status"] == "degraded"


def test_successful_write_updates_run_and_write_clocks_only_after_final_success() -> None:
    run = running_run()
    success = update_run(
        run, expected_revision=2, status="success", phase="verify", now=LATER,
        completed_phases=[*run["completed_phases"], "validate", "apply", "verify"],
    )
    projected = project_public_status(
        initial_status(), success, content_changed=True,
        last_main_commit_at="2026-07-28T07:06:00Z",
    )
    assert projected["pipeline"]["last_successful_run_at"] == LATER
    assert projected["pipeline"]["last_successful_write_at"] == LATER
    assert projected["pipeline"]["last_main_commit_at"] == "2026-07-28T07:06:00Z"
    assert projected["status"] == "operational"


def test_redact_text_strips_signed_url_query() -> None:
    text, changed = redact_text("see https://example.org/path?sig=secret#x")
    assert changed
    assert text == "see https://example.org/path"


def test_sanitize_url_rejects_password_without_username() -> None:
    text, changed = redact_text("see http://:secret@example.org/path")
    assert changed
    assert text == "see [REDACTED-URL]"
