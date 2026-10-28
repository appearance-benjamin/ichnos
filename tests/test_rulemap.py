"""The evidence map by rule: sections from the findings' first axis, direction counts, the 'other' outcomes apart."""

import unittest

from psyscientist import rulemap
from psyscientist.store import Store

PROTOCOL = {"title": "Rules", "findings": {
    "matrix": ["candidate_rule", "outcome"],
    "fields": [{"name": "candidate_rule", "categories": ["kin_support", "child_quality_investment", "none"]},
               {"name": "modern_input", "categories": ["grandparental_help", "child_costs_norms", "other"]},
               {"name": "outcome", "categories": ["completed_fertility_children", "timing_first_birth", "other"]},
               {"name": "direction", "categories": ["lowers", "raises", "no_relation", "mixed"]}]}}


def finding(rule, inp, out, direction, text):
    return {"finding": text, "evidence": text, "fields": {"candidate_rule": rule, "modern_input": inp,
                                                          "outcome": out, "direction": direction}}


class RuleMap(unittest.TestCase):
    def test_sections_counts_and_the_other_outcomes_apart(self):
        store = Store(":memory:")
        run = store.start_run("pilot", "Z", None, {})
        a = store.upsert_work({"title": "Grandmothers and births", "doi": "10.1/a", "year": 2015,
                               "authors": ["Ann Smith"]})
        b = store.upsert_work({"title": "Siblings and schooling", "year": 2019, "authors": ["Bo Lee"]})
        store.replace_findings(run, a, "model:f", [
            finding("kin_support", "grandparental_help", "completed_fertility_children", "raises", "Help, more births."),
            finding("kin_support", "grandparental_help", "completed_fertility_children", "raises", "Again more."),
            finding("kin_support", "grandparental_help", "timing_first_birth", "no_relation", "No timing effect.")])
        store.replace_findings(run, b, "model:f", [
            finding("child_quality_investment", "child_costs_norms", "other", "lowers", "More siblings, less school.")])
        for w, rule, country in ((a, "kin_support", "finland"), (b, "child_quality_investment", "china")):
            store.add_feature(run, w, "model:e", "candidate_rule", rule, rule, "q", True)
            store.add_feature(run, w, "model:e", "country", country, country, "q", True)
            store.add_feature(run, w, "model:e", "design", "longitudinal_panel", "longitudinal_panel", "q", True)
        twin = store.upsert_work({"title": "Grandmothers and births (working paper)", "year": 2014,
                                  "abstract": None})
        with store.db:
            store.db.execute("UPDATE works SET abstract = ? WHERE work_id IN (?, ?)", ("A " * 200, a, twin))
        store.replace_findings(run, twin, "model:f", [
            finding("kin_support", "grandparental_help", "completed_fertility_children", "raises", "Help, more.")])
        store.add_sample(run, [a, b, twin], "pilot_screening")
        for w in (a, b, twin):
            store.add_screening(run, w, "Z", "title_abstract", "model:s", "include")
        text = rulemap.build(store, run, PROTOCOL)
        self.assertIn("1 second records of a study", text)                 # the working paper counts once
        self.assertIn("| [kin support](#kin-support) | 1 | 1 | 3 | 0 | 2 | 1 | 0 |", text)
        self.assertIn("| grandparental help | 2↑ | 1= |", text)
        self.assertIn("**Findings with outcome other** (1)", text)
        self.assertIn("More siblings, less school.", text)
        self.assertIn("[DOI](https://doi.org/10.1/a)", text)
        self.assertIn("**Design:** longitudinal panel 1", text)

    def test_a_focus_keeps_causal_longitudinal_framed_and_ancestral_explicit_studies_only(self):
        focus = {"rules": ["kin_support"], "causal": ["natural_experiment"], "longitudinal": ["longitudinal"],
                 "framing_dimension": "evolutionary_framing", "framed": ["explicit", "partial"],
                 "explicit": ["explicit"], "ancestral": ["historical"]}
        f = lambda w, t, ctx, d="raises", o="completed_fertility_children": {           # noqa: E731
            "work_id": w, "fields": {"evidence_type": t, "context": ctx, "direction": d, "outcome": o}}
        feats = {2: {"evolutionary_framing": ["partial"]}, 3: {"evolutionary_framing": ["explicit"]},
                 4: {"evolutionary_framing": ["none"]}, 5: {"evolutionary_framing": ["explicit"]}}
        chosen = rulemap.focus_studies([
            f(1, "natural_experiment", "high_income_post_transition"),      # a: causal
            f(2, "longitudinal", "high_income_post_transition"),            # b: longitudinal, framed
            f(3, "cross_sectional", "historical"),                          # c: explicit, ancestral
            f(4, "longitudinal", "historical"),                             # longitudinal but not framed: out
            f(5, "cross_sectional", "high_income_post_transition"),         # explicit but modern, weak design: out
            f(6, "natural_experiment", "historical", d="not_applicable"),   # no direction: out
            f(7, "natural_experiment", "historical", o="other")], feats, focus, "outcome")
        self.assertEqual({w: r for w, (r, _) in chosen.items()}, {1: "a", 2: "b", 3: "c"})

    def test_fisher_exact_and_verdicts(self):
        # the lady tasting tea: [[3, 1], [1, 3]] has two-sided p = 0.486
        self.assertAlmostEqual(rulemap.fisher_exact(3, 1, 1, 3), 0.4857, places=3)
        self.assertAlmostEqual(rulemap.fisher_exact(28, 0, 3, 3), 20 / 5984, places=6)   # only the observed table
        self.assertEqual(rulemap.fisher_exact(0, 0, 0, 0), 1.0)
        self.assertAlmostEqual(rulemap.odds_ratio(2, 1, 1, 2), 4.0)
        rules = {"strong_min_studies": 8, "strong_share": .75, "strong_design_share": .5,
                 "moderate_min_studies": 5, "moderate_share": .6}
        self.assertEqual(rulemap.verdict(16, 0.8, 0.6, rules), "strong")
        self.assertEqual(rulemap.verdict(16, 0.8, 0.3, rules), "moderate")     # consistent, but weak designs
        self.assertEqual(rulemap.verdict(6, 0.5, 1.0, rules), "inconsistent")
        self.assertEqual(rulemap.verdict(4, 1.0, 1.0, rules), "limited")

    def test_statements_test_ancestral_against_modern(self):
        focus = {"strength": {}, "mismatch": {"ancestral": ["historical"], "modern": ["high_income_post_transition"]}}
        f = lambda w, d, ctx: {"work_id": w, "fields": {"direction": d, "context": ctx,      # noqa: E731
                                                       "evidence_type": "longitudinal"}}
        rows = rulemap._statements("status_competition",
                                   [f(i, "raises", "historical") for i in range(1, 9)]
                                   + [f(i, "lowers", "high_income_post_transition") for i in range(20, 26)]
                                   + [f(30, "raises", "high_income_post_transition")], focus)
        text = "\n".join(rows)
        self.assertIn("| historical | 8 | 0 | 8 | 0 | 0 | raises | 100% | 100% | **strong** |", text)
        self.assertIn("in 8 of 8 studies with a direction in ancestral contexts (100%) and in 1 of 7", text)
        self.assertIn("The share finding a rise differs between the two.", text)
        self.assertIn("**Strong**: in historical populations, more of the cue raises fertility", text)

    def test_one_direction_per_study(self):
        f = lambda d: {"fields": {"direction": d}}               # noqa: E731
        self.assertEqual(rulemap.study_direction([f("raises"), f("no_relation")]), "raises")
        self.assertEqual(rulemap.study_direction([f("raises"), f("lowers")]), "mixed")
        self.assertEqual(rulemap.study_direction([f("no_relation")]), "no_relation")
        self.assertIsNone(rulemap.study_direction([f("not_applicable")]))
        rows = rulemap._by_study([{"work_id": 1, "fields": {"direction": "raises", "context": "historical"}},
                                  {"work_id": 1, "fields": {"direction": "raises", "context": "historical"}},
                                  {"work_id": 2, "fields": {"direction": "lowers", "context": "historical"}}],
                                 "context")
        self.assertIn("| historical | 2 | 1 | 1 | 0 | 0 |", rows)


if __name__ == "__main__":
    unittest.main()
