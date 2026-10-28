"""Signals, agent tasks (screen and extract), the evidence map and the review page, with a toy domain.

The toy protocol is deliberately not about work or meaning, to check that nothing depends on corpus A.
"""

import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from psyscientist import agents, mapping, signals
from psyscientist.report import build_report, write_report
from psyscientist.review import build_data
from psyscientist.store import Store

PROTOCOL = '''
corpus = "Z"
title = "Toy corpus: sleep and adolescent mood"
version = "test"
query = """("sleep duration" OR insomnia) AND (adolescen* OR teen*) AND (longitudinal OR cohort)"""
query_blocks = ["exposure", "population", "design"]
sources = []

[pilot]
max_records_per_source = 10
sample_size = 10
seed = 1

[screening]
stage = "title_abstract"
rule = "Include longitudinal studies of adolescent sleep and mood."
decisions = ["include", "exclude", "unsure"]
exclude_reasons = ["not_empirical", "review", "wrong_population", "cross_sectional", "other"]

[[screening.metrics]]
id = "empirical"
question = "Original data?"
fail_reason = "not_empirical"
review_reason = "review"

[[screening.metrics]]
id = "population"
question = "Adolescents?"
fail_reason = "wrong_population"

[[screening.metrics]]
id = "design"
question = "Longitudinal?"
fail_reason = "cross_sectional"

[map]
tree = ["country", "measure"]
match_across = "country"
match_on = ["measure"]

[[map.dimensions]]
name = "country"
question = "Where?"
categories = ["uk", "us", "nl", "other"]

[[map.dimensions]]
name = "measure"
question = "Which mood instrument?"

[findings]
question = "How does adolescent sleep relate to mood over time, and for whom?"
max_per_study = 3

[[findings.fields]]
name = "direction"
question = "Direction"
categories = ["better", "worse", "none"]

[[findings.fields]]
name = "note"
question = "Anything else"

[[claims]]
id = "two_process"
source = "Borbely"
locator = "1982"
claim = "Sleep pressure builds with time awake."
'''

RECORDS = [
    (1, "Sleep duration and mood in adolescents: a cohort study", "In a UK cohort, the SMFQ fell over three waves."),
    (2, "Insomnia in teens across the transition to high school", "Latent class growth models in US teens; trajectories differed by sex."),
    (3, "Sleep in adults: a review", "We review studies of adult sleep."),
    (4, "Adolescent insomnia and depressive symptoms", "Dutch adolescents completed the SMFQ at baseline and follow-up."),
]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "protocol").mkdir()
        (self.root / "protocol" / "corpus_z.toml").write_text(PROTOCOL, encoding="utf-8")
        self.protocol = tomllib.loads(PROTOCOL)
        self.store = Store(self.root / "evidence.sqlite")
        sha = self.store.add_protocol("Z", "protocol/corpus_z.toml", PROTOCOL)
        self.run = self.store.start_run("pilot", "Z", sha, {})
        for wid, title, abstract in RECORDS:
            w = self.store.upsert_work({"title": title, "abstract": abstract, "year": 2020, "doi": f"10.9/{wid}"})
            self.store.add_retrieval(self.run, w, "openalex", str(wid), wid)
        self.store.add_sample(self.run, [1, 2, 3, 4], "pilot_screening")
        self.store.set_run_source(self.run, "openalex", "q", 4, 4, "ok")
        self.store.finish_run(self.run)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def answer(self, folder: Path, make):
        for batch in sorted(folder.glob("batch_[0-9][0-9].json")):
            records = json.loads(batch.read_text(encoding="utf-8"))
            batch.with_name(batch.stem + "_result.json").write_text(
                json.dumps([make(r) for r in records]), encoding="utf-8")


class Signals(unittest.TestCase):
    def test_levels_are_domain_general(self):
        lex = signals.lexicons(tomllib.loads(PROTOCOL))
        self.assertIn("query:exposure", lex)
        strong = signals.summary("Insomnia trajectories", "Growth mixture models over four waves.", lex)
        moderate = signals.summary("Sleep and mood", "A cohort study; effects were moderated by sex.", lex)
        none = signals.summary("Sleep and mood", "A cross-sectional survey.", lex)
        self.assertEqual((strong["level"], moderate["level"], none["level"]), ("strong", "moderate", "none"))
        self.assertTrue(none["cross_sectional"])

    def test_protocol_can_add_terms(self):
        p = tomllib.loads(PROTOCOL)
        p["signals"] = {"add": {"trajectory": ["sleep debt accumulat*"]}}
        s = signals.summary("", "Sleep debt accumulated over term.", signals.lexicons(p))
        self.assertEqual(s["level"], "strong")


class Decisions(unittest.TestCase):
    metrics = tomllib.loads(PROTOCOL)["screening"]["metrics"]

    def test_first_zero_in_protocol_order_excludes(self):
        self.assertEqual(agents.decide({"empirical": 2, "population": 0, "design": 0}, "study", self.metrics),
                         ("exclude", "wrong_population"))
        self.assertEqual(agents.decide({"empirical": 0, "population": 2, "design": 2}, "review", self.metrics),
                         ("exclude", "review"))
        self.assertEqual(agents.decide({"empirical": 2, "population": 2, "design": 2}, "study", self.metrics),
                         ("include", None))
        self.assertEqual(agents.decide({"empirical": 2, "population": 1, "design": 2}, "study", self.metrics),
                         ("unsure", None))

    def test_grounding(self):
        text = "Latent class growth models in US teens; trajectories differed by sex."
        self.assertTrue(agents.grounded("trajectories differed by sex", text))
        self.assertTrue(agents.grounded("Latent class ... differed by sex", text))
        self.assertTrue(agents.grounded("Latent class | differed by sex", text))         # quotes joined by ' | '
        self.assertFalse(agents.grounded("Latent class | differed by age", text))
        self.assertFalse(agents.grounded("trajectories differed by age", text))
        self.assertIsNone(agents.grounded("", text))

    def test_grounding_keeps_text_between_comparison_signs(self):
        text = "Groups differed (p < .001). A hierarchical regression was run. Reliability was good (α > .80)."
        self.assertTrue(agents.grounded("A hierarchical regression was run", text))
        self.assertTrue(agents.grounded("Meaning <i>after</i> work", "Meaning after work"))


class TaskRoundTrip(Base):
    def screen(self):
        folder = agents.write_tasks(self.store, self.run, "screen", "model:test", root=self.root, batch_size=3)
        self.assertEqual(len(list(folder.glob("batch_[0-9][0-9].json"))), 2)
        text = (folder / "instructions.md").read_text(encoding="utf-8")
        self.assertIn("`population`: Adolescents?", text)

        def make(r):
            adult = "adults" in r["title"]
            return {"work_id": r["work_id"], "kind": "review" if adult else "study",
                    "scores": {"empirical": {"score": 0 if adult else 2, "evidence": r["title"][:20]},
                               "population": {"score": 0 if adult else 2, "evidence": "invented words"},
                               "design": {"score": 1, "evidence": ""}},
                    "note": "toy"}
        self.answer(folder, make)
        return folder, agents.import_task(folder, self.store)

    def test_screen_import_derives_decisions_and_checks_quotes(self):
        _, counts = self.screen()
        self.assertEqual(counts["problems"], [])
        self.assertEqual(counts["stored"], 4)
        self.assertEqual((counts["evidence"], counts["grounded"]), (8, 4))
        rows = dict(self.store.db.execute("SELECT work_id, decision FROM screening WHERE screener = 'model:test'"))
        self.assertEqual(rows, {1: "unsure", 2: "unsure", 3: "exclude", 4: "unsure"})
        reason = self.store.db.execute("SELECT reason FROM screening WHERE work_id = 3").fetchone()[0]
        self.assertEqual(reason, "review")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM calls WHERE kind = 'llm'").fetchone()[0], 2)

    def test_answered_task_folders_are_never_overwritten(self):
        first, _ = self.screen()
        second = agents.write_tasks(self.store, self.run, "screen", "model:test", root=self.root, work_ids=[1])
        self.assertNotEqual(first, second)
        self.assertTrue(second.name.endswith("__2"))
        self.assertTrue(any(first.glob("batch_*_result.json")))

    def test_bad_answers_store_nothing(self):
        folder = agents.write_tasks(self.store, self.run, "screen", "model:bad", root=self.root)
        self.answer(folder, lambda r: {"work_id": r["work_id"], "scores": {"empirical": {"score": 3}}})
        counts = agents.import_task(folder, self.store)
        self.assertTrue(counts["problems"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM screening").fetchone()[0], 0)

    def test_extract_map_matches_gaps_and_review(self):
        self.screen()
        self.assertEqual(agents.kept_ids(self.store, self.run), [1, 2, 4])
        folder = agents.write_tasks(self.store, self.run, "extract", "model:extractor", root=self.root)
        self.assertIn("two_process", (folder / "instructions.md").read_text(encoding="utf-8"))
        countries = {1: "uk", 2: "us", 4: "nl"}
        measures = {1: "smfq", 2: "not_reported", 4: "SMFQ"}

        def make(r):
            w = r["work_id"]
            return {"work_id": w, "features": {
                "country": {"value": countries[w], "canonical": countries[w], "evidence": ""},
                "measure": {"value": measures[w], "canonical": measures[w], "evidence": "SMFQ" if w != 2 else ""}},
                "claims": [{"claim": "two_process", "relation": "supports", "evidence": "baseline and follow-up",
                            "reason": "toy"}] if w == 4 else []}
        self.answer(folder, make)
        counts = agents.import_task(folder, self.store)
        self.assertEqual(counts["problems"], [])

        m = mapping.build(self.store, self.run, self.protocol)
        self.assertEqual(m.matches, [{"on": {"measure": "smfq"}, "studies": {"nl": [4], "uk": [1]}, "n_studies": 2}])
        self.assertEqual(m.gaps, [{"on": {"measure": "smfq"}, "present": {"nl": 1, "uk": 1}, "missing": ["us"]}])
        self.assertEqual(m.matrix["measure"]["cells"]["us"], {"not_reported": 1})
        self.assertEqual(m.claims[0]["links"]["supports"][0]["work_id"], 4)

        data = build_data(self.store, self.run)
        self.assertEqual(len(data["records"]), 4)
        self.assertEqual(data["records"][1]["level"], "strong")
        self.assertIn("model:test", data["screeners"])
        self.assertEqual(data["map"]["matches"][0]["on"], {"measure": "smfq"})
        out = self.root / "report.md"
        write_report(self.store, self.run, out)
        page = (self.root / "review.html").read_text(encoding="utf-8")
        self.assertIn('"corpus": "Z"', page)
        self.assertNotIn("</script>\n<script>\n\"use strict\";\n</script>", page)
        report = build_report(self.store, self.run)
        self.assertIn("## Trajectory signal", report)
        self.assertIn("## Metric scores", report)
        self.assertIn("**Matches across country** (same measure): 1", report)

    def test_extract_rejects_unknown_category(self):
        self.screen()
        folder = agents.write_tasks(self.store, self.run, "extract", "model:x", root=self.root)
        self.answer(folder, lambda r: {"work_id": r["work_id"], "features": {
            "country": {"value": "france", "canonical": "france", "evidence": ""},
            "measure": {"value": "not_reported", "canonical": "not_reported", "evidence": ""}}})
        self.assertTrue(any("not in" in p for p in agents.import_task(folder, self.store)["problems"]))

    def test_a_dimension_added_later_is_charted_alone(self):
        self.screen()
        full = agents.write_tasks(self.store, self.run, "extract", "model:x", root=self.root)
        self.answer(full, lambda r: {"work_id": r["work_id"], "features": {
            "country": {"value": "uk", "canonical": "uk", "evidence": ""},
            "measure": {"value": "SMFQ", "canonical": "smfq", "evidence": "SMFQ"}}})
        self.assertEqual(agents.import_task(full, self.store)["problems"], [])
        folder = agents.write_tasks(self.store, self.run, "extract", "model:x", root=self.root, work_ids=[1],
                                    options={"dims": "measure"})
        text = (folder / "instructions.md").read_text(encoding="utf-8")
        self.assertIn("`measure`", text)
        self.assertNotIn("`country`", text)
        self.assertNotIn("two_process", text)                 # claims belong to a full charting pass
        self.assertEqual(json.loads((folder / "task.json").read_text(encoding="utf-8"))["dims"], ["measure"])
        self.answer(folder, lambda r: {"work_id": r["work_id"], "features": {
            "measure": {"value": "CES-D", "canonical": "ces-d", "evidence": ""}}})
        self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        rows = dict(self.store.db.execute("SELECT dimension, canonical FROM study_features WHERE work_id = 1"))
        self.assertEqual(rows, {"country": "uk", "measure": "ces-d"})
        with self.assertRaises(ValueError):
            agents.write_tasks(self.store, self.run, "extract", "model:x", root=self.root, options={"dims": "mood"})


if __name__ == "__main__":
    unittest.main()
