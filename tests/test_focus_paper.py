"""A [focus] paper (aim kind "focus"): its findings, method facts, tables and writer brief come from the focused view."""

import unittest

from psyscientist import aim, paper, tables
from psyscientist.store import Store

FIELDS = [{"name": "candidate_rule", "categories": ["readiness_embodied_capital", "status_competition", "none"]},
          {"name": "outcome", "categories": ["completed_fertility_children", "other"]},
          {"name": "direction", "question": "What more of the cue does.", "categories": ["lowers", "raises", "no_relation", "mixed", "not_applicable"]},
          {"name": "context", "categories": ["high_income_post_transition", "historical"]},
          {"name": "evidence_type", "categories": ["natural_experiment", "longitudinal", "cross_sectional"]}]
PROTOCOL = {
    "title": "Rules", "findings": {"matrix": ["candidate_rule", "outcome"], "fields": FIELDS},
    "focus": {"rules": ["readiness_embodied_capital", "status_competition"], "causal": ["natural_experiment"],
              "longitudinal": ["longitudinal"], "framing_dimension": "evolutionary_framing",
              "framed": ["explicit", "partial"], "explicit": ["explicit"], "ancestral": ["historical"],
              "strength": {"strong_min_studies": 8},
              "mismatch": {"ancestral": ["historical"], "modern": ["high_income_post_transition"], "alpha": 0.05}},
    "aim": {"id": "mismatch", "kind": "focus", "question": "Do the cues work differently today?",
            "audience": "evolutionary demographers"},
    "paper": {"target": "a journal"}}


def finding(rule, direction, context, design, text):
    return {"finding": text, "evidence": text,
            "fields": {"candidate_rule": rule, "outcome": "completed_fertility_children", "direction": direction,
                       "context": context, "evidence_type": design}}


class FocusPaper(unittest.TestCase):
    def setUp(self):
        self.store = store = Store(":memory:")
        self.run = run = store.start_run("pilot", "Z", None, {})
        self.w = {}
        for key, title, framing, rows in (
                ("reform", "A school reform and births", "none",
                 [finding("readiness_embodied_capital", "lowers", "high_income_post_transition", "natural_experiment",
                          "Longer schooling, fewer births.")]),
                ("parish", "Wealth and children in parish records", "explicit",
                 [finding("status_competition", "raises", "historical", "cross_sectional",
                          "Richer farmers had more children.")]),
                ("survey", "A survey of wishes", "none",
                 [finding("status_competition", "lowers", "high_income_post_transition", "cross_sectional",
                          "Higher status, fewer wished-for children.")])):
            w = self.w[key] = store.upsert_work({"title": title, "year": 2020, "authors": ["Ann Smith"]})
            store.replace_findings(run, w, "model:f", rows)
            store.add_feature(run, w, "model:e", "evolutionary_framing", framing, framing, "q", True)
        store.add_sample(run, list(self.w.values()), "pilot_screening")
        for w in self.w.values():
            store.add_screening(run, w, "Z", "title_abstract", "model:s", "include")

    def test_the_paper_rests_on_the_focused_findings_only(self):
        rows = aim.aim_rows(self.store, self.run, PROTOCOL)
        self.assertEqual({r["work_id"] for r in rows}, {self.w["reform"], self.w["parish"]})   # the survey is out

    def test_method_facts_say_how_the_studies_were_chosen_and_what_no_person_checked(self):
        facts = {f["id"]: f["text"] for f in aim.facts(self.store, self.run, PROTOCOL)}
        self.assertIn("It rests on 2 findings from 2 studies on 2 candidate rules", facts["A0"])
        self.assertIn("1 by design, 0 longitudinal with an evolutionary framing, 1 explicit", facts["A1"])
        self.assertIn("Fisher's exact test", facts["A3"])
        self.assertIn("What no person checked", facts["A5"])

    def test_the_statements_lead_and_the_studies_go_to_the_supplement(self):
        labels = tables.table_labels(PROTOCOL)
        self.assertEqual(labels["statements"], "Table 1")
        self.assertTrue(labels["studies"].startswith("Table S"))
        self.assertNotIn("Table 2", [v for k, v in labels.items() if k != "studies"])
        items = {i["id"]: i for i in paper.table_items(self.store, self.run, PROTOCOL)}
        self.assertEqual(items["T1"]["place"], "main")
        self.assertIn("Evidence by rule and demographic context: 2 studies", items["T1"]["text"])
        studies = items[tables.label_id(labels["studies"])]
        self.assertEqual(studies["place"], "supplement")
        self.assertIn("explicit evolutionary test, subsistence or historical population", studies["text"])

    def test_the_printed_supplement_keeps_the_focused_studies_and_the_map_tables_go_to_the_data_file(self):
        method = {"section": "Method.", "studies": {str(self.w["reform"]): {}}, "retained": list(self.w.values()),
                  "tables": [{"id": "S1", "role": "sources", "place": "supplement", "text": "**Table S1.** All."},
                             {"id": "S2", "role": "studies", "place": "supplement", "text": "**Table S2.** Focus."}]}
        entry = lambda w: (w["cite"], w["title"])
        printed = paper._supplement_body(self.store, None, method, entry, "print")
        self.assertIn("**Table S2.** Focus.", printed)
        self.assertNotIn("**Table S1.** All.", printed)
        self.assertIn("Table S1 and the list of every record retained", printed)
        self.assertIn("A school reform and births", printed)
        self.assertNotIn("A survey of wishes", printed)                      # retained, but not a focused study
        data = paper._supplement_body(self.store, None, method, entry, "data")
        self.assertIn("**Table S1.** All.", data)
        self.assertNotIn("**Table S2.** Focus.", data)
        self.assertIn("A survey of wishes", data)

    def test_records_left_on_the_sheet_unscreened_are_not_counted_as_screened(self):
        left = self.store.upsert_work({"title": "A mined record nobody screened", "year": 2021})
        self.store.add_sample(self.run, [left], "pilot_screening")
        from psyscientist.agents import screened_ids
        self.assertNotIn(left, screened_ids(self.store, self.run))
        figure = paper.flow(self.store, self.run)
        self.assertIn("3 records screened", figure)
        self.assertIn("1 records left unscreened by a decision recorded in the protocol", figure)
        facts = " ".join(f["text"] for f in paper.method_facts(self.store, self.run, PROTOCOL))
        self.assertIn("1 further records on the screening sheet were left unscreened", facts)
        self.assertIn("pre-specified tests that count studies by direction", facts)

    def test_sensitivity_analyses_repeat_the_test_and_count_named_samples_once(self):
        from psyscientist import rulemap
        lo, hi = rulemap.odds_ci(10, 4, 2, 17)
        self.assertAlmostEqual(lo, 3.27, places=1)
        self.assertAlmostEqual(hi, 137.7, places=0)
        store, run = self.store, self.run
        twin = store.upsert_work({"title": "Wealth and children in parish records, the thesis", "doi": "10.9/thesis",
                                  "year": 2019, "authors": ["Bo Lee"]})
        store.replace_findings(run, twin, "model:f", [finding("status_competition", "raises", "historical",
                                                              "cross_sectional", "Richer farmers, more children.")])
        store.add_feature(run, twin, "model:e", "evolutionary_framing", "explicit", "explicit", "q", True)
        store.add_sample(run, [twin], "pilot_screening")
        store.add_screening(run, twin, "Z", "title_abstract", "model:s", "include")
        with store.db:
            store.db.execute("UPDATE works SET doi = '10.9/article' WHERE work_id = ?", (self.w["parish"],))
        p = dict(PROTOCOL, focus=dict(PROTOCOL["focus"], sensitivity={"outcomes": ["completed_fertility_children"]}),
                 overlaps=[{"dois": ["10.9/article", "10.9/thesis"], "reason": "article and thesis"}])
        self.assertEqual(tables.table_labels(p)["sensitivity"], "Table 2")
        self.assertIn("Tables 1-2", paper.paper_instructions(p))
        text = rulemap.sensitivity_table(store, run, p, "Table 2")
        self.assertIn("| status competition | As pre-specified (Table 1) | 2 of 2 (100%) | 0 of 0 | – | no test |", text)
        self.assertIn("| status competition | Each shared sample named in the protocol counted once | 1 of 1 (100%) "
                      "| 0 of 0 |", text)
        self.assertIn("were not specified in advance", text)
        self.assertNotIn("Direction on each side", text)
        p["focus"]["sensitivity"]["sign_test"] = True
        text = rulemap.sensitivity_table(store, run, p, "Table 2")
        self.assertIn("| status competition | Direction on each side: the share finding ↑ against one half (two-sided "
                      "sign test, p for each side) | 2 of 2, p = 0.5 | 0 of 0, p = 1 | – | – |", text)
        self.assertEqual(round(rulemap.sign_test(8, 9), 4), 0.0391)
        f = lambda w, d, ctx: {"work_id": w, "fields": {"direction": d, "context": ctx,      # noqa: E731
                                                       "evidence_type": "longitudinal"}}
        lines = rulemap._statements("status_competition", [f(1, "raises", "historical"), f(2, "raises", "historical"),
                                                            f(3, "lowers", "high_income_post_transition")], p["focus"])
        self.assertIn("odds ratio 15.0 (95% CI 0.2 to 1236.3)", " ".join(lines))

    def test_a_person_can_check_a_random_sample_of_the_counted_findings_with_their_codes(self):
        import json
        from psyscientist import checkpack, checks
        paper.ensure(self.store)
        with self.store.db:
            pid = self.store.db.execute(
                "INSERT INTO papers (corpus, run_id, version, author, title, content, method, created_at) "
                "VALUES ('Z', ?, 1, 'model:w', 'T', ?, ?, '2026-10-26')",
                (self.run, json.dumps({"abstract": [], "sections": []}), json.dumps({"aim_id": "mismatch"}))).lastrowid
        row = self.store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (pid,)).fetchone()
        items = checks.sample_findings(self.store, row, PROTOCOL, 2, seed=1)
        self.assertEqual({i["work"] for i in items}, {self.w["reform"], self.w["parish"]})   # the survey is not counted
        self.assertTrue(all(i["uses"][0]["part"] == "Random sample" for i in items))
        pack = checkpack._findings(self.store, row, PROTOCOL, sample=2, seed=1)
        self.assertIn("direction: lowers", next(f["codes"] for f in pack if f["work_id"] == self.w["reform"]))
        with self.store.db:
            self.store.db.execute("UPDATE papers SET method = ? WHERE paper_id = ?",
                                  (json.dumps({"aim_id": "mismatch", "aim_kind": "focus"}), pid))
        row = self.store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (pid,)).fetchone()
        self.assertEqual(checks.headline_names(PROTOCOL, row), ["Abstract", "Main findings", "Conclusion"])

    def test_a_further_aim_brings_its_own_focus(self):
        kin = {"rules": ["kin_support"], "ancestral": ["historical"]}
        p = dict(PROTOCOL, aims=[{"id": "kin", "kind": "focus", "question": "Which kin?", "focus": kin}])
        self.assertEqual(aim.with_aim(p, "kin")["focus"], kin)
        self.assertEqual(aim.with_aim(p, "mismatch")["focus"], PROTOCOL["focus"])

    def test_the_writer_is_asked_for_a_test_not_a_review(self):
        text = paper.paper_instructions(PROTOCOL)
        self.assertIn("This paper tests one question: Do the cues work differently today?", text)
        self.assertIn("Cite Figure 1 and Table 1 in the text", text)
        self.assertIn("Main findings", text)
        self.assertNotIn("Tables 1-4", text)
        self.assertNotIn("Practical implications", text)

    def test_the_writer_draws_practical_implications_bound_to_the_evidence(self):
        a = dict(PROTOCOL["aim"], implications={"heading": "Practical implications for policy", "words": "400-600",
                                                "for": "Say what the results mean for family policy."})
        text = paper.paper_instructions(dict(PROTOCOL, aim=a))
        self.assertIn("a block headed 'Practical implications for policy' (400-600 words): Say what the results mean "
                      "for family policy.", text)
        self.assertIn("one resting on associations says what should be tried and evaluated", text)
        self.assertIn("and its main practical implication.", text)


if __name__ == "__main__":
    unittest.main()
