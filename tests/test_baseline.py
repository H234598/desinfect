import json
import unittest
from pathlib import Path
from scripts import validate_baseline
ROOT=Path(__file__).resolve().parents[1]
class BaselineTests(unittest.TestCase):
    def test_complete_baseline(self)->None: validate_baseline.main()
    def test_locked_decisions(self)->None:
        data=json.loads((ROOT/"config/architecture-decisions.json").read_text(encoding="utf-8")); self.assertEqual(data["locked_decisions"],{"ADR-003":"A","ADR-014":"B"}); choices={item["id"]:item["choice"] for item in data["decisions"]}; self.assertEqual(choices["ADR-003"],"A"); self.assertEqual(choices["ADR-014"],"B")
if __name__=="__main__": unittest.main()
