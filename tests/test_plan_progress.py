"""Tests for work-package progress and evidence rules."""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from scripts.validate_plan_progress import (
    is_positive_int,
    validate,
    validate_completion_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class PlanProgressTests(unittest.TestCase):
    """Verify ordered progress tracking and the P00 evidence lifecycle."""

    def test_plan_progress_is_traceable(self) -> None:
        """Run the complete progress validator."""
        validate()

    def test_positive_pr_numbers_exclude_booleans_and_nonpositive_values(self) -> None:
        """Match JSON Schema integer/minimum semantics in the Python validator."""
        for value in (True, False, 0, -1, None, "1"):
            self.assertFalse(is_positive_int(value), value)
        self.assertTrue(is_positive_int(1))
        self.assertTrue(is_positive_int(42))

    def test_completion_requires_requirement_coverage(self) -> None:
        """Reject a completion record that omits affected requirement IDs."""
        item = {
            "id": "P99.1",
            "pr_number": 1,
            "evidence": {
                "merge_sha": "0" * 40,
                "ci_runs": ["run:1"],
                "tests": ["python3 -m unittest"],
                "requirement_ids": [],
                "accepted_at": "2026-07-28T04:00:00Z",
                "accepted_by": "H234598",
            },
        }
        with self.assertRaisesRegex(ValueError, "requirement_ids"):
            validate_completion_evidence(item, {"MUSS-01"})

    def test_p00_review_or_completion_has_matching_evidence(self) -> None:
        """Accept only a review state or a fully evidenced completion state."""
        data = json.loads(
            (ROOT / "docs/implementation-status.json").read_text(encoding="utf-8")
        )
        p00 = [item for item in data["work_packages"] if item["id"].startswith("P00.")]
        self.assertEqual(len(p00), 3)
        for item in p00:
            evidence = item.get("evidence") or {}
            if item["status"] == "im_review":
                self.assertTrue(is_positive_int(item.get("pr_number")))
                self.assertIsNone(evidence.get("merge_sha"))
                self.assertTrue(evidence.get("requirement_ids"))
            elif item["status"] == "umgesetzt":
                self.assertRegex(evidence.get("merge_sha", ""), SHA40)
                self.assertTrue(evidence.get("ci_runs"))
                self.assertTrue(evidence.get("tests"))
                self.assertTrue(evidence.get("requirement_ids"))
                self.assertTrue(evidence.get("accepted_at"))
                self.assertTrue(evidence.get("accepted_by"))
            else:
                self.fail(f"{item['id']}: unerwarteter P00-Status {item['status']}")


if __name__ == "__main__":
    unittest.main()
