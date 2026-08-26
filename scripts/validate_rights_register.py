#!/usr/bin/env python3
"""Validate reviewed rights policy and authoritative source decisions offline."""
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rki_pipeline.rights import (  # noqa: E402
    RightsState,
    load_rights_policy,
    load_rights_register,
)


def validate() -> None:
    """Parse both reviewed inputs and reassert their fail-closed defaults."""

    policy = load_rights_policy()
    register = load_rights_register()
    if policy.default_state is not RightsState.METADATA_ONLY:
        raise ValueError("Rechtepolicy ist nicht fail-closed")
    keys = [
        (
            entry.approval_key.source_id,
            entry.approval_key.canonical_url,
            entry.approval_key.version_or_bitstream,
            entry.approval_key.source_sha256,
        )
        for entry in register.entries
    ]
    if keys != sorted(keys) or len(keys) != len(set(keys)):
        raise ValueError("Rights-Register ist nicht kanonisch und eindeutig")


def main() -> None:
    """Run validation and emit one stable summary line."""

    validate()
    print("rights register: ok; exact revision+action authority; default metadata_only")


if __name__ == "__main__":
    main()
