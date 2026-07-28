"""Tests for rule-based requirement traceability."""
import unittest

from scripts.validate_requirements import validate


class RequirementTraceabilityTests(unittest.TestCase):
    """Verify exact coverage of every registered requirement identifier."""

    def test_complete_rule_based_traceability(self) -> None:
        """Resolve all MUST and V2 identifiers to exactly one complete rule."""
        validate()


if __name__ == "__main__":
    unittest.main()
