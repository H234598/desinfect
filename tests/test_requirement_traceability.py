"""Tests for plan provenance and rule-based requirement traceability."""
from __future__ import annotations

import unittest

from scripts.validate_plan_source import validate_plan_source
from scripts.validate_requirements import validate

EXPECTED_SOURCE_SHA256 = "aa50863cde1313a7039691b4ca596c1ab498d0fab0008da324de5cb69f12ffc4"


class RequirementTraceabilityTests(unittest.TestCase):
    """Verify exact requirement coverage and canonical plan-control provenance."""

    def test_complete_rule_based_traceability(self) -> None:
        """Resolve all MUST and V2 identifiers to exactly one complete rule."""
        validate()

    def test_plan_source_fingerprint(self) -> None:
        """Verify the frozen external-plan identity and canonical control hash."""
        self.assertEqual(validate_plan_source(), EXPECTED_SOURCE_SHA256)


if __name__ == "__main__":
    unittest.main()
