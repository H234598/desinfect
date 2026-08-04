#!/usr/bin/env python3
"""Plan and apply one repository-scoped rolling pipeline incident issue."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable
from urllib.request import Request, urlopen

from scripts.rki_pipeline.io_utils import stable_json_dumps
from scripts.rki_pipeline.runtime_status import (
    RuntimeStatusError,
    project_public_status,
    redact_text,
)
from scripts.rki_pipeline.schema_registry import SchemaContractError, validate_document


MARKER = "<!-- desinfect:rki-pipeline-incident:v1 -->"
REPOSITORY = "H234598/desinfect"
LABEL = "pipeline-incident"
TITLE_PREFIX = "[RKI-Pipeline] Wiederholter Fehler"
DEFAULT_THRESHOLD = 2
MIN_THRESHOLD = 2
MAX_THRESHOLD = 100
GITHUB_HOST = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
HTTP_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 5
MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_BODY_CHARS = 20_000
MAX_COMMENT_CHARS = 2_000

_ISSUES_PATH = f"/repos/{REPOSITORY}/issues"
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_MARKDOWN = re.compile(r"([\\`*_{}\[\]()#+.!|-])")
Transport = Callable[..., Any]


class IncidentIssueError(RuntimeError):
    """Incident planning or bounded GitHub access failed safely."""


@dataclass(frozen=True, slots=True)
class IssueMatch:
    """One normalized GitHub issue containing the stable incident marker."""

    number: int
    state: str

    def __post_init__(self) -> None:
        if type(self.number) is not int or self.number < 1:
            raise IncidentIssueError("Issue-Nummer ist ungültig")
        if self.state not in {"open", "closed"}:
            raise IncidentIssueError("Issue-Zustand ist ungültig")


@dataclass(frozen=True, slots=True)
class IncidentIssuePlan:
    """Immutable decision with all public text needed for one issue action."""

    action: str
    repository: str
    labels: tuple[str, ...]
    title: str | None
    body: str | None
    comment: str | None
    issue_number: int | None

    def __post_init__(self) -> None:
        _validate_incident_plan(self)

    def to_dict(self) -> dict[str, object]:
        return {
            "action": self.action,
            "body": self.body,
            "comment": self.comment,
            "issue_number": self.issue_number,
            "labels": list(self.labels),
            "repository": self.repository,
            "title": self.title,
        }


def _validate_incident_plan(plan: IncidentIssuePlan) -> None:
    if not isinstance(plan, IncidentIssuePlan):
        raise IncidentIssueError("Incident-Plan besitzt den falschen Typ")
    if plan.action not in {"create", "update", "reopen", "heal", "noop"}:
        raise IncidentIssueError("Incident-Plan enthält unbekannte Aktion")
    if plan.repository != REPOSITORY or plan.labels != (LABEL,):
        raise IncidentIssueError("Incident-Plan verletzt feste Repository-Felder")

    issue_number_valid = (
        type(plan.issue_number) is int and plan.issue_number > 0
    )
    if plan.action in {"update", "reopen", "heal"} and not issue_number_valid:
        raise IncidentIssueError("Incident-Plan enthält keine gültige Issue-Nummer")
    if plan.action == "create" and plan.issue_number is not None:
        raise IncidentIssueError("Incident-Plan für create darf keine Issue-Nummer haben")
    if plan.action == "noop" and plan.issue_number is not None and not issue_number_valid:
        raise IncidentIssueError("Incident-Plan enthält keine gültige Issue-Nummer")

    if plan.action in {"create", "update", "reopen"}:
        if (
            plan.title != TITLE_PREFIX
            or not isinstance(plan.body, str)
            or not 1 <= len(plan.body) <= MAX_BODY_CHARS
            or plan.body.count(MARKER) != 1
            or plan.comment is not None
        ):
            raise IncidentIssueError("Incident-Plan enthält ungültigen Issue-Inhalt")
        return
    if plan.action == "heal":
        if (
            plan.title is not None
            or plan.body is not None
            or not isinstance(plan.comment, str)
            or not 1 <= len(plan.comment) <= MAX_COMMENT_CHARS
        ):
            raise IncidentIssueError("Incident-Plan enthält ungültigen Heilungskommentar")
        return
    if plan.title is not None or plan.body is not None or plan.comment is not None:
        raise IncidentIssueError("Incident-Plan für noop enthält unerlaubten Inhalt")


def _threshold(value: object) -> int:
    if type(value) is not int or not MIN_THRESHOLD <= value <= MAX_THRESHOLD:
        raise IncidentIssueError(
            f"threshold muss zwischen {MIN_THRESHOLD} und {MAX_THRESHOLD} liegen"
        )
    return value


def _validated_status(status: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(status, dict):
        raise IncidentIssueError("Status muss ein JSON-Objekt sein")
    try:
        validate_document("status", status)
    except SchemaContractError as exc:
        raise IncidentIssueError("Status verletzt den öffentlichen Vertrag") from exc
    return status


def _markdown_text(value: object, *, limit: int = 600) -> str:
    text, _changed = redact_text(value, limit=limit)
    text = _ANSI.sub("", text).replace(MARKER, "[REDACTED-MARKER]")
    text = " ".join(
        "".join(character if character.isprintable() else " " for character in text).split()
    )
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN.sub(r"\\\1", text) or "nicht gemeldet"


def _incident_body(status: dict[str, Any]) -> str:
    pipeline = status["pipeline"]
    error = pipeline["last_error"] or {}
    return "\n".join(
        (
            MARKER,
            "# RKI-Pipeline-Incident",
            "",
            f"- Repository: `{REPOSITORY}`",
            f"- Öffentlicher Status: `{_markdown_text(status['status'])}`",
            f"- Aufeinanderfolgende Fehler: `{pipeline['consecutive_failures']}`",
            f"- Aktualisiert: `{_markdown_text(status['updated_at'])}`",
            f"- Fehlercode: `{_markdown_text(error.get('code'))}`",
            f"- Phase: `{_markdown_text(error.get('phase'))}`",
            f"- Fehlerzeit: `{_markdown_text(error.get('occurred_at'))}`",
            "",
            "## Letzter redigierter Fehler",
            "",
            _markdown_text(error.get("message")),
            "",
            "## Sichere Wiederherstellung",
            "",
            "1. Laufartefakte und feste Pipeline-Phasen prüfen.",
            "2. Ursache beheben und denselben Lauf kontrolliert wiederholen.",
            "3. Issue erst nach öffentlichem Status `operational` schließen.",
        )
    )


def _healing_comment(status: dict[str, Any]) -> str:
    return "\n".join(
        (
            "Pipelinezustand wiederhergestellt.",
            "",
            f"- Status: `{_markdown_text(status['status'])}`",
            f"- Aktualisiert: `{_markdown_text(status['updated_at'])}`",
        )
    )


def plan_incident_issue(
    status: dict[str, Any],
    matches: tuple[IssueMatch, ...] | list[IssueMatch],
    *,
    threshold: object = DEFAULT_THRESHOLD,
) -> IncidentIssuePlan:
    """Return one deterministic create/update/reopen/heal/no-op decision."""

    current = _validated_status(status)
    failure_threshold = _threshold(threshold)
    normalized = tuple(matches)
    if any(not isinstance(match, IssueMatch) for match in normalized):
        raise IncidentIssueError("Marker-Treffer sind nicht normalisiert")
    if len(normalized) > 1:
        raise IncidentIssueError("Mehr als ein Incident-Marker-Treffer; Abbruch")
    match = normalized[0] if normalized else None
    failures = current["pipeline"]["consecutive_failures"]

    if failures >= failure_threshold:
        action = "create" if match is None else ("update" if match.state == "open" else "reopen")
        return IncidentIssuePlan(
            action=action,
            repository=REPOSITORY,
            labels=(LABEL,),
            title=TITLE_PREFIX,
            body=_incident_body(current),
            comment=None,
            issue_number=None if match is None else match.number,
        )

    if match is not None and match.state == "open" and current["status"] == "operational" and failures == 0:
        return IncidentIssuePlan(
            action="heal",
            repository=REPOSITORY,
            labels=(LABEL,),
            title=None,
            body=None,
            comment=_healing_comment(current),
            issue_number=match.number,
        )

    return IncidentIssuePlan(
        action="noop",
        repository=REPOSITORY,
        labels=(LABEL,),
        title=None,
        body=None,
        comment=None,
        issue_number=None if match is None else match.number,
    )


class GitHubRestClient:
    """Bounded GitHub Issues REST adapter for the fixed repository."""

    def __init__(self, token: str, *, transport: Transport = urlopen) -> None:
        if not isinstance(token, str) or not token.strip():
            raise IncidentIssueError("GitHub-Token fehlt")
        self._token = token
        self._transport = transport

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> object:
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "desinfect-rki-pipeline",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if payload is not None:
            data = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{GITHUB_HOST}{path}", data=data, headers=headers, method=method)
        try:
            with self._transport(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = getattr(response, "status", 200)
        except Exception as exc:
            raise IncidentIssueError("GitHub-Anfrage fehlgeschlagen") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise IncidentIssueError("GitHub-Antwortgröße überschreitet Grenze")
        if not 200 <= status < 300:
            raise IncidentIssueError("GitHub-Anfrage lieferte keinen Erfolg")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IncidentIssueError("GitHub-Antwort ist kein gültiges JSON") from exc

    def list_issue_matches(self) -> tuple[IssueMatch, ...]:
        matches: list[IssueMatch] = []
        marker_hits = 0
        for page_number in range(1, MAX_PAGES + 1):
            path = (
                f"{_ISSUES_PATH}?state=all&labels={LABEL}"
                f"&per_page={PAGE_SIZE}&page={page_number}"
            )
            page = self._request("GET", path)
            if not isinstance(page, list) or len(page) > PAGE_SIZE:
                raise IncidentIssueError("GitHub-Issue-Liste ist ungültig")
            for issue in page:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                body = issue.get("body")
                occurrences = body.count(MARKER) if isinstance(body, str) else 0
                marker_hits += occurrences
                if occurrences:
                    matches.append(
                        IssueMatch(number=issue.get("number"), state=issue.get("state"))
                    )
                if marker_hits > 1:
                    raise IncidentIssueError("Mehr als ein Incident-Marker-Treffer; Abbruch")
            if len(page) < PAGE_SIZE:
                return tuple(matches)
        raise IncidentIssueError("GitHub-Issue-Seitengrenze erreicht")

    def apply(self, plan: IncidentIssuePlan, *, status: dict[str, Any]) -> None:
        _validate_incident_plan(plan)
        current = _validated_status(status)
        expected_body = (
            _incident_body(current)
            if plan.action in {"create", "update", "reopen"}
            else None
        )
        expected_comment = _healing_comment(current) if plan.action == "heal" else None
        if plan.body != expected_body or plan.comment != expected_comment:
            raise IncidentIssueError("Incident-Plan stimmt nicht mit Status überein")
        if plan.action == "noop":
            return
        if plan.action == "create":
            self._request(
                "POST",
                _ISSUES_PATH,
                {"body": expected_body, "labels": [LABEL], "title": TITLE_PREFIX},
            )
            return
        if plan.issue_number is None:
            raise IncidentIssueError("Incident-Plan enthält keine Issue-Nummer")
        issue_path = f"{_ISSUES_PATH}/{plan.issue_number}"
        if plan.action == "update":
            self._request(
                "PATCH",
                issue_path,
                {"body": expected_body, "labels": [LABEL], "title": TITLE_PREFIX},
            )
            return
        if plan.action == "reopen":
            self._request(
                "PATCH",
                issue_path,
                {
                    "body": expected_body,
                    "labels": [LABEL],
                    "state": "open",
                    "title": TITLE_PREFIX,
                },
            )
            return
        if plan.action == "heal":
            self._request("POST", f"{issue_path}/comments", {"body": expected_comment})
            self._request("PATCH", issue_path, {"state": "closed"})
            return
        raise IncidentIssueError("Incident-Plan enthält unbekannte Aktion")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise IncidentIssueError("Eingabedatei ist nicht lesbar") from exc
    if len(raw) > MAX_INPUT_BYTES:
        raise IncidentIssueError("Eingabedatei überschreitet Größenlimit")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IncidentIssueError("Eingabedatei enthält kein gültiges JSON") from exc
    if not isinstance(value, dict):
        raise IncidentIssueError("JSON-Wurzel muss ein Objekt sein")
    return value


def _run_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    manifest = payload.get("run_manifest", payload)
    if not isinstance(manifest, dict):
        raise IncidentIssueError("run_manifest muss ein JSON-Objekt sein")
    return manifest


def _synthetic_job_failure(
    status: dict[str, Any],
    manifest: dict[str, Any] | None,
    job_status: str,
) -> dict[str, Any]:
    current = _validated_status(status)
    if manifest is not None:
        try:
            validate_document("run-manifest", manifest)
        except SchemaContractError as exc:
            raise IncidentIssueError("Run-Manifest verletzt den Vertrag") from exc
    occurred_at = (
        (manifest.get("ended_at") or manifest.get("updated_at"))
        if manifest is not None
        else current["updated_at"]
    )
    code = "ci_job_failure" if job_status == "failure" else "ci_job_cancelled"
    message = (
        "GitHub Actions job reported failure."
        if job_status == "failure"
        else "GitHub Actions job was cancelled."
    )
    result = deepcopy(current)
    result["updated_at"] = occurred_at
    result["status"] = "degraded"
    result["pipeline"]["consecutive_failures"] += 1
    result["pipeline"]["last_error"] = {
        "code": code,
        "phase": "complete",
        "occurred_at": occurred_at,
        "message": message,
        "redacted": True,
    }
    return _validated_status(result)


def _decision_status(
    status: dict[str, Any],
    manifest: dict[str, Any] | None,
    job_status: str | None,
) -> dict[str, Any]:
    if job_status in {"failure", "cancelled"}:
        return _synthetic_job_failure(status, manifest, job_status)
    if manifest is not None:
        try:
            return project_public_status(status, manifest, content_changed=False)
        except (RuntimeStatusError, SchemaContractError, ValueError) as exc:
            raise IncidentIssueError("Run-Manifest kann nicht projiziert werden") from exc
    return _validated_status(status)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "apply"), required=True)
    parser.add_argument("--status", type=Path, required=True)
    parser.add_argument("--run-manifest", type=Path)
    parser.add_argument("--job-status", choices=("success", "failure", "cancelled"))
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    return parser


def main(
    argv: list[str] | None = None,
    *,
    transport: Transport | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        status = _load_json(args.status)
        manifest = (
            None
            if args.run_manifest is None
            else _run_manifest(_load_json(args.run_manifest))
        )
        current = _decision_status(status, manifest, args.job_status)
        if args.mode == "plan":
            plan = plan_incident_issue(current, (), threshold=args.threshold)
        else:
            token = os.environ.get("GH_TOKEN")
            if not token:
                raise IncidentIssueError("GH_TOKEN fehlt für apply")
            client = (
                GitHubRestClient(token)
                if transport is None
                else GitHubRestClient(token, transport=transport)
            )
            matches = client.list_issue_matches()
            plan = plan_incident_issue(current, matches, threshold=args.threshold)
            client.apply(plan, status=current)
        print(stable_json_dumps(plan.to_dict()), end="")
        return 0
    except IncidentIssueError as exc:
        print(f"incident_issue: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
