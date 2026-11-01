"""The strong-evidence aim: the selection rule over appraised studies, and the checks of the agents' answers."""

import unittest
from unittest import mock

from ichnos import strong


def appraisal(ident, experiment, country="x", outcome="number", publication="peer_reviewed", completed=True):
    return {"identification": ident, "experiment": experiment, "country": country, "outcome": outcome,
            "publication": publication, "completed": completed}


class FakeStore:
    def get_fulltext(self, w):
        return {"status": "parsed"} if w % 2 else None


class Selection(unittest.TestCase):
    def select(self, cands, apps, rule):
        protocol = {"aim": {"kind": "strong", "select": rule}, "focus": {"rules": ["ready", "kin"]}}
        with mock.patch.object(strong, "candidates", return_value=cands), \
                mock.patch.object(strong, "latest_appraisals", return_value=apps), \
                mock.patch.object(strong.tables, "works", return_value={w: {"year": 2000 + w} for w in cands}):
            return strong.select(FakeStore(), 1, protocol)

    def test_design_first_one_study_per_experiment_scarce_rule_first_and_a_country_cap(self):
        cands = {1: ["ready"], 2: ["ready"], 3: ["ready", "kin"], 4: ["ready"], 5: ["ready"], 6: ["kin"], 7: ["ready"]}
        apps = {1: appraisal("other_natural_experiment", "a"),
                2: appraisal("regression_discontinuity", "b"),
                3: appraisal("randomised", "c"),
                4: appraisal("regression_discontinuity", "B"),          # the same experiment as 2, a later study
                5: appraisal("not_causal", "e"),
                6: appraisal("difference_in_differences", "f", country="y"),
                7: appraisal("regression_discontinuity", "g", outcome="timing")}
        out = self.select(cands, apps, {"order": ["kin", "ready"], "quota": {"kin": 5, "ready": 2}, "per_country": 9})
        self.assertEqual(out["by_rule"]["kin"], [3, 6])              # kin first takes the trial both rules share
        self.assertEqual(out["by_rule"]["ready"], [4, 7])            # 4 outranks 2 (more recent), number before timing
        self.assertEqual(out["same_experiment"], {2: 4})
        self.assertEqual(out["not_causal"], [5])
        self.assertEqual(out["fulltext"], {3: True, 6: False, 4: False, 7: True})
        capped = self.select(cands, apps, {"order": ["ready"], "quota": {"ready": 5}, "per_country": 2})
        self.assertEqual(capped["by_rule"]["ready"], [3, 4])

    def test_intentions_only_studies_drop_out_names_alias_and_the_last_rule_fills_the_total(self):
        cands = {1: ["kin"], 2: ["ready"], 3: ["ready"], 4: ["ready"], 5: ["ready"]}
        apps = {1: appraisal("randomised", "survey experiment", outcome="intentions"),
                2: appraisal("regression_discontinuity", "Britain 1972 school leaving age"),
                3: appraisal("regression_discontinuity", "UK 1972 RoSLA", country="z"),
                4: appraisal("instrumental_variable", "d", country="y"),
                5: appraisal("other_natural_experiment", "e", country="w")}
        out = self.select(cands, apps, {"order": ["kin", "ready"], "quota": {"kin": 3, "ready": 1}, "total": 2,
                                        "births_only": True,
                                        "same": {"UK 1972 RoSLA": ["Britain 1972 school leaving age"]}})
        self.assertEqual(out["intentions_only"], [1])
        self.assertEqual(out["same_experiment"], {2: 3})
        self.assertEqual(out["by_rule"], {"kin": [], "ready": [3, 4]})     # the total of 2, not the quota of 1

    def test_answers_are_checked_against_the_rubric(self):
        batch = [{"work_id": 1}, {"work_id": 2}]
        errs = strong._check("appraise", batch, {"studies": [
            {"work_id": 1, "identification": "regression discontinuity", "publication": "peer_reviewed",
             "outcome": "both", "variation": "v", "experiment": "e", "threat": "t", "evidence": "q"}]})
        self.assertIn("every study exactly once (missing [2], unknown [])", errs)
        self.assertTrue(any("identification must be one of" in e for e in errs))
        errs = strong._check("effects", [{"work_id": 1}], {"studies": [{"work_id": 1, "estimates": [
            {"statement": "s", "exposure": "x", "estimate": "0.4 years", "population": "p", "evidence": "q",
             "outcome": "first_birth_timing", "direction": "sooner"}]}]})
        self.assertEqual(errs, [f"study 1 estimate 1: direction must be one of {strong.DIRECTIONS}"])


class Paper(unittest.TestCase):
    def test_a_strong_essay_cites_its_effects_table_and_is_told_not_to_count(self):
        from ichnos import paper, tables
        protocol = {"title": "T", "aim": {"id": "strong", "kind": "strong", "layout": "essay", "question": "Q?"},
                    "paper": {}}
        labels = tables.table_labels(protocol)
        self.assertEqual((labels["effects"], labels["model"]), ("Table 1", "Table 2"))
        self.assertTrue(labels["appraisals"].startswith("Table S"))
        text = paper.paper_instructions(protocol)
        self.assertIn("no counts of directions across studies", text)
        self.assertIn(f"{labels['appraisals']} in the supplement gives every candidate's appraisal", text)
        self.assertIn("never let a delay stand for a loss of births", text)
        self.assertNotIn("Lead with the majority", text)


if __name__ == "__main__":
    unittest.main()
