"""The evidence-first writer: input, sentence checks, storage and rendering."""

import json

from ichnos import agents, insights, writer
from tests.test_findings_insights import FindingsAndInsights


class Writer(FindingsAndInsights):
    def test_draft_sentences_are_checked_one_by_one(self):
        self.findings_task()
        dropped = insights.add(self.store, "Z", "human:t", "manual", "gap", "Irrelevant.")
        insights.set_status(self.store, dropped, "dropped", "human:t")
        folder = agents.write_tasks(self.store, self.run, "write", "model:w", root=self.root)
        data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertNotIn(dropped, [i["id"] for i in data["insights"]])
        self.assertTrue(all("evidence" in f for f in data["findings"]))
        self.assertIn("Every number in a sentence must appear", (folder / "instructions.md").read_text(encoding="utf-8"))
        f1, f2 = data["findings"][0]["id"], data["findings"][1]["id"]
        answer = {"title": "Sleep and mood", "paragraphs": [
            {"heading": "Results", "sentences": [
                {"text": "Two studies speak to the question.", "frame": True},
                {"text": "Mood improved as sleep lengthened.", "cites": [f1]},
                {"text": "Trajectories differed by sex in 42% of teens.", "cites": [f2]},
                {"text": "Nothing else is known.", "cites": []},
                {"text": "As 3 of 12 cohorts show.", "frame": True},
                {"text": "A dropped idea.", "cites": [f1], "insights": [dropped]}]}],
            "insights": []}
        (folder / "batch_01_result.json").write_text(json.dumps(answer), encoding="utf-8")
        counts = agents.import_task(folder, self.store)
        self.assertEqual(counts["problems"], [])
        t = counts["verification"]
        self.assertEqual((t["sentences"], t["verified"], t["frame"], t["flagged"]), (6, 1, 2, 4))
        d = writer.latest(self.store, self.run)
        problems = [c["problems"] for c in json.loads(d["verification"])["sentences"]]
        self.assertEqual(problems[0], [])
        self.assertIn("numbers not in the cited findings: 42%", problems[2][0])
        self.assertIn("cites no finding", problems[3][0])
        self.assertIn("contains numbers", problems[4][0])
        self.assertIn("dropped", problems[5][0])

        md = writer.render_markdown(self.store, d["draft_id"])
        self.assertIn("# Sleep and mood", md)
        self.assertIn("Mood improved as sleep lengthened (Anonymous, 2020).", md)
        self.assertIn("## References", md)
        self.assertIn("## Verification", md)
        self.assertIn("**[⚠]**", md)

    def test_malformed_draft_is_rejected(self):
        self.findings_task()
        folder = agents.write_tasks(self.store, self.run, "write", "model:w", root=self.root)
        (folder / "batch_01_result.json").write_text(json.dumps({"paragraphs": []}), encoding="utf-8")
        self.assertTrue(agents.import_task(folder, self.store)["problems"])

    def test_cite_labels(self):
        self.assertEqual(writer.cite_label('["Jane Doe"]', 2019), "Doe, 2019")
        self.assertEqual(writer.cite_label('["Jane Doe", "Max Roe"]', 2019), "Doe & Roe, 2019")
        self.assertEqual(writer.cite_label('["A B", "C D", "E F"]', None), "B et al., n.d.")
        self.assertEqual(writer.cite_label('["Janske van Eersel", "X Y"]', 2024), "van Eersel & Y, 2024")
        self.assertEqual(writer.cite_label('["Ludwig van der Berg"]', 2020), "van der Berg, 2020")
