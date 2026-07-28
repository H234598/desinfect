import json
import unittest
from pathlib import Path
from scripts.validate_plan_progress import validate
ROOT=Path(__file__).resolve().parents[1]
class PlanProgressTests(unittest.TestCase):
    def test_plan_progress_is_traceable(self)->None: validate()
    def test_p00_is_not_claimed_as_implemented(self)->None:
        data=json.loads((ROOT/"docs/implementation-status.json").read_text(encoding="utf-8")); p00=[item for item in data["work_packages"] if item["id"].startswith("P00.")]; self.assertEqual(len(p00),3); self.assertTrue(all(item["status"]=="im_review" for item in p00)); self.assertTrue(all(item["pr_number"]==1 for item in p00)); self.assertTrue(all((item.get("evidence") or {}).get("merge_sha") is None for item in p00))
if __name__=="__main__": unittest.main()
