#!/usr/bin/env python3
"""Validate automatic-write policy, CODEOWNERS coverage, and optionally the index."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.write_policy import load_policy, validate_index  # noqa: E402

CRITICAL_OWNER_PATHS = {
    "/.github/",
    "/cloudflare/",
    "/config/",
    "/scripts/",
    "/schemas/",
    "/tests/",
    "/web/",
    "/.gitattributes",
    "/mkdocs.yml",
    "/pyproject.toml",
    "/requirements*.in",
    "/requirements*.txt",
    "/package*.json",
    "/SECURITY.md",
    "/research/rights-register.yml",
    "/research/taxonomy*.yml",
}


def _parse_codeowners(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Parse non-comment CODEOWNERS rules without substring shortcuts."""

    rules: list[tuple[str, tuple[str, ...]]] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2 or not all(owner.startswith("@") for owner in parts[1:]):
            raise ValueError(f"Ungültige CODEOWNERS-Zeile {number}: {raw}")
        rules.append((parts[0], tuple(parts[1:])))
    return rules


def validate(*, check_index: bool = False) -> None:
    """Validate policy syntax, exact owner coverage, and optionally the index."""

    load_policy()
    owners_text = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    rules = _parse_codeowners(owners_text)
    patterns = {pattern for pattern, _owners in rules}
    missing = sorted(CRITICAL_OWNER_PATHS - patterns)
    if missing:
        raise ValueError(
            "CODEOWNERS deckt geschützte Pfade nicht explizit ab: "
            f"{missing}"
        )
    if not any(
        pattern == "*" and "@H234598" in rule_owners
        for pattern, rule_owners in rules
    ):
        raise ValueError(
            "@H234598 muss initialer globaler CODEOWNER in einer exakten *-Regel bleiben"
        )
    if check_index:
        validate_index(ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-index", action="store_true")
    args = parser.parse_args()
    validate(check_index=args.check_index)
    print("automatic write policy: ok; deny-first; CODEOWNER @H234598")
