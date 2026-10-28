"""Full manuscripts: generated method facts, sentence checks, audit, peer review, editor, revision."""

import json

from psyscientist import agents, paper
from psyscientist.review import build_data
from tests.test_findings_insights import FindingsAndInsights

BACKGROUND = '''
[paper]
authors = ["Test Author"]
kind = "scoping review"

[[paper.core]]
id = "empirical"
metric = "empirical"
fails = "not an original study"

[[background]]
id = "B1"
cite = "Doe, 2020"
reference = "Doe, J. (2020). A book. Press."
claim = "Sleep matters for 42% of mood."
checked = "test"

[[background]]
id = "B11"
cite = "Arksey & O'Malley, 2005"
reference = "Arksey, H., & O'Malley, L. (2005). Scoping studies."
claim = "Scoping reviews map research."
checked = "test"
'''


def sentence(text, **kw):
    return dict({"text": text}, **kw)


class Papers(FindingsAndInsights):
    def setUp(self):
        super().setUp()
        p = self.root / "protocol" / "corpus_z.toml"
        p.write_text(p.read_text(encoding="utf-8") + BACKGROUND, encoding="utf-8")

    def manuscript(self, f1, results_text="Mood improved as sleep lengthened."):
        return {"title": "Sleep and mood: a scoping review",
                "abstract": [{"sentences": [sentence("Two studies were charted.", method=["M1"]),
                                            sentence(results_text, cites=[f1])]}],
                "keywords": ["sleep", "mood"],
                "sections": [
                    {"name": "Introduction", "paragraphs": [{"sentences": [
                        sentence("Sleep matters for 42% of mood.", background=["B1"]),
                        sentence("We map the evidence.", frame=True)]}]},
                    {"name": "Results", "paragraphs": [{"sentences": [
                        sentence(results_text, cites=[f1]),
                        sentence("Every retained record is listed.", tables=["S1"]),
                        sentence("Background alone is not a result.", background=["B1"])]}]},
                    {"name": "Discussion", "paragraphs": [{"sentences": [
                        sentence("This review was AI-assisted; 70 of 90 claims are invented.", method=["M1"])]}]},
                    {"name": "Conclusion", "paragraphs": [{"sentences": [sentence("More work is needed.", cites=[f1])]}]}],
                "insights": []}

    def answer(self, folder, obj):
        (folder / "batch_01_result.json").write_text(json.dumps(obj), encoding="utf-8")
        return agents.import_task(folder, self.store)

    def test_full_cycle(self):
        self.findings_task()
        folder = agents.write_tasks(self.store, self.run, "paper", "model:writer", root=self.root)
        data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertTrue(data["method_facts"][0]["id"] == "M1")
        self.assertIn("### Information sources and search", data["method_section"])
        self.assertIn("the completed checklist is Table S", data["method_section"])
        self.assertIn("**Table S2.**", " ".join(t["text"] for t in data["tables"]))
        self.assertTrue(any("form the core" in f["text"] for f in data["method_facts"]))
        f1 = data["findings"][0]["id"]
        counts = self.answer(folder, self.manuscript(f1))
        self.assertEqual(counts["problems"], [])
        t = counts["verification"]
        self.assertEqual((t["frame"], t["flagged"]), (1, 2))   # background-only result; invented numbers
        pid = counts["paper"]
        anew = agents.write_tasks(self.store, self.run, "paper", "model:writer", root=self.root, options={"base": pid})
        self.assertIn("# Sleep and mood", json.loads((anew / "batch_01.json").read_text(encoding="utf-8"))["previous_version"])
        self.assertIn("## The previous version", (anew / "instructions.md").read_text(encoding="utf-8"))
        import shutil
        shutil.rmtree(anew)                                    # not answered here; the cycle goes on with the first
        text = paper.render(self.store, pid, full=True)
        self.assertIn("## 2. Method", text)
        self.assertIn("**Declaration of AI use:**", text)
        self.assertIn("Doe, J. (2020)", text)
        self.assertIn("Arksey, H.", text)
        self.assertIn("## Appendix: verification", text)
        self.assertNotIn("## Appendix", paper.render(self.store, pid, full=False))

        self.assertIn("(Table S1)", text)
        audit = agents.write_tasks(self.store, self.run, "audit", "model:auditor", root=self.root)
        abatch = json.loads((audit / "batch_01.json").read_text(encoding="utf-8"))
        items = abatch["sentences"]
        self.assertTrue(any(c.get("passage") for i in items for c in i["cited"]))
        self.assertIn("**Table S1.**", abatch["tables"]["S1"])
        self.assertTrue(any(c.get("study", {}).get("type") for i in items for c in i["cited"]))
        self.assertEqual(self.answer(audit, {"verdicts": [{"id": i["id"], "verdict": "faithful", "reason": "ok"}
                                                          for i in items]})["problems"], [])

        bad = agents.write_tasks(self.store, self.run, "peer", "model:r", root=self.root, options={"persona": "critical"})
        self.assertTrue(self.answer(bad, {"overall": 11})["problems"])
        reviews = []
        for persona in ("methodologist", "critical"):
            f = agents.write_tasks(self.store, self.run, "peer", "model:r", root=self.root, options={"persona": persona})
            self.assertIn("## Abstract", json.loads((f / "batch_01.json").read_text(encoding="utf-8"))["manuscript"])
            review = {k: 3 for k in paper.RUBRIC}
            review.update(overall=5, confidence=4, decision="major_revision", summary="s", strengths=["a"],
                          weaknesses=["b"], questions=[], requested_changes=[{"change": "c", "severity": "major"}])
            reviews.append(self.answer(f, review))
        self.assertTrue(all(r["problems"] == [] for r in reviews))

        meta = agents.write_tasks(self.store, self.run, "meta", "model:editor", root=self.root)
        mdata = json.loads((meta / "batch_01.json").read_text(encoding="utf-8"))
        self.assertEqual(len(mdata["reviews"]), 2)
        self.assertEqual(mdata["audit"]["verdicts"], {"faithful": len(items)})
        self.assertEqual(self.answer(meta, {"summary": "s", "decision": "major_revision", "scores": {"overall": 5},
                                            "required_changes": [{"id": "R1", "change": "fix numbers",
                                                                  "severity": "major"}]})["problems"], [])

        rev = agents.write_tasks(self.store, self.run, "revise", "model:writer", root=self.root)
        v2 = self.manuscript(f1)
        v2["sections"][2]["paragraphs"][0]["sentences"][0] = sentence("This review was AI-assisted.", method=["M1"])
        self.assertTrue(self.answer(rev, dict(v2))["problems"])     # no response to R1
        rev = agents.write_tasks(self.store, self.run, "revise", "model:writer", root=self.root)
        v2["response"] = [{"change": "R1", "action": "done", "response": "Removed.", "where": "Discussion"}]
        counts = self.answer(rev, v2)
        self.assertEqual(counts["problems"], [])
        self.assertEqual(counts["verification"]["flagged"], 1)
        versions = paper.overview(self.store, self.run)
        self.assertEqual([v["version"] for v in versions], [1, 2])
        self.assertEqual(versions[1]["parent"], pid)
        self.assertIn("## Response to reviewers", paper.render(self.store, versions[1]["id"]))

        final = agents.write_tasks(self.store, self.run, "meta", "model:editor", root=self.root)
        fdata = json.loads((final / "batch_01.json").read_text(encoding="utf-8"))
        self.assertIn("response_to_reviewers", fdata)
        self.assertEqual(json.loads((final / "task.json").read_text())["round"], 2)

        page = build_data(self.store, self.run)
        self.assertEqual(len(page["papers"]), 2)
        self.assertIn("# Sleep and mood", page["papers"][0]["text"])


class PotentialCore(FindingsAndInsights):
    """[paper] core_needs_fulltext: core only when read in full; on the abstract alone, a potential core study."""

    def setUp(self):
        super().setUp()
        p = self.root / "protocol" / "corpus_z.toml"
        p.write_text(p.read_text(encoding="utf-8") + BACKGROUND, encoding="utf-8")

    def test_a_study_read_only_as_an_abstract_is_a_potential_core_study(self):
        import tomllib
        from psyscientist import library, tables
        self.findings_task()
        reads = self.store.db.execute("SELECT read_from, count(*) FROM findings_reads WHERE run_id = ? GROUP BY "
                                      "read_from", (self.run,)).fetchall()
        self.assertTrue(reads and reads[0][1])          # every record a findings task read, also one with none
        for w in agents.kept_ids(self.store, self.run):
            self.store.add_score(self.run, w, "model:s", "empirical", 2, "a cohort", 1)
        protocol = tomllib.loads((self.root / "protocol" / "corpus_z.toml").read_text(encoding="utf-8"))
        core = sorted(e["work_id"] for e in tables.eligibility(self.store, self.run, protocol) if e["core"])
        self.assertTrue(core)
        protocol["paper"]["core_needs_fulltext"] = True
        rows = {e["work_id"]: e for e in tables.eligibility(self.store, self.run, protocol)}
        self.assertEqual([w for w in core if rows[w]["core"]], [])           # none of them was read in full
        self.assertTrue(all(rows[w]["potential"] and rows[w]["group"] == tables.POTENTIAL for w in core))
        todo = library.todo(self.store, self.run, protocol)
        first = [r for r in todo if r["group"] == 1]
        self.assertEqual(sorted(r["work_id"] for r in first if r["potential"]), core)
        self.assertEqual(library.group_label(1, todo), library.POTENTIAL)
        w = core[0]
        self.store.set_fulltext(w, "parsed", "library", sections=[{"heading": "Results", "text": "Mood rose. " * 300}])
        rows = {e["work_id"]: e for e in tables.eligibility(self.store, self.run, protocol)}
        self.assertTrue(rows[w]["potential"])                 # the PDF is in, but the study was not charted from it
        self.store.db.execute("INSERT INTO study_features (work_id, extractor, dimension, value, canonical, evidence, "
                              "grounded, run_id, created_at) VALUES (?, 'model:x', 'design', 'cohort', 'cohort', 'q', "
                              "1, ?, '2999-01-01T00:00:00+00:00')", (w, self.run))
        rows = {e["work_id"]: e for e in tables.eligibility(self.store, self.run, protocol)}
        self.assertTrue(rows[w]["core"] and not rows[w]["potential"])                # charted from it: core
        # read in full, it reports no results of its own: not an original empirical study
        protocol["paper"]["core"][0]["no_results_fails"] = True
        self.store.add_findings_read(self.run, w, "model:x", "full text", 0, at="3001-01-01T00:00:00+00:00")
        rows = {e["work_id"]: e for e in tables.eligibility(self.store, self.run, protocol)}
        self.assertEqual((rows[w]["core"], rows[w]["group"]), (False, "not an original study"))
        self.store.add_findings_read(self.run, w, "model:x", "full text", 3, at="3002-01-01T00:00:00+00:00")
        self.assertTrue({e["work_id"]: e for e in tables.eligibility(self.store, self.run, protocol)}[w]["core"])
        # a conference abstract counts as an abstract, whatever text was found: its own set, nothing to download
        protocol["paper"]["conference_abstracts"] = [tables.works(self.store, [w])[w]["doi"]]
        rows = {e["work_id"]: e for e in tables.eligibility(self.store, self.run, protocol)}
        self.assertEqual((rows[w]["core"], rows[w]["potential"], rows[w]["group"]), (False, False, tables.ABSTRACT_ONLY))
        self.assertNotIn(w, {r["work_id"] for r in library.todo(self.store, self.run, protocol)})
        del protocol["paper"]["conference_abstracts"]
        self.store.set_fulltext(w, "parsed", "library", sections=[{"heading": "Abstract", "text": "Mood rose."}])
        self.store.db.execute("UPDATE study_features SET created_at = '3000-01-01T00:00:00+00:00' WHERE work_id = ?",
                              (w,))
        rows = {e["work_id"]: e for e in tables.eligibility(self.store, self.run, protocol)}
        self.assertTrue(rows[w]["potential"])                 # a text of a few words is not the study
        self.assertIn(w, {r["work_id"] for r in library.todo(self.store, self.run, protocol)})
        self.assertIn("potential core study", tables.eligibility_table(list(rows.values()), protocol))
