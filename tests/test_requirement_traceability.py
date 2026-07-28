import unittest
from scripts.validate_requirements import validate
class RequirementTraceabilityTests(unittest.TestCase):
    def test_complete_rule_based_traceability(self)->None: validate()
if __name__=="__main__": unittest.main()
