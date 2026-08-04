#!/usr/bin/env python3
"""Render schema-validated pipeline diagnostics as safe fixed Markdown."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Sequence
import unicodedata

from scripts.rki_pipeline.io_utils import atomic_write_text, read_bounded_json_object
from scripts.rki_pipeline.runtime_status import redact_text
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document


MISSING = "nicht gemeldet"
JOB_STATUSES = {"success", "failure", "cancelled"}
MAX_INPUT_BYTES = 2 * 1024 * 1024
_RETENTION_DAYS = {
    "success": 14,
    "no_op": 14,
    "recovered": 14,
    "failed": 30,
    "blocked": 90,
}
_METRIC_FIELDS = (
    "due_task_count",
    "executed_task_count",
    "checked_rki_entry_count",
    "new_pdf_count",
    "changed_pdf_count",
    "markdown_conversion_count",
    "ocr_case_count",
    "archive_created_count",
    "archive_unchanged_count",
    "lfs_new_bytes",
    "lfs_largest_new_file_bytes",
    "lfs_fetch_bytes",
    "lfs_push_bytes",
    "rights_case_count",
)
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*\[\]()<>#!|&_~])")
_VALUE_LIMIT = 500


def _validate_job_status(job_status: str | None) -> None:
    if job_status is not None and job_status not in JOB_STATUSES:
        raise ValueError("job_status muss success, failure oder cancelled sein")


def retention_days(run_status: str, *, job_status: str | None = None) -> int:
    """Return state-derived diagnostic retention without rewriting run status."""

    _validate_job_status(job_status)
    try:
        manifest_retention = _RETENTION_DAYS[run_status]
    except KeyError as exc:
        raise ValueError(f"Kein finaler Laufstatus: {run_status!r}") from exc
    if run_status == "blocked":
        return 90
    if job_status in {"failure", "cancelled"}:
        return 30
    return manifest_retention


def _fold_and_escape(value: object) -> str:
    if value is None or value == "":
        raw = MISSING
    elif type(value) is bool:
        raw = "ja" if value else "nein"
    else:
        raw = str(value)
    raw = _ANSI_ESCAPE.sub("", raw)
    raw = raw.replace("\r\n", " ").replace("\r", " ").replace("\n", " ").replace("\t", " ")
    raw = "".join(
        character for character in raw if unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
    )
    raw = " ".join(raw.split()) or MISSING
    redacted, _changed = redact_text(raw, limit=_VALUE_LIMIT)
    redacted = " ".join(redacted.split()) or MISSING
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", redacted)


def _table(first_heading: str, rows: Sequence[tuple[str, object]]) -> list[str]:
    lines = [f"| {first_heading} | Wert |", "| --- | --- |"]
    lines.extend(f"| {label} | {_fold_and_escape(value)} |" for label, value in rows)
    return lines


def _manifest_and_commit(payload: dict[str, Any]) -> tuple[dict[str, Any], bool | None]:
    if not isinstance(payload, dict):
        raise SchemaContractError("ci-summary: JSON-Wurzel muss ein Objekt sein")
    if "run_manifest" not in payload:
        manifest = payload
        commit_required = None
    else:
        manifest = payload["run_manifest"]
        commit_required = payload.get("commit_required")
        if "commit_required" in payload and type(commit_required) is not bool:
            raise SchemaContractError("ci-summary: commit_required muss boolesch sein")
    if not isinstance(manifest, dict):
        raise SchemaContractError("ci-summary: run_manifest muss ein Objekt sein")
    validate_document("run-manifest", manifest)
    return manifest, commit_required


def render_summary(
    payload: dict[str, Any],
    *,
    public_status: dict[str, Any] | None = None,
    job_status: str | None = None,
) -> str:
    """Validate inputs and return deterministic, bounded Markdown."""

    _validate_job_status(job_status)
    manifest, commit_required = _manifest_and_commit(payload)
    days = retention_days(manifest["status"], job_status=job_status)
    if public_status is not None:
        if not isinstance(public_status, dict):
            raise SchemaContractError("ci-summary: status muss ein Objekt sein")
        validate_document("status", public_status)

    context = manifest["context"]
    metrics = manifest["metrics"]
    tasks = ", ".join(sorted(manifest["tasks"])) or None
    periods = public_status["periods"] if public_status is not None else {}
    corpus = public_status["corpus"] if public_status is not None else {}
    error = manifest["error"] or {}
    recovery = manifest["recovery"] or {}

    lines = ["# RKI-Pipeline", "", "## Lauf"]
    lines.extend(
        _table(
            "Feld",
            (
                ("Auslöser", context["trigger_source"]),
                ("Modus", context["run_mode"]),
                ("Workflowstatus", job_status),
                ("Transaktionsstatus", manifest["status"]),
                ("Phase", manifest["phase"]),
                ("Aufgaben", tasks),
                ("Commitentscheidung", commit_required),
                ("Aufbewahrung", f"{days} Tage"),
            ),
        )
    )
    lines.extend(["", "## Metriken"])
    lines.extend(_table("Metrik", tuple((field, metrics.get(field)) for field in _METRIC_FIELDS)))
    lines.extend(["", "## Datenstand"])
    lines.extend(
        _table(
            "Feld",
            (
                ("Letzte vollständige Woche", periods.get("last_completed_week")),
                ("Letzter vollständiger Monat", periods.get("last_completed_month")),
                ("Letztes vollständiges Jahr", periods.get("last_completed_year")),
                ("Letzte Reconciliation", periods.get("last_reconciliation_at")),
                ("Letzter Recovery-Drill", periods.get("last_recovery_drill_year")),
                ("Inventar vollständig bis", corpus.get("inventory_complete_through_year")),
                (
                    "Analysekorpus vollständig bis",
                    corpus.get("analysis_corpus_complete_through_year"),
                ),
                (
                    "Öffentlicher Spiegel vollständig bis",
                    corpus.get("public_mirror_complete_through_year"),
                ),
                ("Taxonomie-Gate", corpus.get("taxonomy_gate_satisfied")),
                ("Taxonomiestatus", corpus.get("taxonomy_state")),
            ),
        )
    )
    lines.extend(["", "## Fehler und Recovery"])
    lines.extend(
        _table(
            "Feld",
            (
                ("Fehlerklasse", error.get("class")),
                ("Fehlercode", error.get("code")),
                ("Fehlermeldung", error.get("message")),
                ("Wiederholbar", error.get("retryable")),
                ("Recovery-Level", recovery.get("level")),
                ("Fortsetzungsphase", recovery.get("resume_phase")),
                ("Folgelauf blockieren", recovery.get("block_next_run")),
                ("Recovery bestätigt", recovery.get("acknowledged")),
                ("Nächste sichere Aktion", recovery.get("action")),
            ),
        )
    )
    return "\n".join(lines) + "\n"


def _load_json(path: Path) -> dict[str, Any]:
    return read_bounded_json_object(path, max_bytes=MAX_INPUT_BYTES)


def _append_github_output(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("GITHUB_OUTPUT ist keine reguläre Datei")
        payload = text.encode("utf-8")
        while payload:
            written = os.write(descriptor, payload)
            payload = payload[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validierte RKI-Pipeline-Zusammenfassung")
    parser.add_argument("input", type=Path, help="Run-Manifest oder Transaktionsumschlag")
    parser.add_argument("--status", type=Path, help="Optionaler öffentlicher Status")
    parser.add_argument("--job-status", choices=sorted(JOB_STATUSES))
    parser.add_argument("--output", type=Path, help="Markdown-Zieldatei statt stdout")
    parser.add_argument("--github-output", type=Path, help="GITHUB_OUTPUT-Datei")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _load_json(args.input)
        public_status = _load_json(args.status) if args.status is not None else None
        rendered = render_summary(
            payload,
            public_status=public_status,
            job_status=args.job_status,
        )
        manifest = payload.get("run_manifest", payload)
        days = retention_days(manifest["status"], job_status=args.job_status)
        if args.output is None:
            print(rendered, end="")
        else:
            atomic_write_text(args.output, rendered, allowed_root=args.output.parent)
        if args.github_output is not None:
            _append_github_output(args.github_output, f"retention_days={days}\n")
        return 0
    except (ValueError, TypeError):
        print("ci-summary: ungültige Eingabe", file=sys.stderr)
        return 2
    except OSError:
        print("ci-summary: Ein-/Ausgabefehler", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
