"""Recall test: references of known sources are matched, found ones judged by the review's screening, missed ones
by agents on the same criteria, and nothing enters the review."""

import json
import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from ichnos import agents, recall
from ichnos.store import Store

PROTOCOL = '''
corpus = "R"
title = "Toy"
query = """"programmatic assessment\""""
sources = []

[screening]
stage = "title_abstract"
rule = "Include empirical studies of programmatic assessment."
decisions = ["include", "exclude", "unsure"]
exclude_reasons = ["not_empirical", "review", "wrong_topic"]

[[screening.metrics]]
id = "empirical"
question = "Original data?"
fail_reason = "not_empirical"
review_reason = "review"

[[screening.metrics]]
id = "topic"
question = "Programmatic assessment?"
fail_reason = "wrong_topic"

[recall]
sources = ["B1"]

[[background]]
id = "B1"
cite = "Known, 2021"
reference = "Known, A. (2021). A review. *J*, *1*, 1. https://doi.org/10.1/review"
claim = "A review."
'''


def oa(wid, title, abstract=""):
    return {"id": f"https://openalex.org/{wid}", "doi": None, "title": title, "publication_year": 2020,
            "abstract_inverted_index": {w: [i] for i, w in enumerate(abstract.split())} or None}


class FakeHttp:
    def __init__(self, store):
        self.store = store

    def get_json(self, source, url, params, headers=None):
        self.store.log_call(None, "http", source, url, 200, 0, b"{}", {})
        if "doi:10.1/review" in url:
            return {"referenced_works": ["https://openalex.org/W1", "https://openalex.org/W2", "https://openalex.org/W3"]}
        return {"results": [oa("W1", "Programmatic assessment in a medical school"),
                            oa("W2", "Students' experience of assessment portfolios", "A qualitative study of students."),
                            oa("W3", "Validity theory revisited")]}

    def sleep(self, s):
        pass


class RecallTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "protocol").mkdir()
        (self.root / "protocol" / "corpus_r.toml").write_text(PROTOCOL, encoding="utf-8")
        self.protocol = tomllib.loads(PROTOCOL)
        self.store = Store(self.root / "evidence.sqlite")
        sha = self.store.add_protocol("R", "protocol/corpus_r.toml", PROTOCOL)
        self.run = self.store.start_run("pilot", "R", sha, {})
        self.found = self.store.upsert_work({"title": "Programmatic assessment in a medical school", "year": 2020,
                                             "openalex_id": "W1"})
        self.store.add_retrieval(self.run, self.found, "pubmed", "1", 1)
        self.store.add_sample(self.run, [self.found], "pilot_screening")
        self.store.add_screening(self.run, self.found, "R", "title_abstract", "model:a", "include")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_found_and_missed_are_judged_alike(self):
        counts = recall.fetch(self.store, self.run, self.protocol, http=FakeHttp(self.store), log=lambda *a: None)
        self.assertEqual(counts["B1"], 3)
        missed = recall.to_judge(self.store, self.run, self.protocol)
        self.assertEqual(len(missed), 2)
        folder = agents.write_tasks(self.store, self.run, "recall", "model:judge", root=self.root)
        self.assertIn("reference lists", (folder / "instructions.md").read_text(encoding="utf-8"))
        batch = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))

        def score(r):
            topical = "portfolios" in r["title"]
            return {"work_id": r["work_id"], "kind": "study",
                    "scores": {"empirical": {"score": 2 if topical else 0, "evidence": ""},
                               "topic": {"score": 2 if topical else 0, "evidence": ""}}}
        (folder / "batch_01_result.json").write_text(json.dumps([score(r) for r in batch]), encoding="utf-8")
        self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        s = recall.summary(self.store, self.run, self.protocol)
        self.assertEqual((s["references"], s["relevant_found"], s["relevant_missed"]), (3, 1, 1))
        self.assertEqual(s["recall"], 0.5)
        self.assertEqual(s["relevant_missed_with_label"], 0)
        # the judgements stay out of the review's screening
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM screening").fetchone()[0], 1)
        self.assertEqual(agents.kept_ids(self.store, self.run), [self.found])


if __name__ == "__main__":
    unittest.main()
