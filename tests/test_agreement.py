import unittest

from ichnos.agreement import Pair, cohen_kappa, complete_pairs


class Kappa(unittest.TestCase):
    def test_textbook_value(self):
        labels = [("include", "include"), ("include", "exclude"), ("exclude", "exclude"), ("exclude", "exclude")]
        self.assertAlmostEqual(cohen_kappa(labels), 0.5)

    def test_perfect_and_empty(self):
        self.assertEqual(cohen_kappa([("exclude", "exclude")] * 3), 1.0)
        self.assertIsNone(cohen_kappa([]))

    def test_collapsing_unsure(self):
        p = Pair("human:a", "model:b", [1, 2, 3, 4],
                 [("include", "unsure"), ("unsure", "include"), ("exclude", "exclude"), ("exclude", "exclude")])
        self.assertLess(p.kappa(), 1.0)
        self.assertEqual(p.kappa(collapse_unsure=True), 1.0)

    def test_dropped_by(self):
        p = Pair("human:a", "model:b", [1, 2, 3],
                 [("include", "exclude"), ("exclude", "unsure"), ("unsure", "exclude")])
        self.assertEqual(p.dropped_by("model:b"), [1, 3])
        self.assertEqual(p.dropped_by("human:a"), [2])


class Completeness(unittest.TestCase):
    def test_only_finished_screeners_are_paired(self):
        rows = [{"screener": "model:b", "work_id": w, "decision": "exclude"} for w in (1, 2, 3)]
        rows += [{"screener": "human:a", "work_id": w, "decision": "exclude"} for w in (1, 2)]
        by, pairs = complete_pairs(rows, {1, 2, 3})
        self.assertEqual(pairs, [])
        self.assertEqual(len(by["human:a"]), 2)
        rows.append({"screener": "human:a", "work_id": 3, "decision": "include"})
        _, pairs = complete_pairs(rows, {1, 2, 3})
        self.assertEqual([(p.a, p.b, p.n) for p in pairs], [("human:a", "model:b", 3)])


if __name__ == "__main__":
    unittest.main()
