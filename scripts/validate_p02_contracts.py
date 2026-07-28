#!/usr/bin/env python3
"""Run every P02 data-contract, status, and write-boundary validator."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_schemas import validate as validate_schemas  # noqa: E402
from scripts.validate_status import validate as validate_status  # noqa: E402
from scripts.validate_write_policy import validate as validate_write_policy  # noqa: E402


def main() -> None:
    validate_schemas()
    validate_status()
    validate_write_policy()
    print("P02 contracts: ok; ADR-003=A; ADR-014=B")


if __name__ == "__main__":
    main()
