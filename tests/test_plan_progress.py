"""Tests for work-package progress and evidence rules."""
import json
import re
import unittest
from pathlib import Path

from scripts.validate_plan_progress import validate

ROOT = Path(__file__).resolve().parents[1]
SHA40 = re.compile(r"^[0-9a-f]{40}$")


class PlanProgressTests(unittest.TestCase):
    """Verify ordered progress tracking and the P00 evidence lifecycle."""

    def test_plan_progress_is_traceable(self) -> None:
        """Run the complete progress validator."""
        validate()

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
                self.assertIsInstance(item.get("pr_number"), int)
                self.assertIsNone(evidence.get("merge_sha"))
            elif item["status"] == "umgesetzt":
                self.assertRegex(evidence.get("merge_sha", ""), SHA40)
                self.assertTrue(evidence.get("ci_runs"))
                self.assertTrue(evidence.get("tests"))
                self.assertTrue(evidence.get("accepted_at"))
                self.assertTrue(evidence.get("accepted_by"))
            else:
                self.fail(f"{item['id']}: unerwarteter P00-Status {item['status']}")


if __name__ == "__main__":
    unittest.main()
