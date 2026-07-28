#!/usr/bin/env python3
"""Validate the offline P00 governance baseline."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"^[0-9a-f]{40}$")
LOCKED = {"ADR-003": "A", "ADR-014": "B"}


def load(path: str) -> dict:
    """Load a UTF-8 JSON document relative to the repository root."""
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def validate_revisions() -> None:
    """Validate frozen repository revisions, observed heads, and drift markers."""
    data = load("config/reference-revisions.json")
    repos = {entry["full_name"]: entry for entry in data["repositories"]}
    expected = {"H234598/desinfect", "H234598/ADHS-Lernpfad", "H234598/Cheatsheets"}
    if set(repos) != expected:
        raise ValueError("Revisionsregister enthält nicht exakt die drei geplanten Repositories")
    for name, entry in repos.items():
        if not SHA40.fullmatch(entry["frozen_sha"]) or not SHA40.fullmatch(entry["observed_head_sha"]):
            raise ValueError(f"{name}: vollständiger 40-stelliger SHA fehlt")
        changed = entry["frozen_sha"] != entry["observed_head_sha"]
        expected_status = "advanced_after_freeze" if changed else "unchanged"
        if entry["drift_status"] != expected_status:
            raise ValueError(f"{name}: drift_status widerspricht den SHAs")
        if not entry["manual_verification_required"]:
            raise ValueError(f"{name}: nicht lesbare Einstellungen wurden verschwiegen")
    if data["policy"]["locked_decisions"] != LOCKED:
        raise ValueError("Revisionspolicy muss ADR-003=A und ADR-014=B sperren")


def validate_decisions() -> None:
    """Validate the complete ADR register and the two locked choices."""
    data = load("config/architecture-decisions.json")
    decisions = {entry["id"]: entry for entry in data["decisions"]}
    if set(decisions) != {f"ADR-{number:03d}" for number in range(1, 16)}:
        raise ValueError("ADR-001 bis ADR-015 müssen vollständig registriert sein")
    if data["locked_decisions"] != LOCKED:
        raise ValueError("Gesperrte Entscheidungen weichen ab")
    for adr_id, entry in decisions.items():
        choice = entry["choice"]
        path = ROOT / "docs/adr" / f"{adr_id}.md"
        if not path.is_file():
            raise ValueError(f"{adr_id}: ADR-Datei fehlt")
        text = path.read_text(encoding="utf-8")
        if f"choice: {choice}" not in text or f"**Entscheidung:** {choice}." not in text:
            raise ValueError(f"{adr_id}: ADR-Datei widerspricht dem Register")
        if "Rückroll- oder Migrationsweg" not in text:
            raise ValueError(f"{adr_id}: Rückroll- oder Migrationsweg fehlt")
    for adr_id, choice in LOCKED.items():
        entry = decisions[adr_id]
        if entry["choice"] != choice or entry.get("locked") is not True:
            raise ValueError(f"{adr_id} muss auf {choice} gesperrt bleiben")


def validate_requirements_index() -> None:
    """Validate the complete ordered MUST and unique V2 identifier sets."""
    data = load("docs/requirements/requirement-index.json")
    must = data["must_ids"]
    v2 = data["v2_ids"]
    if must != [f"MUSS-{number:02d}" for number in range(1, 41)]:
        raise ValueError("MUSS-Index ist nicht lückenlos MUSS-01 bis MUSS-40")
    if len(v2) != 169 or len(set(v2)) != 169:
        raise ValueError("V2-Index muss 169 eindeutige IDs enthalten")


def validate_progress_count() -> None:
    """Validate work-package uniqueness and the derived status counters."""
    data = load("docs/implementation-status.json")
    items = data["work_packages"]
    if len(items) != 60 or len({item["id"] for item in items}) != 60:
        raise ValueError("Fortschrittsregister muss 60 eindeutige Arbeitspakete enthalten")
    counts = Counter(item["status"] for item in items)
    expected = {
        "total": 60,
        "offen": counts["offen"],
        "in_arbeit": counts["in_arbeit"],
        "im_review": counts["im_review"],
        "umgesetzt": counts["umgesetzt"],
        "blockiert": counts["blockiert"],
    }
    if data["summary"] != expected:
        raise ValueError("Fortschrittszusammenfassung ist nicht synchron")


def main() -> None:
    """Run every invariant that belongs to the compact baseline validator."""
    validate_revisions()
    validate_decisions()
    validate_requirements_index()
    validate_progress_count()
    print("baseline: ok; ADR-003=A; ADR-014=B; 40 MUST; 169 V2; 60 work packages")


if __name__ == "__main__":
    main()
