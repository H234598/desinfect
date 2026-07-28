
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
    "/.github/", "/cloudflare/", "/config/", "/scripts/", "/schemas/", "/tests/", "/web/",
    "/.gitattributes", "/mkdocs.yml", "/pyproject.toml", "/SECURITY.md",
    "/research/rights-register.yml", "/research/taxonomy*.yml",
}


def validate(*, check_index: bool = False) -> None:
    load_policy()
    owners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
    lines = {line.split()[0] for line in owners.splitlines() if line.strip() and not line.startswith("#")}
    missing = sorted(CRITICAL_OWNER_PATHS - lines)
    if missing:
        raise ValueError(f"CODEOWNERS deckt geschützte Pfade nicht explizit ab: {missing}")
    if "* @H234598" not in owners:
        raise ValueError("@H234598 muss initialer globaler CODEOWNER bleiben")
    if check_index:
        validate_index(ROOT)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-index", action="store_true")
    args = parser.parse_args()
    validate(check_index=args.check_index)
    print("automatic write policy: ok; deny-first; CODEOWNER @H234598")
