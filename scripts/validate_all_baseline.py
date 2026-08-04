#!/usr/bin/env python3
"""Run every P00 baseline validator without external dependencies."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_baseline import main as validate_baseline  # noqa: E402
from scripts.validate_plan_progress import validate as validate_progress  # noqa: E402
from scripts.validate_requirements import validate as validate_requirements  # noqa: E402

REQUIRED_P07_RECONCILIATION_PATHS = {
    "scripts/rki_pipeline/reconciliation.py",
    "tests/test_reconciliation.py",
    "tests/test_reconciliation_cli.py",
    "tests/fixtures/reconciliation/fixture.json",
    "docs/Wartung/Reconciliation.md",
    "runbooks/RKI-SOURCE-CHANGED.md",
}


def validate_p07_reconciliation_paths() -> None:
    """Require the complete P07.3 operator and offline-test surface."""
    missing = sorted(
        relative for relative in REQUIRED_P07_RECONCILIATION_PATHS if not (ROOT / relative).is_file()
    )
    if missing:
        raise ValueError(f"P07.3-Reconciliation-Dateien fehlen: {missing}")


def main() -> None:
    """Run baseline, requirement, and progress validation in a fixed order."""
    validate_baseline()
    validate_requirements()
    validate_progress()
    validate_p07_reconciliation_paths()
    print("all baseline validators: ok")


if __name__ == "__main__":
    main()
