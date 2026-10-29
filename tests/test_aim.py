"""Aim-focused papers: the aim set, the solutions table, the labels and the shape of the manuscript."""

import json
import tomllib

from ichnos import aim, paper, tables
from ichnos.store import now
from tests.test_agents_map import Base, PROTOCOL

AIM = '''
[aim]
kind = "solutions"
question = "Which sleep habits help mood?"
by = "design_choice"
link = "aim_link"
domain = "outcome_domain"

[[findings.fields]]
name = "design_choice"
question = "Which habit?"
categories = ["bedtime", "screens", "none"]

[[findings.fields]]
name = "aim_link"
question = "How?"
categories = ["tested", "associated", "reported", "none"]

[[findings.fields]]
name = "outcome_domain"
question = "Which outcome?"
categories = ["learning", "wellbeing", "both", "neither"]
'''


class AimPaper(Base):
    def setUp(self):
        super().setUp()
        self.protocol = tomllib.loads(PROTOCOL + AIM)
        for w, fields in ((1, {"design_choice": "bedtime", "aim_link": "tested; reported", "outcome_domain": "wellbeing",
                               "direction": "better"}),
                          (2, {"design_choice": "screens", "aim_link": "reported", "outcome_domain": "both",
                               "direction": "worse"}),
                          (3, {"design_choice": "none", "aim_link": "none", "outcome_domain": "neither"})):
            self.store.db.execute("INSERT INTO findings (work_id, run_id, extractor, finding, fields, evidence, status, "
                                  "created_at) VALUES (?, ?, 'model:x', 'f', ?, 'p', 'extracted', ?)",
                                  (w, self.run, json.dumps(fields), now()))
        self.store.db.commit()

    def test_only_findings_that_bear_on_the_aim_count(self):
        rows = aim.aim_rows(self.store, self.run, self.protocol)
        self.assertEqual(sorted(r["work_id"] for r in rows), [1, 2])
        text = aim.solutions_table(self.store, self.run, self.protocol, "Table 1")
        self.assertIn("| bedtime | 1 | 0 | 0 | 1 |", text)           # studies, described as PA, full text, findings
        self.assertIn("1 tested", text)                       # the strongest of 'tested; reported'
        self.assertIn("| screens | 1 | 0 | 0 | 1 | 1 worse | 1 worse | 1 reported |", text)   # both outcomes count
        self.assertIn("(2 when reports that may share a sample count once)", text)

    def test_a_focused_paper_moves_the_scoping_tables_and_needs_a_method(self):
        L = tables.table_labels(self.protocol)
        self.assertEqual((L["solutions"], L["comparator"], L["patterns"]), ("Table 1", "Table 2", "Table 3"))
        self.assertTrue(L["overlaps"].startswith("Table S"))
        shape = {"title": "t", "abstract": [{"sentences": [{"text": "a"}]}],
                 "sections": [{"name": n, "paragraphs": []} for n in paper.SECTIONS]}
        self.assertIn("section Method is missing", paper.check_shape(shape, {"aim": self.protocol["aim"]}))
        self.assertEqual(paper.check_shape(shape, {}), [])


class Directions(AimPaper):
    def test_proposed_directions_are_checked_stored_and_chosen(self):
        from ichnos import directions
        data = {"finding_ids": [1, 2], "tables": {"T1": "x"}, "insights": [{"id": 5}], "paper_id": 1}
        good = {"title": "t", "kind": "new_review", "question": "q", "why": "w", "evidence": "e", "unknown": "u",
                "next_step": "n", "value": 4, "feasibility": 3, "novelty": 2, "cites": [1], "tables": ["T1"]}
        self.assertIn("propose 6 to 10 directions, not 1", directions.check({"directions": [good]}, data))
        bad = [dict(good, id=f"D{i}") for i in range(6)]
        bad[0] = dict(bad[0], kind="vibes", value=9, cites=[99])
        errs = "\n".join(directions.check({"directions": bad}, data))
        self.assertIn("kind must be one of", errs)
        self.assertIn("value must be an integer", errs)
        self.assertIn("cites what was not given: [99]", errs)
        self.assertEqual(directions.check({"directions": [dict(good, id=f"D{i}") for i in range(6)]}, data), [])

    def test_several_papers_are_read_together(self):
        from ichnos import directions
        paper.ensure(self.store)
        ids = []
        for title in ("General", "Proposal"):
            method = {"aim": True, "section": "", "facts": [], "retained": [], "core": [], "cite_labels": {},
                      "background_full": [], "tables": [{"id": "T1", "place": "main", "text": f"**Table 1.** {title}."}]}
            content = {"title": title, "abstract": [{"sentences": [{"text": "A.", "frame": True}]}], "sections": []}
            with self.store.db:
                ids.append(self.store.db.execute(
                    "INSERT INTO papers (corpus, run_id, version, author, title, content, method, verification, "
                    "created_at) VALUES ('Z', ?, 1, 'model:x', ?, ?, ?, '{}', ?)",
                    (self.run, title, json.dumps(content), json.dumps(method), now())).lastrowid)
        data = directions._input(self.store, self.run, self.protocol, ids)
        self.assertEqual([p["title"] for p in data["papers"]], ["General", "Proposal"])
        self.assertEqual(set(data["tables"]), {f"P{ids[0]}-T1", f"P{ids[1]}-T1"})
        self.assertEqual(data["paper_id"], ids[1])
        self.assertIn("several finished papers", directions.instructions(self.protocol, True))
        one = directions._input(self.store, self.run, self.protocol, None)          # the latest paper alone
        self.assertNotIn("papers", one)
        self.assertEqual((one["paper_id"], set(one["tables"])), (ids[1], {"T1"}))


class FocusedRender(AimPaper):
    def test_the_submission_leaves_the_supplement_apart(self):
        from ichnos import paper as paper_mod
        paper_mod.ensure(self.store)
        method = {"aim": True, "section": "Full generated method.", "facts": [], "retained": [], "core": [],
                  "tables": [{"id": "T1", "place": "main", "text": "**Table 1.** Main table."},
                             {"id": "S2", "place": "supplement", "text": "**Table S2.** Second."},
                             {"id": "S1", "place": "supplement", "text": "**Table S1.** First."}],
                  "cite_labels": {}, "background_full": []}
        content = {"title": "Focused", "abstract": [{"sentences": [{"text": "A.", "frame": True}]}],
                   "sections": [{"name": n, "paragraphs": [{"sentences": [{"text": n + " text.", "frame": True}]}]}
                                for n in ("Introduction", "Method", "Results", "Discussion", "Conclusion")]}
        with self.store.db:
            cur = self.store.db.execute(
                "INSERT INTO papers (corpus, run_id, version, author, title, content, method, verification, created_at) "
                "VALUES ('Z', ?, 1, 'model:x', 'Focused', ?, ?, '{}', ?)",
                (self.run, json.dumps(content), json.dumps(method), now()))
        pid = cur.lastrowid
        sub = paper_mod.render(self.store, pid, full=False)
        self.assertIn("**Table 1.** Main table.", sub)
        self.assertLess(sub.index("Method text."), sub.index("**Table 1.**"))       # tables follow the Method
        self.assertNotIn("Table S1", sub)
        self.assertNotIn("Full generated method.", sub)
        supp = paper_mod.render_supplement(self.store, pid)
        self.assertLess(supp.index("Table S1"), supp.index("Table S2"))             # in number order
        self.assertIn("Full generated method.", supp)


class GeneralPaper(Base):
    def test_the_general_paper_answers_by_outcome_first(self):
        protocol = tomllib.loads(PROTOCOL + AIM.replace('kind = "solutions"', 'kind = "effectiveness"'))
        L = tables.table_labels(protocol)
        self.assertEqual((L["outcomes_aim"], L["solutions"], L["comparator"], L["patterns"]),
                         ("Table 1", "Table 2", "Table 3", "Table 4"))
        self.assertTrue(L["studies"].startswith("Table S"))
        brief = "\n".join(paper._aim_parts(protocol))
        self.assertIn("Main findings", brief)
        self.assertIn("not turn it into a hypothesis", brief.replace("do not turn it into a", "not turn it into a"))
        self.assertIn("`model_table` (shown after the Discussion as Table 5)", brief)   # no comparisons table here

    def test_the_comparative_evidence_and_the_recommendations_are_main_tables(self):
        protocol = tomllib.loads(PROTOCOL + AIM.replace('kind = "solutions"', 'kind = "effectiveness"')
                                 + '\n[[paper.focus]]\ntitle = "Comparative evidence"\ngroup_by = "design"\n')
        L = tables.table_labels(protocol)
        self.assertEqual((L["focus0"], L["model"]), ("Table 5", "Table 6"))
        supplement = sorted(int(v[7:]) for v in L.values() if v.startswith("Table S"))
        self.assertEqual(supplement, list(range(1, len(supplement) + 1)))      # no gap where the table left
        self.assertIn("the comparative evidence study by study is in T5", "\n".join(paper._aim_parts(protocol)))
        row = {"component": "keep scores off single data points", "change": "x", "why": "y", "performance": "none",
               "wellbeing": "up", "strength": "4 studies", "test": "z", "cites": [1]}
        shape = {"title": "t", "abstract": [{"sentences": [{"text": "a"}]}], "model_table": {"rows": [row] * 2},
                 "sections": [{"name": n, "paragraphs": []} for n in paper.SECTIONS + ("Method",)]}
        self.assertIn("the recommendations table needs 3 to 9 rows, not 2", paper.check_shape(shape, {"aim": protocol["aim"]}))
        shape["model_table"]["rows"] = [row] * 3
        self.assertEqual(paper.check_shape(shape, {"aim": protocol["aim"]}), [])
        del shape["model_table"]                                               # optional in a general paper
        self.assertEqual(paper.check_shape(shape, {"aim": protocol["aim"]}), [])


class Proposal(Base):
    def test_a_proposal_names_its_aim_needs_its_sections_and_checks_its_model_table(self):
        from ichnos import aim as aim_mod
        protocol = tomllib.loads(PROTOCOL + AIM.replace('kind = "solutions"', 'id = "main"\nkind = "solutions"')
                                 + '\n[[aims]]\nid = "redesign"\nkind = "proposal"\nquestion = "How to redesign?"\n'
                                   'by = "design_choice"\nlink = "aim_link"\ndomain = "outcome_domain"\n')
        chosen = aim_mod.with_aim(protocol, "redesign")
        self.assertEqual(chosen["aim"]["kind"], "proposal")
        with self.assertRaises(ValueError):
            aim_mod.with_aim(protocol, "nope")
        self.assertEqual(tables.table_labels(chosen)["solutions"], "Table 1")
        sections = [{"name": n, "paragraphs": []} for n in paper.PROPOSAL_SECTIONS]
        row = {"component": "c", "change": "x", "why": "y", "performance": "up", "wellbeing": "up", "strength": "low",
               "test": "trial", "cites": [1]}
        draft = {"title": "t", "abstract": [{"sentences": [{"text": "a"}]}], "sections": sections,
                 "model_table": {"caption": "m", "rows": [row] * 3}}
        self.assertIn("the model table needs 4 to 9 rows, not 3", paper.check_shape(draft, {"aim": chosen["aim"]}))
        draft["model_table"]["rows"] = [row] * 4
        self.assertEqual(paper.check_shape(draft, {"aim": chosen["aim"]}), [])
        rows = [s for sec, _, _, s in paper.all_sentences(draft) if sec == "Model table"]
        self.assertEqual((len(rows), rows[0]["cites"]), (4, [1]))       # each row is checked like a sentence


class PlanSentences(Base):
    def test_design_parameters_in_a_proposal_are_not_taken_for_evidence(self):
        data = {"findings": [], "background": [], "method_facts": [{"id": "M1", "text": "x"}], "tables": [],
                "insights": [], "studies": {}}
        draft = {"sections": [
            {"name": "Evaluation", "paragraphs": [{"sentences": [
                {"text": "We would randomise 12 cohorts over 4 steps.", "method": ["M1"], "plan": True},
                {"text": "We would randomise 12 cohorts.", "method": ["M1"]}]}]},
            {"name": "Results", "paragraphs": [{"sentences": [
                {"text": "Scores rose by 12 points.", "method": ["M1"], "plan": True}]}]}]}
        flagged = [r["problems"] for r in paper.verify(draft, data, self.store)["sentences"]]
        self.assertEqual(flagged[0], [])                          # a plan's parameters
        self.assertTrue(flagged[1])                               # the same number, not marked as plan
        self.assertTrue(flagged[2])                               # 'plan' does not exempt evidence sections
