"""Recoding existing findings keeps their ids and records every change."""

import json

from ichnos import agents
from tests.test_agents_map import Base
from tests.test_findings_insights import FindingsAndInsights


class Recode(FindingsAndInsights):
    def test_recode_keeps_ids_and_logs_changes(self):
        self.findings_task()
        before = {r["finding_id"]: json.loads(r["fields"]) for r in
                  self.store.db.execute("SELECT finding_id, fields FROM findings")}
        folder = agents.write_tasks(self.store, self.run, "recode", "model:r", root=self.root)
        batch = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertEqual(sorted(b["id"] for b in batch), sorted(before)[:len(batch)])
        self.assertIn("Y is the outcome and X the predictor", (folder / "instructions.md").read_text(encoding="utf-8"))
        bad = {"findings": [{"id": b["id"], "fields": {"direction": "sideways", "note": ""}} for b in batch]}
        (folder / "batch_01_result.json").write_text(json.dumps(bad), encoding="utf-8")
        self.assertTrue(agents.import_task(folder, self.store)["problems"])

        folder = agents.write_tasks(self.store, self.run, "recode", "model:r", root=self.root)
        first = batch[0]["id"]
        answer = {"findings": [{"id": b["id"], "fields": {"direction": "worse" if b["id"] == first else
                                                          b["fields"]["direction"], "note": b["fields"]["note"]},
                                "changed": ["direction"] if b["id"] == first else [], "reason": "passage says worse"}
                               for b in batch]}
        (folder / "batch_01_result.json").write_text(json.dumps(answer), encoding="utf-8")
        counts = agents.import_task(folder, self.store)
        self.assertEqual((counts["problems"], counts["changed"]), ([], 1))
        row = self.store.db.execute("SELECT fields FROM findings WHERE finding_id = ?", (first,)).fetchone()
        self.assertEqual(json.loads(row["fields"])["direction"], "worse")
        log = self.store.db.execute("SELECT field, old, new, reason FROM finding_recodes").fetchall()
        self.assertEqual([tuple(r) for r in log], [("direction", before[first]["direction"], "worse",
                                                    "passage says worse")])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM findings").fetchone()[0], len(before))

    def test_recode_of_one_field_leaves_the_others(self):
        self.findings_task()
        before = {r["finding_id"]: json.loads(r["fields"]) for r in
                  self.store.db.execute("SELECT finding_id, fields FROM findings")}
        folder = agents.write_tasks(self.store, self.run, "recode", "model:r", root=self.root,
                                    options={"dims": "direction"})
        text = (folder / "instructions.md").read_text(encoding="utf-8")
        self.assertIn("`direction`", text)
        self.assertNotIn("`note`", text)
        batch = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        answer = {"findings": [{"id": b["id"], "fields": {"direction": "worse"}} for b in batch]}
        (folder / "batch_01_result.json").write_text(json.dumps(answer), encoding="utf-8")
        self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        for b in batch:
            row = json.loads(self.store.db.execute("SELECT fields FROM findings WHERE finding_id = ?",
                                                   (b["id"],)).fetchone()["fields"])
            self.assertEqual(row, {**before[b["id"]], "direction": "worse"})


class SecondCoderOnFindings(Base):
    def test_a_blind_second_coding_of_findings_is_kept_apart(self):
        import json as _json
        from ichnos import agents, recode
        from ichnos.store import now as _now
        ids = []
        for w, d in ((1, "better"), (2, "worse")):
            cur = self.store.db.execute("INSERT INTO findings (work_id, run_id, extractor, finding, fields, evidence, "
                                        "status, created_at) VALUES (?, ?, 'model:x', 'f', ?, 'p', 'extracted', ?)",
                                        (w, self.run, _json.dumps({"direction": d}), _now()))
            ids.append(cur.lastrowid)
        self.store.db.commit()
        folder = agents.write_tasks(self.store, self.run, "recode", "model:second", root=self.root,
                                    options={"dims": "direction", "second_coder": True})
        batch = _json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertNotIn("direction", batch[0]["fields"])                      # blind to the first codes
        (folder / "batch_01_result.json").write_text(_json.dumps({"findings": [
            {"id": ids[0], "fields": {"direction": "better"}}, {"id": ids[1], "fields": {"direction": "better"}}]}),
            encoding="utf-8")
        self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        kept = {r[0]: _json.loads(r[1])["direction"] for r in self.store.db.execute(
            "SELECT finding_id, fields FROM findings WHERE finding_id IN (?, ?)", ids)}
        self.assertEqual(kept, {ids[0]: "better", ids[1]: "worse"})          # the findings are untouched
        agree = recode.field_agreement(self.store, self.run, ["direction"])["direction"]
        self.assertEqual((agree["n"], agree["agree"]), (2, 1))
