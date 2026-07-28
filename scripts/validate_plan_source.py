#!/usr/bin/env python3
"""Validate the frozen source-plan identity and canonical execution-control bytes."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCKED = {"ADR-003": "A", "ADR-014": "B"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_plan_source() -> str:
    """Return the frozen source SHA after validating provenance and control drift."""
    manifest = json.loads((ROOT / "config/plan-source.json").read_text(encoding="utf-8"))
    source_sha = manifest.get("source_sha256")
    if manifest.get("schema_version") != "1.0.0":
        raise ValueError("Unbekannte Planquellenmanifest-Version")
    if not isinstance(source_sha, str) or not SHA256.fullmatch(source_sha):
        raise ValueError("Ungültiger SHA-256 der Planlangfassung")
    if type(manifest.get("source_size")) is not int or manifest["source_size"] <= 0:
        raise ValueError("Ungültige Größe der Planlangfassung")
    if manifest.get("locked_decisions") != LOCKED:
        raise ValueError("Planquellenmanifest muss ADR-003=A und ADR-014=B sperren")

    relative = manifest.get("canonical_control_path")
    if not isinstance(relative, str) or not relative:
        raise ValueError("Kanonischer Steuerungspfad fehlt")
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
        raise ValueError("Kanonische Steuerungsdatei fehlt oder verlässt die Repositorygrenze")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != manifest.get("canonical_control_sha256"):
        raise ValueError("Kanonische Steuerungsdatei driftet")
    if b"P00.1" not in payload or b"P18.3" not in payload:
        raise ValueError("Kanonische Steuerungsdatei enthält nicht die vollständige Paketspanne")
    return source_sha


if __name__ == "__main__":
    print(f"plan source: ok ({validate_plan_source()})")
