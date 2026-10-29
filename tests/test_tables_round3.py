"""Tables added after the second editorial decision: one outcome table, comparisons in kinds, fidelity, programmes,
evidence strength, and names charted in lowercase."""

import unittest

from ichnos import tables
from ichnos.store import Store

PROTOCOL = {
    "map": {"dimensions": [
        {"name": "setting", "categories": ["medicine", "nursing"]},
        {"name": "design", "categories": ["survey", "case_report", "quasi_experimental"]},
        {"name": "comparison", "categories": ["none", "pre_post", "randomised"]},
        {"name": "fidelity", "categories": ["low_stakes_data_points", "committee_decision", "replaces_traditional",
                                            "added_to_traditional", "none_described"]},
        {"name": "comparison_kind", "categories": ["pa_vs_other_parallel", "before_after", "variants_within_pa", "none"]},
        {"name": "institution"}, {"name": "population"}]},
    "findings": {"fields": [
        {"name": "outcome", "categories": ["performance", "stress"]},
        {"name": "setting", "categories": ["medicine", "nursing"]},
        {"name": "evidence_type", "categories": ["comparative", "pre_post", "perception"]},
        {"name": "direction", "categories": ["favourable", "unfavourable", "mixed"]}]},
}
SPEC = {"title": "Comparative evidence", "any": [{"dimension": "evidence_type", "values": ["comparative", "pre_post"]}],
        "group_by": "comparison_kind", "groups": {"pa_vs_other_parallel": "In parallel", "before_after": "Before and after",
                                                  "variants_within_pa": "Variants"},
        "effect_groups": ["pa_vs_other_parallel", "before_after"], "partial": ["added_to_traditional"]}


class RoundThreeTables(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.run = self.store.start_run("pilot", "T", None, {})
        studies = {
            "A": ({"setting": "medicine", "design": "quasi_experimental", "comparison": "pre_post",
                   "fidelity": "low_stakes_data_points; committee_decision; replaces_traditional",
                   "comparison_kind": "before_after", "institution": "example university",
                   "population": "120 students"},
                  [("performance", "medicine", "pre_post", "favourable"), ("stress", "medicine", "perception", "unfavourable")]),
            "B": ({"setting": "nursing", "design": "case_report", "comparison": "none", "fidelity": "added_to_traditional",
                   "comparison_kind": "pa_vs_other_parallel", "institution": "example university",
                   "population": "n = 1 trainee"},
                  [("performance", "nursing", "comparative", "mixed")]),
            "C": ({"setting": "medicine", "design": "survey", "comparison": "none", "fidelity": "none_described",
                   "comparison_kind": "none", "institution": "not_reported", "population": "40 teachers"},
                  [("stress", "medicine", "perception", "favourable"), ("stress", "nursing", "perception", "unfavourable")]),
        }
        self.ids = {}
        for name, (chart, finds) in studies.items():
            w = self.store.upsert_work({"title": f"Study {name} of programmatic assessment", "year": 2020,
                                        "authors": [{"A": "Ann Smith", "B": "Bob Jones", "C": "Cy Brown"}[name]]})
            self.ids[name] = w
            for d, v in chart.items():
                self.store.add_feature(self.run, w, "model:c", d, v, v, None, None)
            self.store.replace_findings(self.run, w, "model:f", [
                {"finding": f"{o} {d}", "evidence": "passage", "fields": {"outcome": o, "setting": s,
                                                                          "evidence_type": e, "direction": d}}
                for o, s, e, d in finds])
        # A and B may share a sample
        self.ov = {"root": {self.ids["A"]: self.ids["A"], self.ids["B"]: self.ids["A"], self.ids["C"]: self.ids["C"]}}

    def test_outcome_table_counts_studies_once_and_independent_studies(self):
        text = tables.outcome_table(self.store, self.run, PROTOCOL, {}, "Table 4", None, self.ov)
        self.assertIn("the 3 studies are 2 independent studies", text)
        self.assertIn("| performance | 2 | 1 | 1 | 1 | 1 | 0 | 1 |", text)      # A and B: one sample
        self.assertIn("| stress | 2 | 2 | 2 | 1 | 1 | 2 | 0 |", text)           # C counts in both settings
        self.assertIn("not vote counts", text)
        only = tables.outcome_table(self.store, self.run, PROTOCOL, {}, "Table 4", {self.ids["C"]}, self.ov)
        self.assertIn("Outcomes of the 1 core studies with findings", only)

    def test_comparisons_are_grouped_and_marked(self):
        text = tables.comparison_table(self.store, self.run, PROTOCOL, SPEC, "Table 2", None, self.ov)
        self.assertIn("*In parallel* (1 studies)", text)
        self.assertIn("*Before and after* (1 studies)", text)
        self.assertIn("case report; single participant; may share a sample with Smith, 2020; partial "
                      "implementation (added to traditional high-stakes exams)", text)
        self.assertIn("2 studies compare programmatic with other assessment (in parallel, before and after); counting "
                      "reports that may share a sample once, they are 1 independent comparisons, 0 of them randomised",
                      text)
        self.assertNotIn("Brown", text)                                      # no comparative finding

    def test_fidelity_programmes_and_strength(self):
        fid = tables.fidelity_table(self.store, self.run, PROTOCOL, {"outcomes": ["stress"]}, "Table 8", None, self.ov)
        self.assertIn("| replaces traditional assessment; decisions on aggregated data | 1 | 1 | 1 (unfavourable 1) |", fid)
        self.assertIn("| no principle described | 1 | 1 | 1 (favourable 1, unfavourable 1) |", fid)
        core = set(self.ids.values())
        prog = tables.programme_table(self.store, self.run, PROTOCOL, {"aliases": {}}, "Table 7", None, self.ov, core)
        self.assertIn("| Example University | 2 | 2 | 1 | medicine 1, nursing 1 |", prog)
        self.assertIn("| No institution named | 1 | 1 | 1 | – | – |", prog)
        strength = tables.strength_table(self.store, self.run, PROTOCOL, "Table 9", None, self.ov)
        self.assertIn("| performance | 2 | 1 | 1 | 2 (0) | 0 |", strength)
        self.assertIn("| stress | 2 | 2 | 1 | 0 (0) | 2 |", strength)

    def test_names_charted_in_lowercase_are_capitalised(self):
        self.assertEqual(tables._proper("united kingdom"), "United Kingdom")
        self.assertEqual(tables._proper("university of otago"), "University of Otago")


if __name__ == "__main__":
    unittest.main()
