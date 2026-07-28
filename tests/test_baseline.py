"""Tests for the compact P00 baseline validator."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from scripts import validate_baseline

ROOT = Path(__file__).resolve().parents[1]


class BaselineTests(unittest.TestCase):
    """Verify the aggregate baseline and locked architecture choices."""

    def test_complete_baseline(self) -> None:
        """Run the complete compact baseline validation."""
        validate_baseline.main()

    def test_locked_decisions(self) -> None:
        """Keep ADR-003 on A and ADR-014 on B in the machine register."""
        data = json.loads(
            (ROOT / "config/architecture-decisions.json").read_text(encoding="utf-8")
        )
        self.assertEqual(data["locked_decisions"], {"ADR-003": "A", "ADR-014": "B"})
        choices = {item["id"]: item["choice"] for item in data["decisions"]}
        self.assertEqual(choices["ADR-003"], "A")
        self.assertEqual(choices["ADR-014"], "B")


if __name__ == "__main__":
    unittest.main()
