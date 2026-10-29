"""The eligibility stage: stricter criteria for the records kept at title and abstract."""

import json

from ichnos import agents, paper
from tests.test_agents_map import PROTOCOL, Base

ELIGIBILITY = '''
[eligibility]
rule = "Keep only studies with a mood outcome measured twice."
exclude_reasons = ["not_empirical", "review", "wrong_population", "cross_sectional", "one_wave", "other"]

[[eligibility.metrics]]
id = "design"
question = "Mood measured at two or more waves?"
fail_reason = "one_wave"
'''


class EligibilityStage(Base):
    def setUp(self):
        super().setUp()
        (self.root / "protocol" / "corpus_z.toml").write_text(PROTOCOL + ELIGIBILITY, encoding="utf-8")
        for w in (1, 2, 4):
            self.store.add_screening(self.run, w, "Z", "title_abstract", "model:s", "include")
        self.store.add_screening(self.run, 3, "Z", "title_abstract", "model:s", "exclude", "review")

    def test_stage_narrows_the_kept_records(self):
        self.assertEqual(agents.kept_ids(self.store, self.run), [1, 2, 4])
        folder = agents.write_tasks(self.store, self.run, "eligibility", "model:e", root=self.root)
        text = (folder / "instructions.md").read_text(encoding="utf-8")
        self.assertIn("Eligibility task", text)
        self.assertIn("mood outcome measured twice", text)
        records = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertEqual([r["work_id"] for r in records], [1, 2, 4])
        scores = {1: 2, 2: 2, 4: 0}
        self.answer(folder, lambda r: {"work_id": r["work_id"], "kind": "study", "scores": {
            "design": {"score": scores[r["work_id"]], "evidence": r["title"][:20] if scores[r["work_id"]] != 1 else ""}}})
        counts = agents.import_task(folder, self.store)
        self.assertEqual(counts["problems"], [])
        self.assertEqual(agents.kept_ids(self.store, self.run), [1, 2])
        self.assertEqual(agents.kept_ids(self.store, self.run, "title_abstract"), [1, 2, 4])
        # its scores do not overwrite the title-and-abstract scores of the same metric id
        metrics = {r[0] for r in self.store.db.execute("SELECT metric FROM screening_scores")}
        self.assertEqual(metrics, {"eligibility:design"})
        self.store.add_screening(self.run, 4, "Z", "full_text", "human:ann", "include")   # a human's call wins
        self.assertEqual(agents.kept_ids(self.store, self.run), [1, 2, 4])
        facts = " ".join(f["text"] for f in paper.method_facts(self.store, self.run, self.protocol | {
            "eligibility": {"rule": "Keep only studies with a mood outcome measured twice."}}))
        self.assertIn("eligibility stage", facts)
        self.store.add_screening(self.run, 4, "Z", "full_text", "human:ann", "exclude", "one_wave")
        self.assertIn("| Eligibility | 3 records assessed against stricter criteria | 1 excluded (one wave 1) |",
                      paper.flow(self.store, self.run))
