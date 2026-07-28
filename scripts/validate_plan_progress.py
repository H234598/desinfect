#!/usr/bin/env python3
"""Validate implementation status, ordering, and evidence requirements."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VALID = {"offen", "in_arbeit", "im_review", "umgesetzt", "blockiert"}
SHA40 = re.compile(r"^[0-9a-f]{40}$")
REQUIREMENT_ID = re.compile(r"^(?:MUSS-[0-9]{2}|V2-[A-Z0-9-]+)$")


def load(path: str) -> dict[str, Any]:
    """Load a UTF-8 JSON document relative to the repository root."""
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def is_positive_int(value: object) -> bool:
    """Return true only for a real positive integer, excluding booleans."""
    return type(value) is int and value >= 1


def validate_completion_evidence(item: dict[str, Any], known_requirements: set[str]) -> None:
    """Require complete merge, CI, test, coverage, and acceptance evidence."""
    if not is_positive_int(item.get("pr_number")):
        raise ValueError(f"{item['id']}: umgesetzt benötigt eine positive PR-Nummer")
    evidence = item.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError(f"{item['id']}: umgesetzt benötigt ein Evidenzobjekt")
    merge_sha = evidence.get("merge_sha")
    if not isinstance(merge_sha, str) or not SHA40.fullmatch(merge_sha):
        raise ValueError(f"{item['id']}: gültiger Merge-SHA fehlt")
    for field in ("ci_runs", "tests", "requirement_ids"):
        values = evidence.get(field)
        if not isinstance(values, list) or not values or not all(
            isinstance(value, str) and value.strip() for value in values
        ):
            raise ValueError(f"{item['id']}: {field} muss eine nichtleere Stringliste sein")
    requirement_ids = evidence["requirement_ids"]
    if len(requirement_ids) != len(set(requirement_ids)):
        raise ValueError(f"{item['id']}: requirement_ids enthält Duplikate")
    invalid_ids = [value for value in requirement_ids if not REQUIREMENT_ID.fullmatch(value)]
    unknown_ids = [value for value in requirement_ids if value not in known_requirements]
    if invalid_ids:
        raise ValueError(f"{item['id']}: ungültige Anforderungs-IDs: {invalid_ids}")
    if unknown_ids:
        raise ValueError(f"{item['id']}: unbekannte Anforderungs-IDs: {unknown_ids}")
    for field in ("accepted_at", "accepted_by"):
        value = evidence.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{item['id']}: {field} fehlt")


def validate() -> None:
    """Validate work-package order, status transitions, and completion evidence."""
    data = load("docs/implementation-status.json")
    items = data["work_packages"]
    if len(items) != 60 or len({item["id"] for item in items}) != 60:
        raise ValueError("Es müssen 60 eindeutige Arbeitspakete vorliegen")
    steering = (ROOT / "docs/IMPLEMENTIERUNGSPLAN-STEUERUNG.md").read_text(encoding="utf-8")
    plan_ids = re.findall(r"^- \[[ xX]\] \*\*(P[0-9]{2}\.[0-9]+)\*\*", steering, re.M)
    status_ids = [item["id"] for item in items]
    if plan_ids != status_ids:
        raise ValueError("Steuerungsplan und Statusregister sind nicht identisch/geordnet")

    counts = Counter(item["status"] for item in items)
    if any(status not in VALID for status in counts):
        raise ValueError("Unbekannter Status")
    expected = {
        "total": 60,
        **{
            status: counts[status]
            for status in ("offen", "in_arbeit", "im_review", "umgesetzt", "blockiert")
        },
    }
    if data["summary"] != expected:
        raise ValueError("Zusammenfassung ist nicht synchron")

    requirements = load("docs/requirements/requirement-index.json")
    known_requirements = set(requirements["must_ids"]) | set(requirements["v2_ids"])
    for item in items:
        status = item["status"]
        if status == "in_arbeit" and not item.get("branch"):
            raise ValueError(f"{item['id']}: in_arbeit benötigt Branch")
        if status == "im_review" and (
            not item.get("branch") or not is_positive_int(item.get("pr_number"))
        ):
            raise ValueError(f"{item['id']}: im_review benötigt Branch und positive PR-Nummer")
        if status == "blockiert" and not item.get("blocker"):
            raise ValueError(f"{item['id']}: blockiert benötigt Begründung")
        if status == "umgesetzt":
            validate_completion_evidence(item, known_requirements)

    status_md = (ROOT / "docs/IMPLEMENTIERUNGSSTATUS.md").read_text(encoding="utf-8")
    for needle in ("ADR-003 = A", "ADR-014 = B"):
        if needle not in status_md:
            raise ValueError(f"Statusseite fehlt: {needle}")
    active_pr = data.get("active_pr")
    if active_pr is not None:
        number = active_pr.get("number")
        if not is_positive_int(number) or f"**PR: #{number}**" not in status_md:
            raise ValueError("Aktiver PR ist zwischen Maschinenstatus und Statusseite nicht synchron")


if __name__ == "__main__":
    validate()
    print("implementation progress: ok")
