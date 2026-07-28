#!/usr/bin/env python3
"""Validate implementation status, ordering and evidence requirements."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VALID = {"offen", "in_arbeit", "im_review", "umgesetzt", "blockiert"}
SHA40 = re.compile(r"^[0-9a-f]{40}$")


def load(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def validate() -> None:
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
    expected = {"total": 60, **{status: counts[status] for status in ("offen", "in_arbeit", "im_review", "umgesetzt", "blockiert")}}
    if data["summary"] != expected:
        raise ValueError("Zusammenfassung ist nicht synchron")

    for item in items:
        status = item["status"]
        if status == "in_arbeit" and not item.get("branch"):
            raise ValueError(f"{item['id']}: in_arbeit benötigt Branch")
        if status == "im_review" and (not item.get("branch") or not isinstance(item.get("pr_number"), int)):
            raise ValueError(f"{item['id']}: im_review benötigt Branch und PR")
        if status == "blockiert" and not item.get("blocker"):
            raise ValueError(f"{item['id']}: blockiert benötigt Begründung")
        if status == "umgesetzt":
            evidence = item.get("evidence") or {}
            required = [evidence.get("merge_sha"), evidence.get("ci_runs"), evidence.get("tests"), evidence.get("accepted_at"), evidence.get("accepted_by")]
            if any(value in (None, "", []) for value in required) or not SHA40.fullmatch(evidence["merge_sha"]):
                raise ValueError(f"{item['id']}: umgesetzt ohne vollständige Merge-/CI-/Abnahmeevidenz")

    status_md = (ROOT / "docs/IMPLEMENTIERUNGSSTATUS.md").read_text(encoding="utf-8")
    for needle in ("ADR-003 = A", "ADR-014 = B"):
        if needle not in status_md:
            raise ValueError(f"Statusseite fehlt: {needle}")
    active_pr = data.get("active_pr")
    if active_pr is not None:
        number = active_pr.get("number")
        if not isinstance(number, int) or f"**PR: #{number}**" not in status_md:
            raise ValueError("Aktiver PR ist zwischen Maschinenstatus und Statusseite nicht synchron")


if __name__ == "__main__":
    validate()
    print("implementation progress: ok")
