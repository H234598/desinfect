"""Contracts for rights operations documentation and blocking CI gates."""

from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _workflow(name: str) -> tuple[Path, dict[str, object]]:
    path = ROOT / ".github" / "workflows" / name
    return path, yaml.safe_load(path.read_text(encoding="utf-8"))


def _triggers(workflow: dict[str, object]) -> dict[str, object]:
    return workflow.get("on", workflow.get(True, {}))


def test_rights_register_changes_trigger_and_block_both_workflows() -> None:
    _baseline_path, baseline = _workflow("p00-baseline.yml")
    _pipeline_path, pipeline = _workflow("rki-pipeline.yml")
    triggers = _triggers(baseline)

    assert "research/**" in triggers["pull_request"]["paths"]
    assert "research/**" in triggers["push"]["paths"]
    command = "python3 scripts/validate_rights_register.py"
    baseline_job = baseline["jobs"]["baseline"]
    pipeline_job = pipeline["jobs"]["pipeline"]
    baseline_step = next(
        step
        for step in baseline_job["steps"]
        if step["name"] == "Validate rights register"
    )
    pipeline_step = next(
        step
        for step in pipeline_job["steps"]
        if step["name"] == "Run one global blocking validation"
    )
    assert "continue-on-error" not in baseline_job
    assert "continue-on-error" not in pipeline_job
    for step in (baseline_step, pipeline_step):
        assert command in step["run"]
        assert "continue-on-error" not in step


def test_rights_docs_lock_authority_review_and_publication_contract() -> None:
    policy = (ROOT / "docs" / "Rechte-und-Lizenzen.md").read_text(encoding="utf-8")
    method = (ROOT / "docs" / "Methodik" / "Rechte.md").read_text(encoding="utf-8")
    text = policy + method

    for required in (
        "source_id",
        "source_sha256",
        "research/rights-register.yml",
        "Rohmetadaten",
        "keine Autorität",
        "decision_sha256",
        "keine Signatur",
        "approved",
        "internal_only",
        "metadata_only",
        "unknown",
        "takedown",
        "public",
        "repository_authorized",
        "internal",
        "restricted",
        "CODEOWNERS",
        "automatische Schreibpfade",
        "manuelle rechtliche Prüfung",
        "python3 scripts/validate_rights_register.py",
    ):
        assert required in text


def test_takedown_runbook_preserves_evidence_and_separates_lfs_history() -> None:
    runbook = (ROOT / "runbooks" / "RIGHTS-TAKEDOWN.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "takedown",
        "nächsten Site-Build",
        "RKI-Originallink",
        "Quellmetadaten",
        "Pages",
        "LFS-Historie",
        "nicht automatisch löschen",
        "Reconciliation",
        "Rollback",
        "python3 scripts/validate_rights_register.py",
    ):
        assert required in runbook


def test_storage_operations_doc_links_rights_policy_and_takedown() -> None:
    text = (ROOT / "docs" / "Wartung" / "RunModes-und-Storage.md").read_text(
        encoding="utf-8"
    )

    assert "../Rechte-und-Lizenzen.md" in text
    assert "../../runbooks/RIGHTS-TAKEDOWN.md" in text
    assert "python3 scripts/validate_rights_register.py" in text
