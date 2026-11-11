"""A metric marked exclude_on_title_only = false cannot exclude a record shown with its title only."""

import unittest

from ichnos.agents import decide

METRICS = [{"id": "empirical", "fail_reason": "not_empirical"},
           {"id": "design", "fail_reason": "not_randomized", "exclude_on_title_only": False},
           {"id": "population", "fail_reason": "wrong_population"}]


class TitleOnly(unittest.TestCase):
    def test_design_cannot_exclude_a_bare_title(self):
        scores = {"empirical": 2, "design": 0, "population": 2}
        self.assertEqual(decide(scores, "study", METRICS, title_only=True), ("unsure", None))
        self.assertEqual(decide(scores, "study", METRICS), ("exclude", "not_randomized"))

    def test_other_metrics_still_exclude_a_bare_title(self):
        scores = {"empirical": 2, "design": 0, "population": 0}
        self.assertEqual(decide(scores, "study", METRICS, title_only=True), ("exclude", "wrong_population"))


if __name__ == "__main__":
    unittest.main()
