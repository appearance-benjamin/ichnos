"""Pre-registered patterns: checked before the coding, counted by code, judged on the studies they were not drawn from."""

import tomllib
import unittest

from psyscientist import insights, patterns
from psyscientist.config import ROOT
from psyscientist.store import now
from tests.test_agents_map import Base, PROTOCOL

SHARE = {"id": "P1", "name": "UK mostly", "statement": "Most coded studies are from the UK.",
         "expect": {"dimension": "country", "values": ["uk"]}, "holds": [0.5, 1.0],
         "predictions": {"theory": "holds", "placebo": "none"}}
CONTRAST = {"id": "P2", "name": "UK better", "statement": "Better more often in the UK than in the US.", "level": "finding",
            "a": [{"dimension": "country", "values": ["uk"]}], "b": [{"dimension": "country", "values": ["us"]}],
            "expect": {"dimension": "direction", "values": ["better"]}, "holds": [0.2, 1.0],
            "predictions": {"theory": "fails", "placebo": "none"}}


class Check(unittest.TestCase):
    def setUp(self):
        self.protocol = tomllib.loads(PROTOCOL)

    def test_patterns_written_against_the_coding_pass(self):
        self.assertEqual(patterns.check([SHARE, CONTRAST], self.protocol, ["theory", "placebo"]), [])

    def test_what_cannot_be_counted_is_named(self):
        bad = [dict(SHARE, expect={"dimension": "country", "values": ["fr"]}),
               dict(SHARE, id="P3", expect={"dimension": "measure", "values": ["x"]}),
               dict(SHARE, id="P4", expect={"dimension": "direction", "values": ["better"]}),
               dict(SHARE, id="P5", holds=[0.8, 0.2]),
               dict(CONTRAST, id="P6", b=[]),
               dict(SHARE, id="P7", predictions={"theory": "maybe"})]
        errs = "\n".join(patterns.check(bad, self.protocol, ["theory", "placebo"]))
        self.assertIn("P1 expect: country has no category ['fr']", errs)
        self.assertIn("P3 expect: measure has no categories to count", errs)
        self.assertIn('P4 expect: direction is a findings field; use level = "finding"', errs)
        self.assertIn("P5: holds must be", errs)
        self.assertIn("P6: a contrast needs groups", errs)
        self.assertIn("P7: the prediction of theory must be one of", errs)
        self.assertIn("P7: no prediction from ['placebo']", errs)

    def test_the_corpus_c_patterns_and_comparator_predictions_can_be_counted(self):
        self.assertEqual(patterns.validate("C", ROOT), [])

    def test_the_approval_tables_do_not_swallow_the_search(self):
        from psyscientist.config import load_protocol
        for corpus in ("C", "D"):
            p = load_protocol(corpus, ROOT)[2]
            self.assertTrue(p.get("query") and p.get("sources"), corpus)
            self.assertEqual(set(p["approval"]) - {"by", "date", "version", "how", "scope", "note"}, set(), corpus)


class Judge(unittest.TestCase):
    S = {"min_n": 3, "min_group": 2}

    def u(self, w, **values):
        return {"works": [w], "values": {k: set(v) for k, v in values.items()}}

    def test_a_pattern_is_judged_on_the_studies_it_was_not_drawn_from(self):
        units = [self.u(1, country=["uk"]), self.u(2, country=["uk"]), self.u(3, country=["us"]),
                 self.u(4, country=["uk"]), self.u(5, country=["uk"]), self.u(6, country=["us"]), self.u(7)]
        [r] = patterns.judge(units, [dict(SHARE, _discovery=[1, 2])], self.S)
        self.assertEqual((r["test"]["n"], round(r["test"]["value"], 2)), (4, 0.5))   # 3-6; 7 is not coded
        self.assertEqual((r["discovery"]["n"], r["discovery"]["value"]), (2, 1.0))
        self.assertEqual(r["verdict"], "holds")
        self.assertEqual(r["lenses"], {"theory": "right", "placebo": "no prediction"})
        [r] = patterns.judge(units, [dict(SHARE, _discovery=[1, 2, 3, 4, 5, 6])], self.S)
        self.assertEqual(r["verdict"], "too few to judge")     # coded, but only in the studies it was drawn from
        [r] = patterns.judge([self.u(7)], [SHARE], self.S)
        self.assertEqual(r["verdict"], "not coded")

    def test_a_contrast_needs_both_groups(self):
        units = [self.u(1, country=["uk"], direction=["better"]), self.u(2, country=["uk"], direction=["worse"]),
                 self.u(3, country=["us"], direction=["worse"]), self.u(4, country=["us"], direction=["worse"])]
        [r] = patterns.judge(units, [CONTRAST], self.S)
        self.assertEqual(r["test"]["value"], 0.5)
        self.assertEqual((r["verdict"], r["lenses"]["theory"]), ("holds", "wrong"))
        [r] = patterns.judge(units[:3], [CONTRAST], self.S)
        self.assertEqual((r["verdict"], r["lenses"]["theory"]), ("too few to judge", "open"))

    def test_uncoded_studies_count_as_no_only_when_asked(self):
        units = [self.u(1, country=["uk"]), self.u(2, measure=["x"]), self.u(3, measure=["y"])]
        p = dict(SHARE, among=[], holds=[0.0, 0.5])
        self.assertEqual(patterns.judge(units, [p], self.S)[0]["verdict"], "too few to judge")
        r = patterns.judge(units, [dict(p, uncoded="no")], self.S)[0]
        self.assertEqual((r["test"]["n"], r["verdict"]), (3, "holds"))


class Count(Base):
    def test_patterns_are_counted_on_the_runs_coding(self):
        for w, country in ((1, "uk"), (2, "uk"), (3, "us"), (4, "not_reported")):
            self.store.db.execute("INSERT INTO study_features (work_id, extractor, dimension, value, canonical, evidence, "
                                  "grounded, run_id, created_at) VALUES (?, 'model:c', 'country', ?, ?, '', 1, ?, ?)",
                                  (w, country, country, self.run, now()))
        self.store.db.commit()
        iid = insights.add(self.store, "Z", "model:c", "screen", "pattern", "Mostly UK.", works=[1])
        [r] = patterns.count(self.store, self.run, tomllib.loads(PROTOCOL), [dict(SHARE, noticed_in=[iid])])
        self.assertEqual((r["discovery"]["n"], r["test"]["n"]), (1, 2))                # work 4 is not coded
        self.assertEqual((r["test"]["value"], r["verdict"]), (0.5, "too few to judge"))
        self.assertIn("| P1 UK mostly | 2 | 0.50 | too few to judge | 1: 1.00 |", patterns.report([r]))

    def test_a_study_excluded_after_charting_does_not_count(self):
        for w, country in ((1, "uk"), (2, "uk"), (3, "us"), (4, "us")):
            self.store.db.execute("INSERT INTO study_features (work_id, extractor, dimension, value, canonical, evidence, "
                                  "grounded, run_id, created_at) VALUES (?, 'model:c', 'country', ?, ?, '', 1, ?, ?)",
                                  (w, country, country, self.run, now()))
        self.store.db.commit()
        for w in (1, 2, 3, 4):
            self.store.add_screening(self.run, w, "Z", "title_abstract", "model:s", "include")
        self.store.add_screening(self.run, 4, "Z", "full_text", "model:s", "exclude", "other")   # charted, then out
        [r] = patterns.count(self.store, self.run, tomllib.loads(PROTOCOL), [SHARE])
        self.assertEqual((r["test"]["n"], round(r["test"]["value"], 2)), (3, 0.67))


class SecondCoder(Base):
    def chart(self, w, country):
        self.store.db.execute("INSERT INTO study_features (work_id, extractor, dimension, value, canonical, evidence, "
                              "grounded, run_id, created_at) VALUES (?, 'model:first', 'country', ?, ?, '', 1, ?, ?)",
                              (w, country, country, self.run, now()))
        self.store.db.commit()

    def test_a_second_coding_is_stored_apart_and_compared(self):
        import json
        from psyscientist import agents
        for w, c in ((1, "uk"), (2, "us"), (3, "uk"), (4, "nl")):
            self.chart(w, c)
        with self.assertRaises(ValueError):
            agents.write_tasks(self.store, self.run, "extract", "model:second", root=self.root, work_ids=[1],
                               options={"second_coder": True})
        folder = agents.write_tasks(self.store, self.run, "extract", "model:second", root=self.root,
                                    work_ids=[1, 2, 3, 4], options={"dims": "country", "second_coder": True})
        self.assertTrue(json.loads((folder / "task.json").read_text(encoding="utf-8"))["second_coder"])
        batch = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        answer = {"uk": "uk", "us": "us", "nl": "uk"}
        mine = {1: "uk", 2: "us", 3: "uk", 4: "uk"}
        (folder / "batch_01_result.json").write_text(json.dumps({"records": [
            {"work_id": r["work_id"], "features": {"country": {"value": mine[r["work_id"]], "canonical": mine[r["work_id"]],
                                                               "evidence": ""}}, "claims": []} for r in batch]}),
            encoding="utf-8")
        counts = agents.import_task(folder, self.store)
        self.assertEqual(counts["problems"], [])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM study_features WHERE extractor = 'model:second'"
                                               ).fetchone()[0], 0)               # the charting is untouched
        rows = patterns.agreement(self.store, self.run, tomllib.loads(PROTOCOL))
        by = {r["category"]: r for r in rows}
        self.assertEqual((by[None]["n"], by[None]["coder"]), (4, "model:second"))
        self.assertEqual((by["uk"]["first"], by["uk"]["second"], round(by["uk"]["kappa"], 2)), (2, 3, 0.5))
        self.assertEqual(by["us"]["kappa"], 1.0)
        self.assertIsNone(by["other"]["kappa"])                                  # neither coder chose it
        self.assertEqual(patterns.unreliable(SHARE, rows, 0.6), ["country: uk"])
        self.assertIn("| | uk | 4 | 2 | 3 | 0.50 |", patterns.agreement_report(rows))
