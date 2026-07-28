#!/usr/bin/env python3
"""Strict run lifecycle, recovery, public-status projection, and redaction."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from scripts.rki_pipeline.io_utils import atomic_write_text, stable_json_dumps
from scripts.rki_pipeline.schema_registry import validate_document

RUN_STATUSES = {
    "created", "running", "success", "no_op", "blocked", "failed", "recovering", "recovered"
}
FINAL_STATUSES = {"success", "no_op", "blocked", "failed", "recovered"}
PHASES = {"initialize", "plan", "materialize", "validate", "apply", "verify", "complete"}
ALLOWED_TRANSITIONS = {
    "created": {"created", "running", "blocked", "failed"},
    "running": {"running", "success", "no_op", "blocked", "failed", "recovering"},
    "success": {"success"},
    "no_op": {"no_op"},
    "blocked": {"blocked", "recovering", "failed"},
    "failed": {"failed", "recovering"},
    "recovering": {"recovering", "recovered", "blocked", "failed"},
    "recovered": {"recovered", "running", "success", "no_op", "blocked", "failed"},
}
_TOKEN_PATTERNS = (
    re.compile(r"(?i)\b(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*bearer\s+)\S+"),
    re.compile(r"(?i)\b(token|password|passwd|secret|api[_-]?key|access[_-]?key)\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b"),
)
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CODE = re.compile(r"^[A-Za-z0-9_.-]{1,120}$")


class RuntimeStatusError(RuntimeError):
    """Base class for strict lifecycle errors."""


class InvalidTransition(RuntimeStatusError):
    """The requested lifecycle transition is not allowed."""


class RevisionConflict(RuntimeStatusError):
    """A stale writer attempted to update a newer run revision."""


def utc_now() -> str:
    """Return an RFC 3339 UTC timestamp with millisecond precision."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("UTC-Zeitstempel mit Z erforderlich")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    return parsed.astimezone(timezone.utc)


def _duration(start: str, end: str) -> float:
    return round(max(0.0, (_parse_utc(end) - _parse_utc(start)).total_seconds()), 3)


def sanitize_url(value: str) -> str:
    """Drop credentials, query strings, and fragments from diagnostic URLs."""

    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        return "[REDACTED-URL]"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def redact_text(value: Any, *, limit: int = 2000) -> tuple[str, bool]:
    """Redact credential URLs, tokens, assignments, and e-mail addresses."""

    text = str(value or "").replace("\x00", "")[: max(limit * 2, limit)]
    changed = False

    # Sanitize complete URLs before generic e-mail/secret patterns can make
    # their authority component syntactically invalid.
    def replace_url(match: re.Match[str]) -> str:
        nonlocal changed
        raw = match.group(0)
        stripped = raw.rstrip(".,;)")
        try:
            sanitized = sanitize_url(stripped)
        except ValueError:
            sanitized = "[REDACTED-URL]"
        if sanitized != stripped:
            changed = True
        return sanitized

    text = re.sub(r"https?://[^\s<>]+", replace_url, text)
    for pattern in _TOKEN_PATTERNS:
        replacement = (
            r"\1[REDACTED]"
            if pattern.groups >= 1 and "bearer" in pattern.pattern.lower()
            else "[REDACTED]"
        )
        updated, count = pattern.subn(replacement, text)
        if count:
            changed = True
            text = updated
    return text[:limit], changed


def _context(
    *, trigger_source: str, run_mode: str, storage_backend: str,
    branch: str | None = None, commit_sha: str | None = None, pr_number: int | None = None,
) -> dict[str, Any]:
    return {
        "repository": "H234598/desinfect",
        "branch": branch,
        "commit_sha": commit_sha,
        "pr_number": pr_number,
        "trigger_source": trigger_source,
        "run_mode": run_mode,
        "storage_backend": storage_backend,
    }


def new_run(
    *, workflow: str, trigger_source: str, run_mode: str, storage_backend: str,
    tasks: Iterable[str] = (), run_id: str, now: str | None = None,
    branch: str | None = None, commit_sha: str | None = None, pr_number: int | None = None,
) -> dict[str, Any]:
    """Create and validate a new revision-one run manifest."""

    if not _RUN_ID.fullmatch(run_id):
        raise ValueError("run_id enthält unportable Zeichen")
    timestamp = now or utc_now()
    result = {
        "schema_version": "1.0.0",
        "run_id": run_id,
        "workflow": workflow,
        "revision": 1,
        "previous_status": None,
        "status": "created",
        "phase": "initialize",
        "completed_phases": [],
        "created_at": timestamp,
        "updated_at": timestamp,
        "ended_at": None,
        "duration_seconds": None,
        "context": _context(
            trigger_source=trigger_source,
            run_mode=run_mode,
            storage_backend=storage_backend,
            branch=branch,
            commit_sha=commit_sha,
            pr_number=pr_number,
        ),
        "tasks": sorted(set(tasks)),
        "metrics": {},
        "artifacts": [],
        "error": None,
        "recovery": None,
    }
    validate_document("run-manifest", result)
    return result


def _redacted_error(error: dict[str, Any] | None, *, phase: str, now: str) -> dict[str, Any] | None:
    if error is None:
        return None
    code = str(error.get("code", "unknown"))
    if not _CODE.fullmatch(code):
        raise ValueError("Fehlercode ist nicht portabel")
    message, _changed = redact_text(error.get("message", "Unbekannter Fehler"))
    return {
        "class": error.get("class", "unknown"),
        "code": code,
        "message": message or "[REDACTED]",
        "phase": phase,
        "occurred_at": now,
        "retryable": bool(error.get("retryable", False)),
        "redacted": True,
    }


def _redacted_recovery(recovery: dict[str, Any] | None) -> dict[str, Any] | None:
    if recovery is None:
        return None
    action, _changed = redact_text(recovery.get("action", "Manuelle Prüfung erforderlich"), limit=1000)
    return {
        "level": recovery.get("level", "manual_intervention"),
        "action": action or "[REDACTED]",
        "resume_phase": recovery.get("resume_phase"),
        "block_next_run": bool(recovery.get("block_next_run", True)),
        "acknowledged": bool(recovery.get("acknowledged", False)),
    }


def update_run(
    manifest: dict[str, Any], *, expected_revision: int, status: str, phase: str,
    now: str | None = None, completed_phases: Iterable[str] | None = None,
    metrics: dict[str, Any] | None = None, artifacts: list[dict[str, Any]] | None = None,
    error: dict[str, Any] | None = None, recovery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Advance a run using optimistic revision and a strict state machine."""

    validate_document("run-manifest", manifest)
    if manifest["revision"] != expected_revision:
        raise RevisionConflict(
            f"Erwartete Revision {expected_revision}, vorgefunden {manifest['revision']}"
        )
    if status not in RUN_STATUSES or phase not in PHASES:
        raise InvalidTransition("Unbekannter Status oder unbekannte Phase")
    previous = manifest["status"]
    if status not in ALLOWED_TRANSITIONS[previous]:
        raise InvalidTransition(f"Übergang {previous} → {status} ist unzulässig")

    timestamp = now or utc_now()
    result = deepcopy(manifest)
    result["revision"] += 1
    result["previous_status"] = previous
    result["status"] = status
    result["phase"] = phase
    result["updated_at"] = timestamp
    if completed_phases is not None:
        phase_values = list(dict.fromkeys(completed_phases))
        if any(value not in PHASES for value in phase_values):
            raise InvalidTransition("completed_phases enthält unbekannte Phase")
        result["completed_phases"] = phase_values
    if metrics is not None:
        result["metrics"] = deepcopy(metrics)
    if artifacts is not None:
        result["artifacts"] = deepcopy(artifacts)
    result["error"] = _redacted_error(error, phase=phase, now=timestamp)
    result["recovery"] = _redacted_recovery(recovery)
    if status in FINAL_STATUSES:
        result["ended_at"] = timestamp
        result["duration_seconds"] = _duration(result["created_at"], timestamp)
        result["phase"] = "complete"
        if "complete" not in result["completed_phases"]:
            result["completed_phases"] = [*result["completed_phases"], "complete"]
    else:
        result["ended_at"] = None
        result["duration_seconds"] = None
    validate_document("run-manifest", result)
    return result


def project_public_status(
    current: dict[str, Any], run: dict[str, Any], *, content_changed: bool,
    last_main_commit_at: str | None = None,
) -> dict[str, Any]:
    """Project one validated run without ever falsifying the three clocks."""

    validate_document("status", current)
    validate_document("run-manifest", run)
    if run["status"] not in FINAL_STATUSES:
        raise RuntimeStatusError("Nur ein finaler Lauf darf öffentlich projiziert werden")
    result = deepcopy(current)
    ended = run["ended_at"] or run["updated_at"]
    result["updated_at"] = ended
    result["runtime"] = {
        "storage_backend": run["context"]["storage_backend"],
        "last_run_mode": run["context"]["run_mode"],
    }
    if last_main_commit_at is not None:
        _parse_utc(last_main_commit_at)
        result["pipeline"]["last_main_commit_at"] = last_main_commit_at

    if run["status"] in {"success", "no_op", "recovered"}:
        result["pipeline"]["last_successful_run_at"] = ended
        if content_changed and run["status"] in {"success", "recovered"}:
            result["pipeline"]["last_successful_write_at"] = ended
        result["pipeline"]["consecutive_failures"] = 0
        result["pipeline"]["last_error"] = None
        result["status"] = "operational"
    else:
        result["pipeline"]["consecutive_failures"] += 1
        error = run.get("error")
        result["pipeline"]["last_error"] = None if error is None else {
            "code": error["code"],
            "phase": error["phase"],
            "occurred_at": error["occurred_at"],
            "message": error["message"][:500],
            "redacted": True,
        }
        result["status"] = "blocked" if run["status"] == "blocked" else "degraded"

    # ADR-014=B: analysis readiness and public mirror completeness remain separate.
    validate_document("status", result)
    return result


def write_validated_json(path: Path, name: str, payload: dict[str, Any]) -> None:
    """Validate and atomically write one JSON document."""

    validate_document(name, payload)
    atomic_write_text(path, stable_json_dumps(payload), allowed_root=path.parent)


def load_json(path: Path) -> dict[str, Any]:
    """Load a UTF-8 JSON object."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeStatusError(f"JSON-Wurzel muss ein Objekt sein: {path}")
    return value
