#!/usr/bin/env python3
"""Validate rule-based traceability for all 40 MUST and 169 V2 IDs."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKED = {"ADR-003": "A", "ADR-014": "B"}


def load(path: str) -> dict:
    """Load a UTF-8 JSON document relative to the repository root."""
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def validate_rule(rule: dict, label: str) -> None:
    """Validate the common phase, path, test, and acceptance rule fields."""
    if not re.fullmatch(r"P[0-9]{2}", rule.get("phase", "")):
        raise ValueError(f"{label}: ungültige Phase")
    if not rule.get("target_paths") or not all(isinstance(path, str) and path for path in rule["target_paths"]):
        raise ValueError(f"{label}: Zielpfad fehlt")
    if not rule.get("blocking_test", "").strip():
        raise ValueError(f"{label}: blockierender Test fehlt")
    if not rule.get("acceptance", "").strip():
        raise ValueError(f"{label}: Abnahme fehlt")


def validate() -> None:
    """Resolve every registered requirement to exactly one complete rule."""
    index = load("docs/requirements/requirement-index.json")
    must = load("docs/requirements/must-register.json")
    v2 = load("docs/requirements/v2-register.json")
    expected = [f"MUSS-{number:02d}" for number in range(1, 41)]
    if index["must_ids"] != expected:
        raise ValueError("MUSS-Index ist nicht lückenlos")
    if len(index["v2_ids"]) != 169 or len(set(index["v2_ids"])) != 169:
        raise ValueError("V2-Index muss 169 eindeutige IDs enthalten")
    if set(must.get("labels", {})) != set(expected):
        raise ValueError("MUSS-Kurztexte sind nicht vollständig")
    for register in (must, v2):
        if register.get("source_plan_sha256") != index["source_plan_sha256"]:
            raise ValueError("Planfingerabdruck driftet")
        if register.get("locked_decisions") != LOCKED:
            raise ValueError("ADR-003=A und ADR-014=B müssen gesperrt bleiben")

    covered: list[str] = []
    for rule in must["rules"]:
        validate_rule(rule, rule.get("rule_id", "MUSS-Regel"))
        covered.extend(rule.get("ids", []))
    if Counter(covered) != Counter(expected):
        raise ValueError("Jede MUSS-ID muss genau einer Regel zugeordnet sein")

    covered = []
    for rule in v2["rules"]:
        prefix = rule.get("prefix", "")
        validate_rule(rule, prefix or "V2-Regel")
        matches = [item for item in index["v2_ids"] if item.startswith(prefix + "-")]
        if not matches:
            raise ValueError(f"{prefix}: Regel deckt keine V2-ID ab")
        covered.extend(matches)
    if Counter(covered) != Counter(index["v2_ids"]):
        raise ValueError("Jede V2-ID muss genau einer Präfixregel zugeordnet sein")


if __name__ == "__main__":
    validate()
    print("requirements: ok (40 MUST, 169 V2, ADR-003=A, ADR-014=B)")
