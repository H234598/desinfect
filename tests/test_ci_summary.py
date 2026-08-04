"""Safe, deterministic CI-summary contract tests."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts.rki_pipeline.ci_summary import main, render_summary, retention_days
from scripts.rki_pipeline.schema_registry import SchemaContractError


ROOT = Path(__file__).resolve().parents[1]
FAILURE_FIXTURE = ROOT / "tests" / "fixtures" / "status" / "failure.json"
MAX_TEST_INPUT_BYTES = 2 * 1024 * 1024


def _success_manifest() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "run_id": "summary-success",
        "workflow": "rki-pipeline",
        "revision": 3,
        "previous_status": "running",
        "status": "success",
        "phase": "complete",
        "completed_phases": ["initialize", "plan", "materialize", "validate", "complete"],
        "created_at": "2026-08-04T10:00:00Z",
        "updated_at": "2026-08-04T10:02:00Z",
        "ended_at": "2026-08-04T10:02:00Z",
        "duration_seconds": 120.0,
        "context": {
            "repository": "H234598/desinfect",
            "branch": "main",
            "commit_sha": "a" * 40,
            "pr_number": None,
            "trigger_source": "github-schedule",
            "run_mode": "apply",
            "storage_backend": "lfs",
        },
        "tasks": ["weekly_ingest", "archive_refresh"],
        "metrics": {
            "due_task_count": 3,
            "executed_task_count": 2,
            "checked_rki_entry_count": 41,
            "new_pdf_count": 4,
            "changed_pdf_count": 0,
            "markdown_conversion_count": 5,
            "ocr_case_count": 2,
            "archive_created_count": 3,
            "archive_unchanged_count": 1,
            "lfs_new_bytes": 1_048_576,
            "lfs_largest_new_file_bytes": 524_288,
            "lfs_fetch_bytes": 2_048,
            "lfs_push_bytes": 1_048_576,
            "rights_case_count": 5,
            "ignored_internal_count": 999,
        },
        "artifacts": [],
        "error": None,
        "recovery": None,
    }


def _public_status() -> dict[str, object]:
    value = json.loads((ROOT / "status.json").read_text(encoding="utf-8"))
    value["periods"] = {
        "last_completed_week": "2026-W31",
        "last_completed_month": "2026-07",
        "last_completed_year": 2025,
        "last_reconciliation_at": "2026-08-03T08:00:00Z",
        "last_recovery_drill_year": 2026,
    }
    value["corpus"] = {
        "inventory_complete_through_year": 2025,
        "analysis_corpus_complete_through_year": 2024,
        "public_mirror_complete_through_year": None,
        "taxonomy_gate_satisfied": True,
        "taxonomy_state": "approved",
    }
    return value


def test_success_summary_is_fixed_deterministic_and_never_invents_missing_metrics() -> None:
    envelope = {"commit_required": True, "run_manifest": _success_manifest()}

    rendered = render_summary(envelope, public_status=_public_status(), job_status="success")

    assert (
        rendered
        == """# RKI-Pipeline

## Lauf
| Feld | Wert |
| --- | --- |
| Auslöser | github-schedule |
| Modus | apply |
| Workflowstatus | success |
| Transaktionsstatus | success |
| Phase | complete |
| Aufgaben | archive\\_refresh, weekly\\_ingest |
| Commitentscheidung | ja |
| Aufbewahrung | 14 Tage |

## Metriken
| Metrik | Wert |
| --- | --- |
| due_task_count | 3 |
| executed_task_count | 2 |
| checked_rki_entry_count | 41 |
| new_pdf_count | 4 |
| changed_pdf_count | 0 |
| markdown_conversion_count | 5 |
| ocr_case_count | 2 |
| archive_created_count | 3 |
| archive_unchanged_count | 1 |
| lfs_new_bytes | 1048576 |
| lfs_largest_new_file_bytes | 524288 |
| lfs_fetch_bytes | 2048 |
| lfs_push_bytes | 1048576 |
| rights_case_count | 5 |

## Datenstand
| Feld | Wert |
| --- | --- |
| Letzte vollständige Woche | 2026-W31 |
| Letzter vollständiger Monat | 2026-07 |
| Letztes vollständiges Jahr | 2025 |
| Letzte Reconciliation | 2026-08-03T08:00:00Z |
| Letzter Recovery-Drill | 2026 |
| Inventar vollständig bis | 2025 |
| Analysekorpus vollständig bis | 2024 |
| Öffentlicher Spiegel vollständig bis | nicht gemeldet |
| Taxonomie-Gate | ja |
| Taxonomiestatus | approved |

## Fehler und Recovery
| Feld | Wert |
| --- | --- |
| Fehlerklasse | nicht gemeldet |
| Fehlercode | nicht gemeldet |
| Fehlermeldung | nicht gemeldet |
| Wiederholbar | nicht gemeldet |
| Recovery-Level | nicht gemeldet |
| Fortsetzungsphase | nicht gemeldet |
| Folgelauf blockieren | nicht gemeldet |
| Recovery bestätigt | nicht gemeldet |
| Nächste sichere Aktion | nicht gemeldet |
"""
    )
    assert (
        render_summary(envelope, public_status=_public_status(), job_status="success") == rendered
    )
    assert "ignored_internal_count" not in rendered

    missing_metric = deepcopy(envelope)
    del missing_metric["run_manifest"]["metrics"]["lfs_fetch_bytes"]
    assert "| lfs_fetch_bytes | nicht gemeldet |" in render_summary(missing_metric)


def test_summary_validates_inputs_and_accepts_direct_manifest() -> None:
    assert "| Commitentscheidung | nicht gemeldet |" in render_summary(_success_manifest())

    invalid_manifest = _success_manifest()
    invalid_manifest["status"] = "invented"
    with pytest.raises(SchemaContractError):
        render_summary(invalid_manifest)

    invalid_status = _public_status()
    invalid_status["corpus"]["taxonomy_state"] = "invented"
    with pytest.raises(SchemaContractError):
        render_summary(_success_manifest(), public_status=invalid_status)

    with pytest.raises(ValueError, match="job_status"):
        render_summary(_success_manifest(), job_status="unknown")


def test_summary_redacts_strips_folds_bounds_and_markdown_escapes_every_value() -> None:
    manifest = _success_manifest()
    dangerous = (
        "\x1b[31mghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 operator@example.org |\n"
        "## Angriff [Link](target) _kursiv_ ~~durch~~ &lt;tag&gt;\x08"
    )
    manifest["metrics"]["checked_rki_entry_count"] = dangerous + "A" * 5_000
    manifest["error"] = {
        "class": "validation",
        "code": "validation.failed",
        "message": dangerous,
        "phase": "validate",
        "occurred_at": "2026-08-04T10:02:00Z",
        "retryable": False,
        "redacted": True,
    }
    manifest["recovery"] = {
        "level": "manual_intervention",
        "action": "secret=do-not-leak\n[erneut](target)",
        "resume_phase": "validate",
        "block_next_run": True,
        "acknowledged": False,
    }

    rendered = render_summary(manifest, job_status="failure")

    assert "\x1b" not in rendered
    assert "ghp_" not in rendered
    assert "operator@example.org" not in rendered
    assert "do-not-leak" not in rendered
    assert "\n## Angriff" not in rendered
    assert r"\| \#\# Angriff \[Link\]\(target\)" in rendered
    assert r"\_kursiv\_ \~\~durch\~\~ \&lt;tag\&gt;" in rendered
    assert r"\[erneut\]\(target\)" in rendered
    assert "A" * 501 not in rendered
    assert "| Workflowstatus | failure |" in rendered
    assert "| Transaktionsstatus | success |" in rendered


@pytest.mark.parametrize(
    ("run_status", "job_status", "expected"),
    [
        ("success", None, 14),
        ("no_op", "success", 14),
        ("recovered", "success", 14),
        ("failed", "success", 30),
        ("success", "failure", 30),
        ("success", "cancelled", 30),
        ("blocked", "success", 90),
        ("blocked", "failure", 90),
    ],
)
def test_retention_uses_manifest_and_separate_workflow_conclusion(
    run_status: str,
    job_status: str | None,
    expected: int,
) -> None:
    assert retention_days(run_status, job_status=job_status) == expected


def test_cli_renders_failure_fixture_appends_retention_and_keeps_validation_success(
    tmp_path: Path,
) -> None:
    output = tmp_path / "summary.md"
    github_output = tmp_path / "github-output"
    github_output.write_text("transaction=kept\n", encoding="utf-8")

    result = main(
        [
            str(FAILURE_FIXTURE),
            "--status",
            str(ROOT / "status.json"),
            "--job-status",
            "failure",
            "--output",
            str(output),
            "--github-output",
            str(github_output),
        ]
    )

    assert result == 0
    assert "| Workflowstatus | failure |" in output.read_text(encoding="utf-8")
    assert "| Transaktionsstatus | failed |" in output.read_text(encoding="utf-8")
    assert github_output.read_text(encoding="utf-8") == "transaction=kept\nretention_days=30\n"


def test_cli_rejects_invalid_schema_without_writing_outputs(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"run_manifest": {"status": "failed"}}), encoding="utf-8")
    output = tmp_path / "summary.md"
    github_output = tmp_path / "github-output"

    assert main([str(invalid), "--output", str(output), "--github-output", str(github_output)]) == 2
    assert not output.exists()
    assert not github_output.exists()


def _unsafe_json_path(
    tmp_path: Path,
    *,
    kind: str,
    valid_payload: dict[str, object],
) -> Path:
    path = tmp_path / f"unsafe-{kind}.json"
    if kind == "symlink":
        target = tmp_path / "symlink-target.json"
        target.write_text(json.dumps(valid_payload), encoding="utf-8")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "oversize":
        raw = json.dumps(valid_payload).encode("utf-8")
        path.write_bytes(raw + b" " * (MAX_TEST_INPUT_BYTES + 1 - len(raw)))
    elif kind == "invalid_utf8":
        path.write_bytes(b"\xff")
    elif kind == "duplicate_keys":
        path.write_bytes(b'{"value": 1, "value": 2}')
    elif kind == "nonfinite":
        path.write_bytes(b'{"value": NaN}')
    else:  # pragma: no cover - test table owns the closed set
        raise AssertionError(kind)
    return path


@pytest.mark.parametrize("location", ["input", "status"])
@pytest.mark.parametrize(
    "kind",
    ["symlink", "directory", "oversize", "invalid_utf8", "duplicate_keys", "nonfinite"],
)
def test_cli_rejects_unsafe_json_files_without_touching_outputs(
    tmp_path: Path,
    location: str,
    kind: str,
) -> None:
    valid_input = tmp_path / "valid-input.json"
    valid_input.write_text(json.dumps({"run_manifest": _success_manifest()}), encoding="utf-8")
    valid_status = tmp_path / "valid-status.json"
    valid_status.write_text(json.dumps(_public_status()), encoding="utf-8")
    unsafe = _unsafe_json_path(
        tmp_path,
        kind=kind,
        valid_payload=(
            {"run_manifest": _success_manifest()} if location == "input" else _public_status()
        ),
    )
    input_path = unsafe if location == "input" else valid_input
    status_path = unsafe if location == "status" else valid_status
    output = tmp_path / "summary.md"
    github_output = tmp_path / "github-output"
    output.write_bytes(b"bestehende summary\n")
    github_output.write_bytes(b"existing=value\n")

    result = main(
        [
            str(input_path),
            "--status",
            str(status_path),
            "--output",
            str(output),
            "--github-output",
            str(github_output),
        ]
    )

    assert result == 2
    assert output.read_bytes() == b"bestehende summary\n"
    assert github_output.read_bytes() == b"existing=value\n"


def test_failure_fixture_is_schema_valid_and_contains_recovery() -> None:
    payload = json.loads(FAILURE_FIXTURE.read_text(encoding="utf-8"))
    rendered = render_summary(deepcopy(payload))

    assert "| Fehlerklasse | validation |" in rendered
    assert "| Nächste sichere Aktion | Validierungsbericht prüfen |" in rendered
    assert "| Aufbewahrung | 30 Tage |" in rendered
