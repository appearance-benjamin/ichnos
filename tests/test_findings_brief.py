"""A brief findings extraction: records marked `max_findings` get a smaller cap than the protocol's."""

import unittest

from ichnos import agents

PROTOCOL = {"findings": {"question": "q", "max_per_study": 6,
                         "brief": {"max_per_study": 3, "focus": "levels and correlates"},
                         "fields": [{"name": "direction", "question": "d", "categories": ["increase", "decrease"]}]}}


def finding(i):
    return {"finding": f"f{i}", "evidence": "e", "fields": {"direction": "increase"}}


class BriefTest(unittest.TestCase):
    def test_cap_per_record(self):
        records = [{"work_id": 1, "max_findings": 3}, {"work_id": 2}]
        ok = [{"work_id": 1, "findings": [finding(i) for i in range(3)]},
              {"work_id": 2, "findings": [finding(i) for i in range(6)]}]
        self.assertEqual(agents._check_findings(records, ok, PROTOCOL), [])
        over = [{"work_id": 1, "findings": [finding(i) for i in range(4)]}, ok[1]]
        self.assertEqual(agents._check_findings(records, over, PROTOCOL), ["1: 4 findings, at most 3"])

    def test_instructions_name_the_focus(self):
        text = agents.findings_instructions(PROTOCOL)
        self.assertIn("`max_findings`", text)
        self.assertIn("levels and correlates", text)


if __name__ == "__main__":
    unittest.main()
