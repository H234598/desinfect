#!/usr/bin/env python3
"""Run every repository-only P01 foundation validator."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_dependency_locks import (  # noqa: E402
    validate_node_lock,
    validate_python_locks,
)
from scripts.validate_fixture_manifest import validate as validate_fixtures  # noqa: E402


def main() -> None:
    """Validate package locks and the complete offline fixture manifest."""

    validate_python_locks()
    validate_node_lock()
    validate_fixtures()
    print("P01 foundation: ok; ADR-003=A; ADR-014=B")


if __name__ == "__main__":
    main()
