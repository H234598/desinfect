#!/usr/bin/env python3
"""Validate the small, offline, provenance-tracked fixture corpus."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.io_utils import (
    detect_path_collisions,
    normalize_posix_path,
    sha256_file,
)

FIXTURE_ROOT = ROOT / "tests" / "fixtures"
MANIFEST = FIXTURE_ROOT / "manifest.json"
ALLOWED_METADATA_FILES = {"README.md", "manifest.json"}
MAX_FIXTURE_BYTES = 65_536
SECRET_PATTERNS = (
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    """Load a UTF-8 fixture manifest."""

    return json.loads(path.read_text(encoding="utf-8"))


def validate(root: Path = FIXTURE_ROOT, manifest_path: Path = MANIFEST) -> None:
    """Validate paths, hashes, sizes, provenance, and absence of extras/secrets."""

    data = load_manifest(manifest_path)
    if data.get("schema_version") != "1.0.0":
        raise ValueError("Fixture-Manifest besitzt eine unbekannte schema_version")
    maximum = data.get("max_file_bytes")
    if type(maximum) is not int or maximum <= 0 or maximum > MAX_FIXTURE_BYTES:
        raise ValueError(
            f"max_file_bytes muss zwischen 1 und {MAX_FIXTURE_BYTES} liegen"
        )
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Fixture-Manifest benötigt mindestens einen Eintrag")

    declared: set[str] = set()
    normalized_paths: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Fixture-Eintrag muss ein Objekt sein")
        relative = normalize_posix_path(entry.get("path", ""))
        if relative in declared:
            raise ValueError(f"Doppelte Fixture: {relative}")
        declared.add(relative)
        normalized_paths.append(relative)
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Fixture fehlt oder ist ein Symlink: {relative}")
        size = path.stat().st_size
        if size > maximum:
            raise ValueError(f"Fixture überschreitet {maximum} Bytes: {relative}")
        if entry.get("bytes") != size:
            raise ValueError(f"Fixture-Größe driftet: {relative}")
        digest = sha256_file(path)
        if entry.get("sha256") != digest:
            raise ValueError(f"Fixture-Hash driftet: {relative}")
        for field in ("media_type", "provenance", "purpose", "license_status"):
            value = entry.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Fixture {relative}: Pflichtfeld {field} fehlt")
        payload = path.read_bytes()
        if any(pattern.search(payload) for pattern in SECRET_PATTERNS):
            raise ValueError(f"Fixture enthält ein Secretmuster: {relative}")

    detect_path_collisions(normalized_paths)

    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Symlink im Fixturebaum ist unzulässig: {path.relative_to(root)}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            if relative not in ALLOWED_METADATA_FILES:
                actual.add(relative)
    extras = sorted(actual - declared)
    missing = sorted(declared - actual)
    if extras or missing:
        raise ValueError(f"Fixture-Manifest driftet; extras={extras}; missing={missing}")


def main() -> None:
    """Run fixture validation from the command line."""

    validate()
    print("fixtures: ok; offline; max 65536 bytes; no symlinks or unregistered files")


if __name__ == "__main__":
    main()
