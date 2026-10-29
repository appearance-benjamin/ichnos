"""Search updates: the same search repeated, its new records on the sheet, and the pipeline's stages written for them
only, one after the other."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from ichnos import agents, paper, update
from ichnos.sources import SearchResult
from tests.test_agents_map import Base, PROTOCOL
from tests.test_eligibility import ELIGIBILITY


def fake_search(source, http, sent, cap, settings):
    recs = [{"source_id": f"U{i}", "doi": f"10.9/{i}", "title": f"Insomnia and mood in adolescents, cohort {i}",
             "abstract": "A longitudinal cohort of teens.", "year": 2026} for i in (1, 2, 50, 51)]
    return SearchResult(source, sent, 4, recs)


class Update(Base):
    with_eligibility = False

    def setUp(self):
        super().setUp()
        text = PROTOCOL.replace("sources = []", 'sources = ["openalex"]')
        if self.with_eligibility:
            text += ELIGIBILITY
        (self.root / "protocol" / "corpus_z.toml").write_text(text, encoding="utf-8")
        self.settings = SimpleNamespace()

    def search(self, day="2026-11-02"):
        with mock.patch.object(update, "search_source", fake_search):
            return update.search(self.store, self.run, self.settings, self.root, log=lambda *_: None, day=day)

    def done(self, status, decide=None, stage="title_abstract"):
        """Answer every task the step wrote: store the decisions (if any) and mark the folder imported."""
        for folder in status["written"]:
            meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
            for w in update._records(folder):
                if decide:
                    self.store.add_screening(self.run, w, "Z", stage, meta["agent"], decide(w))
            (folder / agents.IMPORTED).write_text(json.dumps({"at": "now"}), encoding="utf-8")

    def test_an_update_takes_only_its_new_records_through_the_stages(self):
        new = self.search()
        self.assertEqual(len(new), 2)                          # 1 and 2 merge by DOI with records the run has
        self.assertEqual(self.search(), [])                    # the same day again adds nothing
        self.assertEqual(update.added(self.store, self.run), {w: "2026-11-02" for w in new})
        row = self.store.db.execute("SELECT * FROM run_sources WHERE source = 'openalex@2026-11-02'").fetchone()
        self.assertEqual((row["fetched"], row["status"]), (4, "ok"))

        status = update.step(self.store, self.run, self.root)
        self.assertEqual(len(status["written"]), 2)             # two screeners
        self.assertTrue(all(update._records(f) == set(new) for f in status["written"]))
        self.assertEqual(status["waiting"], 2)
        again = update.step(self.store, self.run, self.root)    # open tasks are not written twice
        self.assertEqual((again["written"], len(again["open"])), ([], 2))

        keep, drop = new
        self.done(status, lambda w: "include" if w == keep else "exclude")
        status = update.step(self.store, self.run, self.root)
        if self.with_eligibility:
            self.assertEqual([json.loads((f / "task.json").read_text())["kind"] for f in status["written"]],
                             ["eligibility", "eligibility"])
            self.assertTrue(all(update._records(f) == {keep} for f in status["written"]))
            self.done(status, lambda w: "include", stage=agents.FULL_TEXT)
            status = update.step(self.store, self.run, self.root)
        # charting first: it decides the findings scope
        self.assertEqual([json.loads((f / "task.json").read_text())["kind"] for f in status["written"]], ["extract"])
        self.done(status)
        status = update.step(self.store, self.run, self.root)
        self.assertEqual([json.loads((f / "task.json").read_text())["kind"] for f in status["written"]], ["findings"])
        self.done(status)                                       # the study yields no finding: still done
        status = update.step(self.store, self.run, self.root)
        self.assertEqual((status["written"], status["waiting"]), ([], 0))
        self.assertTrue(all(s["done"] == s["records"] for s in status["stages"].values()))

        s = update.summary(self.store, self.run, self.root)
        d = s["days"][0]
        self.assertEqual((d["day"], d["added"], d["kept_title_abstract"], d["kept"], d["with_findings"]),
                         ("2026-11-02", 2, 1, 1, 0))
        self.assertIn("## Update of 2026-11-02", update.report(s))

        flow = paper.flow(self.store, self.run)
        self.assertIn("| Identification (update searches) | 4 records from the same search repeated on 2026-11-02 | "
                      "all but 2 already in the review |", flow)
        self.assertIn("| Identification | 4 records from databases (OpenAlex 4)", flow)   # the run's search as it was

    def test_without_an_update_nothing_is_written(self):
        status = update.step(self.store, self.run, self.root)
        self.assertEqual((status["added"], status["written"], status["waiting"]), ({}, [], 0))


class UpdateWithEligibility(Update):
    """The same, with an eligibility stage between title and abstract and the charting."""
    with_eligibility = True


SUPPLEMENT = '''
[[supplements]]
id = "design"
why = "to reach cohort studies worded otherwise"
query = """insomnia AND cohort"""
'''


class Supplement(Update):
    """A supplementary search with its own string: stored apart, through the same pipeline, reported as such."""

    def setUp(self):
        super().setUp()
        path = self.root / "protocol" / "corpus_z.toml"
        path.write_text(path.read_text(encoding="utf-8") + SUPPLEMENT, encoding="utf-8")

    def test_a_supplementary_search_is_stored_and_reported_apart(self):
        seen = []
        with mock.patch.object(update, "search_source", lambda *a: seen.append(a[2]) or fake_search(*a)):
            new = update.search(self.store, self.run, self.settings, self.root, log=lambda *_: None, day="2026-11-03",
                                supplement="design")
        self.assertEqual(seen, ["insomnia AND cohort"])          # its own string, not the run's
        self.assertEqual(update.added(self.store, self.run), {w: "2026-11-03+design" for w in new})
        self.assertEqual(update.supplement_of("openalex@2026-11-03+design"), "design")
        self.assertIsNone(update.supplement_of("openalex@2026-11-03"))
        status = update.step(self.store, self.run, self.root)
        self.assertTrue(all(update._records(f) == set(new) for f in status["written"]))
        flow = paper.flow(self.store, self.run)
        self.assertIn("| Identification (supplementary search) | 4 records from a supplementary search on 2026-11-03 "
                      "(design) | all but 2 already in the review |", flow)
        self.assertNotIn("update searches", flow)
        facts = " ".join(f["text"] for f in paper.method_facts(self.store, self.run, self.protocol_now()))
        self.assertIn("On 2026-11-03 a supplementary search was run in OpenAlex (to reach cohort studies worded "
                      "otherwise), with its own string (4 records retrieved); the 2 records new", facts)
        self.assertIn("Supplementary string: `insomnia AND cohort`", facts)
        self.assertNotIn("The search was updated", facts)
        self.assertIn("## Supplementary search design of 2026-11-03", update.report(update.summary(self.store, self.run,
                                                                                                    self.root)))
        with self.assertRaises(ValueError):
            update.search(self.store, self.run, self.settings, self.root, log=lambda *_: None, supplement="nope")

    def test_screen_all_takes_the_citation_search_records_through_the_pipeline(self):
        w = self.store.upsert_work({"title": "Insomnia in a teen cohort, cited", "year": 2021, "doi": "10.9/cited"})
        self.store.add_retrieval(self.run, w, "citation_search", "C1", 1)
        self.store.add_sample(self.run, [w], "pilot_screening")
        self.assertEqual(update.step(self.store, self.run, self.root)["written"], [])   # left unscreened by default
        path = self.root / "protocol" / "corpus_z.toml"
        path.write_text(path.read_text(encoding="utf-8") + "\n[mining]\nscreen_all = true\n", encoding="utf-8")
        status = update.step(self.store, self.run, self.root)
        self.assertEqual([update._records(f) for f in status["written"]], [{w}, {w}])

    def protocol_now(self):
        import tomllib
        return tomllib.loads((self.root / "protocol" / "corpus_z.toml").read_text(encoding="utf-8"))

    test_an_update_takes_only_its_new_records_through_the_stages = None
    test_without_an_update_nothing_is_written = None


if __name__ == "__main__":
    unittest.main()
