"""Findings rows, insight capture from agents and by hand, synthesis, and the insight log."""

import json

from ichnos import agents, insights
from ichnos.report import build_report
from ichnos.review import build_data
from tests.test_agents_map import Base


class FindingsAndInsights(Base):
    def keep_all(self):
        for w in (1, 2, 4):
            self.store.add_screening(self.run, w, "Z", "title_abstract", "model:s", "include")

    def findings_task(self):
        self.keep_all()
        folder = agents.write_tasks(self.store, self.run, "findings", "model:f", root=self.root)
        text = (folder / "instructions.md").read_text(encoding="utf-8")
        self.assertIn("How does adolescent sleep relate", text)
        self.assertIn("Insights (optional)", text)
        answer = {"records": [
            {"work_id": 1, "findings": [{"finding": "Mood improved as sleep lengthened.", "effect": "",
                                         "for_whom": "", "fields": {"direction": "Better", "note": ""},
                                         "evidence": "the SMFQ fell over three waves", "location": "abstract"}]},
            {"work_id": 2, "findings": [{"finding": "Insomnia trajectories differed by sex.", "effect": "",
                                         "for_whom": "girls and boys", "fields": {"direction": "worse", "note": "x"},
                                         "evidence": "trajectories differed by sex", "location": "abstract"},
                                        {"finding": "Invented.", "fields": {"direction": "none", "note": ""},
                                         "evidence": "words that are nowhere", "location": "abstract"}]},
            {"work_id": 4, "findings": []}],
            "insights": [{"kind": "method", "statement": "Every study measures mood with the SMFQ.",
                          "rationale": "Same instrument in two of three studies.", "works": [1, 4],
                          "quotes": [{"work_id": 4, "text": "completed the SMFQ at baseline"}]}]}
        (folder / "batch_01_result.json").write_text(json.dumps(answer), encoding="utf-8")
        return folder, agents.import_task(folder, self.store)

    def test_findings_rows_sources_and_agent_insights(self):
        _, counts = self.findings_task()
        self.assertEqual(counts["problems"], [])
        self.assertEqual((counts["stored"], counts["insights"]), (3, 1))
        rows = self.store.db.execute("SELECT work_id, source, grounded, fields FROM findings ORDER BY finding_id").fetchall()
        self.assertEqual([(r[0], r[1], r[2]) for r in rows], [(1, "abstract", 1), (2, "abstract", 1), (2, None, 0)])
        self.assertEqual(json.loads(rows[0][3])["direction"], "better")
        ins = insights.rows(self.store, "Z")
        self.assertEqual((ins[0]["kind"], ins[0]["stage"], ins[0]["status"]), ("method", "findings", "noted"))
        self.assertTrue(json.loads(ins[0]["support"])["quotes"][0]["grounded"])

    def test_bad_findings_are_rejected(self):
        self.keep_all()
        folder = agents.write_tasks(self.store, self.run, "findings", "model:bad", root=self.root)
        answer = {"records": [{"work_id": w, "findings": [{"finding": "x", "evidence": "y",
                                                           "fields": {"direction": "sideways", "note": ""}}]}
                              for w in (1, 2, 4)],
                  "insights": [{"kind": "vibe", "statement": "s", "works": [99]}]}
        (folder / "batch_01_result.json").write_text(json.dumps(answer), encoding="utf-8")
        problems = agents.import_task(folder, self.store)["problems"]
        self.assertTrue(any("sideways" in p for p in problems))
        self.assertTrue(any("vibe" in p for p in problems))
        self.assertTrue(any("[99]" in p for p in problems))

    def test_synthesis_cites_findings_and_the_log_keeps_history(self):
        self.findings_task()
        folder = agents.write_tasks(self.store, self.run, "synthesize", "model:syn", root=self.root)
        data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertEqual(len(data["findings"]), 3)
        ids = [f["id"] for f in data["findings"]]
        self.assertIn("evidence", data["findings"][0])
        answer = {"insights": [{"kind": "hypothesis", "statement": "Sex differences may drive the trajectories.",
                                "rationale": "One study, abstract only; 42% of 596 teens (made up).",
                                "findings": ids[1:2], "works": [2]}]}
        (folder / "batch_01_result.json").write_text(json.dumps(answer), encoding="utf-8")
        counts = agents.import_task(folder, self.store)
        self.assertEqual((counts["problems"], counts["insights"]), ([], 1))
        self.assertEqual(counts["unverified_numbers"], 2)
        self.assertEqual(insights.numbers(insights.strip_references(
            "as existing insights 32, 39 and 46 and study 51 show 42% of 596")), ["42%", "596"])
        syn = [i for i in insights.rows(self.store, "Z") if i["stage"] == "synthesize"][0]
        self.assertEqual(json.loads(syn["support"])["unverified_numbers"], ["42%", "596"])

        bad = agents.write_tasks(self.store, self.run, "synthesize", "model:syn", root=self.root)
        (bad / "batch_01_result.json").write_text(json.dumps({"insights": [
            {"kind": "pattern", "statement": "x", "findings": [9999], "works": [2]}]}), encoding="utf-8")
        self.assertTrue(any("9999" in p for p in agents.import_task(bad, self.store)["problems"]))

        mine = insights.add(self.store, "Z", "human:tester", "manual", "question", "Is the SMFQ enough?", works=[1])
        insights.set_status(self.store, mine, "tested", "human:tester", "checked two more studies")
        insights.set_status(self.store, mine, "supported", "human:tester")
        with self.assertRaises(ValueError):
            insights.set_status(self.store, mine, "famous", "human:tester")
        log = insights.write_log(self.store, "Z", self.root / "notes" / "insights.md").read_text(encoding="utf-8")
        self.assertIn("## Supported (1)", log)
        self.assertIn("history: ", log)
        self.assertIn("checked two more studies", log)

        report = build_report(self.store, self.run)
        self.assertIn("## Findings", report)
        self.assertIn("## Insights", report)
        data = build_data(self.store, self.run)
        self.assertEqual(len(data["insights"]), 3)
        self.assertEqual(sum(len(r["findings"]) for r in data["records"]), 3)
        self.assertEqual(data["finding_fields"], ["direction", "note"])
