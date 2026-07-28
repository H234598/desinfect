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


def main() -> None:
    """Run baseline, requirement, and progress validation in a fixed order."""
    validate_baseline()
    validate_requirements()
    validate_progress()
    print("all baseline validators: ok")


if __name__ == "__main__":
    main()
