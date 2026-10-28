"""Citation searching: mined records join the sheet only when they match the search string, and only once."""

import json
import unittest

from psyscientist import mining
from psyscientist.agents import sheet_ids
from psyscientist.store import Store


def paper(pid, title, abstract=""):
    return {"paperId": pid, "externalIds": {}, "title": title, "abstract": abstract, "year": 2025, "authors": []}


class MiningTest(unittest.TestCase):
    def setUp(self):
        self.saved = (mining.seeds, mining.edges, mining.Http)
        self.store = Store(":memory:")
        self.run = self.store.start_run("pilot", "T", None, {})
        self.seed = self.store.upsert_work({"title": "A review of anxiety about artificial intelligence", "s2_id": "s1"})
        self.known = self.store.upsert_work({"title": "AI anxiety among nurses", "s2_id": "k1"})
        self.store.add_retrieval(self.run, self.known, "pubmed", "k1", 1)
        self.store.add_sample(self.run, [self.known], "pilot_screening")
        mining.seeds = lambda store, run_id, protocol: {"references": [self.seed], "citations": [],
                                                         "reviews": [self.seed], "core": []}
        mining.edges = lambda http, pid, edge, api_key=None: [
            paper("k1", "AI anxiety among nurses"),
            paper("n1", "AI anxiety and burnout in teachers"),
            paper("x1", "Deep learning for image segmentation")]

        class NoHttp:
            def __init__(self, *a, **k):
                pass

            def sleep(self, s):
                pass
        mining.Http = NoHttp

    def tearDown(self):
        mining.seeds, mining.edges, mining.Http = self.saved

    def test_matching_records_join_the_sheet(self):
        out = mining.mine(self.store, self.run, {"query": '"AI anxiety"'})
        self.assertEqual((out["unique"], out["matched"], out["already"], out["new"]), (3, 2, 1, 1))
        new = out["new_ids"][0]
        self.assertIn(new, sheet_ids(self.store, self.run))
        self.assertEqual(len(sheet_ids(self.store, self.run)), 2)
        row = self.store.db.execute("SELECT * FROM run_sources WHERE source = 'citation_search'").fetchone()
        self.assertEqual(json.loads(row["notes"])["new"], 1)
        again = mining.mine(self.store, self.run, {"query": '"AI anxiety"'})
        self.assertEqual(again["new"], 0)


if __name__ == "__main__":
    unittest.main()
