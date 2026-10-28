"""Citation searching through OpenAlex: references by id in batches, citing works by cursor, matches join the sheet."""

import unittest
from unittest import mock

from psyscientist import mining
from psyscientist.store import Store

ABS = {"Grandmothers": [0], "raise": [1], "fertility": [2], "in": [3], "Finland": [4]}


def work(wid, title, abstract=None):
    return {"id": f"https://openalex.org/{wid}", "title": title, "publication_year": 2010,
            "abstract_inverted_index": abstract, "authorships": [], "ids": {}}


class OpenAlexMining(unittest.TestCase):
    def test_references_and_citations_from_openalex_and_only_matches_join(self):
        store = Store(":memory:")
        run = store.start_run("pilot", "T", None, {})
        review = store.upsert_work({"title": "A review of kin and fertility", "doi": "10.1/rev"})
        core = store.upsert_work({"title": "Core study", "openalex_id": "W100"})
        asked = []

        def answer(http, source, url, params, headers=None, reuse=False):
            asked.append((url, params.get("filter")))
            if url.endswith("/doi:10.1/rev"):
                return {"id": "https://openalex.org/W1", "referenced_works": ["https://openalex.org/W2",
                                                                               "https://openalex.org/W3"]}
            if url.endswith("/W100"):
                return {"id": "https://openalex.org/W100", "referenced_works": []}
            f = params.get("filter", "")
            if f.startswith("openalex:"):
                return {"results": [work("W2", "Grandmothers and births", ABS), work("W3", "Soil carbon in crops")]}
            if f == "cites:W100":
                return {"results": [work("W4", "Fertility and grandparent childcare in Italy")],
                        "meta": {"next_cursor": None}}
            raise AssertionError(url)

        with mock.patch.object(mining, "seeds", lambda s, r, p: {"references": [review, core], "citations": [core],
                                                                 "reviews": [review], "core": [core]}), \
             mock.patch("psyscientist.http.Http.get_json", answer):
            out = mining.mine(store, run, {"query": "fertility AND (grandmothers OR grandparent)"}, via="openalex",
                              log=lambda *a: None)
        self.assertEqual((out["references"], out["citations"], out["matched"], out["new"]), (2, 1, 2, 2))
        self.assertEqual(out["via"], "OpenAlex")
        titles = {r["title"] for r in store.db.execute(
            "SELECT w.title FROM samples s JOIN works w USING (work_id) WHERE s.run_id = ?", (run,))}
        self.assertEqual(titles, {"Grandmothers and births", "Fertility and grandparent childcare in Italy"})
        self.assertIn(("https://api.openalex.org/works", "openalex:W2|W3"), asked)


if __name__ == "__main__":
    unittest.main()
