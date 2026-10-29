"""Count-table specs with filters, first-match grouping for summary tables, table labels by rule."""

import unittest

from ichnos import paper, tables


class TablesTest(unittest.TestCase):
    def test_count_specs(self):
        p = {"paper": {"count_tables": [["a", "b"], {"rows": "c", "cols": "d", "where": {"a": ["x"]}}]}}
        self.assertEqual(tables.count_specs(p), [{"rows": "a", "cols": "b"},
                                                  {"rows": "c", "cols": "d", "where": {"a": ["x"]}}])
        self.assertEqual(tables.count_specs({"findings": {"matrix": ["a", "b"]}}), [{"rows": "a", "cols": "b"}])

    def test_first_group_wins(self):
        groups = {"Students": ["student"], "Health professionals": ["nurs"], "_rest": "other"}
        self.assertEqual(tables._grouped(["nursing students"], "", groups), ["Students"])
        self.assertEqual(tables._grouped(["engineers"], "", groups), ["other or unnamed"])
        self.assertEqual(tables._grouped(["not_reported"], "", groups), ["not reported"])
        self.assertEqual(tables._grouped(["ghana"], "", {"China": ["china"], "_rest": "values"}), ["Ghana"])
        self.assertEqual(tables._grouped(["united_kingdom"], "", {"_rest": "values"}), ["United Kingdom"])
        self.assertEqual(tables._grouped(["intervention_trial", "experimental"], "", None),
                         ["experimental + intervention trial"])

    def test_table_label(self):
        self.assertEqual([paper.table_label(t) for t in ("F1", "T7", "S5", "x")],
                         ["Figure 1", "Table 7", "Table S5", None])
        self.assertEqual(tables.label_id("Table S5"), "S5")


if __name__ == "__main__":
    unittest.main()
