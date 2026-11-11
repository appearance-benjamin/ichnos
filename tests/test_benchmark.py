"""The validation benchmark: labelled records become a benchmark run whose labels the agents never see; screening and
retrieval are scored against them."""

import shutil
import tempfile
import unittest
from pathlib import Path

from ichnos import benchmark
from ichnos.store import Store
from tests.test_agents_map import PROTOCOL

LABELS = """id;title;abstract;doi;year;label
r1;Sleep and mood in adolescents;A cohort study of sleep and mood.;10.5/a1;2019;1
r2;Insomnia and depression trajectories;Four waves of data.;10.5/a2;2020;include
r3;Soil chemistry of vineyards;Nothing about sleep.;10.5/a3;2018;0
r4;Market prices of copper;Unrelated.;10.5/a4;2017;exclude
r5;Sleep loss and irritability;An experiment.;10.5/a5;2021;1
r5b;Sleep loss and irritability;Duplicate row of r5.;10.5/a5;2021;0
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "protocol").mkdir()
        (self.root / "protocol" / "corpus_z.toml").write_text(PROTOCOL + "\n[benchmark]\nrecall_min = 0.9\n",
                                                              encoding="utf-8")
        self.store = Store(self.root / "evidence.sqlite")
        self.file = self.root / "labels.csv"
        self.file.write_text(LABELS, encoding="utf-8")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def imported(self):
        return benchmark.import_labels(self.store, "Z", self.file, "t-bench", "test", self.root)

    def work(self, doi):
        return self.store.db.execute("SELECT work_id FROM works WHERE doi = ?", (doi,)).fetchone()[0]

    def screen(self, run, screener, keep):
        for doi in ("10.5/a1", "10.5/a2", "10.5/a3", "10.5/a4", "10.5/a5"):
            self.store.add_screening(run, self.work(doi), "Z", "title_abstract", screener,
                                     "include" if doi in keep else "exclude")


class Screening(Base):
    def test_import_keeps_labels_apart(self):
        r = self.imported()
        self.assertEqual((r["records"], r["included"], r["duplicates"]), (5, 3, 1))
        cols = {c["name"] for c in self.store.db.execute("PRAGMA table_info(works)")}
        self.assertNotIn("label", cols)
        sheet = self.store.db.execute("SELECT count(*) FROM samples WHERE run_id = ? AND purpose = 'pilot_screening'",
                                      (r["run_id"],)).fetchone()[0]
        self.assertEqual(sheet, 5)
        sources = {s[0] for s in self.store.db.execute("SELECT source_id FROM retrievals WHERE run_id = ?",
                                                        (r["run_id"],))}
        self.assertEqual(sources, {"r1", "r2", "r3", "r4", "r5"})       # ids, never labels
        with self.assertRaisesRegex(ValueError, "already imported"):
            self.imported()

    def test_changed_file_needs_a_new_name(self):
        self.imported()
        self.file.write_text(LABELS.replace("Soil", "Clay"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "different file"):
            self.imported()

    def test_bad_label_and_missing_criteria(self):
        self.file.write_text(LABELS.replace(";1\n", ";maybe\n", 1), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "neither include nor exclude"):
            self.imported()
        (self.root / "protocol" / "corpus_y.toml").write_text('corpus = "Y"\n', encoding="utf-8")
        self.file.write_text(LABELS, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "screening.metrics"):
            benchmark.import_labels(self.store, "Y", self.file, "y-bench", root=self.root)

    def test_score_per_screener_and_combined(self):
        r = self.imported()
        self.assertTrue(benchmark.score(self.store, "t-bench")["verdict"].startswith("incomplete"))
        self.screen(r["run_id"], "model:a", keep={"10.5/a1", "10.5/a2", "10.5/a3"})       # misses a5, keeps a3
        self.screen(r["run_id"], "model:b", keep={"10.5/a1", "10.5/a5"})                  # misses a2
        res = benchmark.score(self.store, "t-bench")
        a, b, comb = res["screeners"]["model:a"], res["screeners"]["model:b"], res["combined"]
        self.assertEqual((a["tp"], a["fn"], a["fp"], a["tn"]), (2, 1, 1, 1))
        self.assertAlmostEqual(b["recall"], 2 / 3)
        self.assertEqual((comb["tp"], comb["fn"], comb["fp"]), (3, 0, 1))   # kept if either keeps
        self.assertEqual(comb["recall"], 1.0)
        self.assertEqual(res["missed"], [])
        self.assertEqual(res["verdict"], "meets every threshold")
        text = benchmark.report(res)
        self.assertIn("combined (what Ichnos keeps)", text)
        self.assertIn("recall_min 0.9", text)

    def test_missed_inclusion_fails_threshold(self):
        r = self.imported()
        self.screen(r["run_id"], "model:a", keep={"10.5/a1"})
        res = benchmark.score(self.store, "t-bench")
        self.assertEqual(len(res["missed"]), 2)
        self.assertTrue(res["verdict"].startswith("fails: recall 0.333"))
        path = benchmark.write_report(self.store, "t-bench", self.root)
        self.assertIn("Inclusions Ichnos missed (2)", path.read_text(encoding="utf-8"))

    def test_wilson(self):
        lo, hi = benchmark.wilson(19, 20)
        self.assertTrue(0.75 < lo < 0.8 and 0.99 < hi <= 1.0)
        self.assertIsNone(benchmark.wilson(0, 0))


class FakeHttp:
    WORKS = {"W1": ("10.5/a1", "Sleep and mood"), "W2": ("10.5/a2", "Soil chemistry"), "W9": ("10.5/a9", "Found by doi")}

    def __init__(self):
        self.calls = []

    def get_json(self, source, url, params):
        self.calls.append(params["filter"])
        key, values = params["filter"].split(":", 1)
        hits = []
        for wid, (doi, title) in self.WORKS.items():
            if (wid if key == "openalex" else doi) in values.split("|"):
                hits.append({"id": f"https://openalex.org/{wid}", "doi": f"https://doi.org/{doi}", "title": title,
                             "publication_year": 2020, "abstract_inverted_index": {"An": [0], "abstract": [1]}})
        return {"results": hits}


class FromIds(Base):
    def test_ids_become_a_labelled_set(self):
        ids = self.root / "x_ids.csv"
        ids.write_text("doi,openalex_id,label_included\n"
                       "https://doi.org/10.5/a1,https://openalex.org/W1,1\n"
                       ",https://openalex.org/W2,0\n"
                       "https://doi.org/10.5/a9,,1\n"
                       ",https://openalex.org/W404,0\n"
                       ",,1\n", encoding="utf-8")
        http = FakeHttp()
        r = benchmark.from_ids(ids, self.root / "out" / "x.csv", http)
        self.assertEqual((r["rows"], r["written"], r["included"]), (5, 3, 2))
        self.assertEqual(r["lost"], {"include": 1, "exclude": 1})
        self.assertEqual(http.calls, ["openalex:W1|W2|W404", "doi:10.5/a9"])
        rows, _ = benchmark.read_rows(r["file"])
        self.assertEqual([(x["id"], x["label"], x["abstract"]) for x in rows],
                         [("W1", "include", "An abstract"), ("W2", "exclude", "An abstract"),
                          ("W9", "include", "An abstract")])
        res = benchmark.import_labels(self.store, "Z", r["file"], "ids-bench", "test", self.root)
        self.assertEqual((res["records"], res["included"]), (3, 2))


class Retrieval(Base):
    def test_recall_by_stage(self):
        run = self.store.start_run("pilot", "Z", None, {})
        ids = {}
        for doi in ("10.5/b1", "10.5/b2", "10.5/b3"):
            ids[doi] = self.store.upsert_work({"title": f"Study {doi}", "doi": doi, "year": 2020})
            self.store.add_retrieval(run, ids[doi], "openalex", doi, 1)
        self.store.add_sample(run, [ids["10.5/b1"], ids["10.5/b2"]], "pilot_screening")
        self.store.add_screening(run, ids["10.5/b1"], "Z", "title_abstract", "model:a", "include")
        self.store.add_screening(run, ids["10.5/b2"], "Z", "title_abstract", "model:a", "exclude")
        ref = self.root / "ref.csv"
        ref.write_text("doi,title\n10.5/b1,Study 10.5/b1\n10.5/b2,Study 10.5/b2\n,A study nobody found\n",
                       encoding="utf-8")
        r = benchmark.import_reference(self.store, run, ref, "t-ref")
        self.assertEqual((r["studies"], r["new_to_store"]), (3, 1))
        res = benchmark.score(self.store, "t-ref")
        s = res["stages"]
        self.assertEqual((s["retrieved"]["found"], s["on the sheet"]["found"], s["kept"]["found"],
                          s["with findings"]["found"]), (2, 2, 1, 0))
        self.assertEqual([m["title"] for m in s["retrieved"]["missing"]], ["A study nobody found"])
        self.assertIn("| kept | 1 of 3 |", benchmark.report(res))


if __name__ == "__main__":
    unittest.main()
