"""Missing abstracts: looked up by identifier, first source wins, every outcome recorded, nothing overwritten."""

import json
import unittest

from ichnos import abstracts, mining
from ichnos.http import ApiError
from ichnos.store import Store


class FakeHttp:
    """Answers by URL; records what was asked."""

    def __init__(self, store, answers):
        self.store, self.answers, self.asked = store, answers, []

    def _answer(self, source, url, params):
        self.asked.append((source, url, dict(params)))
        self.store.log_call(None, "http", source, url, 200, 0, b"{}", {})
        for key, value in self.answers.items():
            if key in url or key in json.dumps(params):
                if isinstance(value, Exception):
                    raise value
                return value
        raise ApiError(source, 404, "not found")

    def get_json(self, source, url, params, headers=None):
        return self._answer(source, url, params)

    def get(self, source, url, params, headers=None, keep_body=True):
        return self._answer(source, url, params)

    def sleep(self, s):
        pass


LONG = "Students in a programmatic assessment programme reported lower stress after the reform than before it."


class AbstractsTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.run = self.store.start_run("pilot", "T", None, {})
        self.bare = self.store.upsert_work({"title": "Programmatic assessment and stress", "doi": "10.1/a"})
        self.done = self.store.upsert_work({"title": "Another study", "doi": "10.1/b", "abstract": LONG})
        self.none = self.store.upsert_work({"title": "A record nobody indexes", "s2_id": "s2x"})

    def test_first_source_with_an_abstract_wins(self):
        http = FakeHttp(self.store, {
            "esearch": {"esearchresult": {"idlist": []}},
            "europepmc": {"resultList": {"result": [{"doi": "10.1/A", "abstractText": "<p>Background: " + LONG + "</p>"}]}},
            "api.crossref.org": {"message": {"abstract": "<jats:p>never reached</jats:p>"}},
        })
        counts = abstracts.fill(self.store, self.run, [self.bare, self.done, self.none], http=http, log=lambda *a: None)
        self.assertEqual(counts["europepmc"], 1)
        self.assertEqual(counts["not_found"], 1)
        row = self.store.db.execute("SELECT abstract FROM works WHERE work_id = ?", (self.bare,)).fetchone()
        self.assertTrue(row["abstract"].startswith("Background: Students"))
        self.assertNotIn("<p>", row["abstract"])
        self.assertFalse(any("crossref" in a[1] for a in http.asked))        # stopped at the first abstract
        self.assertFalse(any("10.1/b" in json.dumps(a) for a in http.asked))  # had one already: not asked
        fill = self.store.db.execute("SELECT * FROM abstract_fills WHERE work_id = ?", (self.bare,)).fetchone()
        self.assertEqual((fill["status"], fill["source"]), ("found", "europepmc"))
        self.assertEqual(json.loads(fill["tried"]), ["pubmed", "europepmc"])
        self.assertIsNotNone(fill["call_id"])
        miss = self.store.db.execute("SELECT * FROM abstract_fills WHERE work_id = ?", (self.none,)).fetchone()
        self.assertEqual(miss["status"], "not_found")
        s = abstracts.summary(self.store, self.run)
        self.assertEqual((s["tried"], s["found"], s["ids"]), (2, 1, [self.bare]))
        again = abstracts.fill(self.store, self.run, [self.none], http=http, log=lambda *a: None)
        self.assertEqual(again["already_tried"], 1)

    def test_stubs_and_titles_are_not_abstracts(self):
        self.assertIsNone(abstracts.clean("No abstract available"))
        self.assertIsNone(abstracts.clean("Programmatic assessment and stress in a long enough title to pass",
                                          "Programmatic assessment and stress in a long enough title to pass"))
        self.assertEqual(abstracts.clean("<jats:title>Abstract</jats:title><jats:p>" + LONG + "</jats:p>"), LONG)
        self.assertIsNone(abstracts.clean('"Towards a systems approach to assessment." Medical Teacher, 34(3), pp. 185-186',
                                          "Towards a systems approach to assessment"))

    def test_a_search_by_doi_that_returns_another_paper_is_rejected(self):
        xml = (b"<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>9</PMID><Article><ArticleTitle>Avoidable "
               b"hospitalizations in Canada</ArticleTitle><ELocationID EIdType='doi'>10.9/other</ELocationID><Abstract>"
               b"<AbstractText>" + LONG.encode() + b"</AbstractText></Abstract></Article></MedlineCitation>"
               b"</PubmedArticle></PubmedArticleSet>")
        http = FakeHttp(self.store, {"esearch": {"esearchresult": {"idlist": ["9"]}}, "efetch": xml})
        work = self.store.db.execute("SELECT * FROM works WHERE work_id = ?", (self.bare,)).fetchone()
        self.assertIsNone(abstracts.lookup(http, work, "pubmed"))


class MiningFallbackTest(unittest.TestCase):
    def test_a_failed_identifier_tries_the_next_and_passes_are_kept(self):
        store = Store(":memory:")
        run = store.start_run("pilot", "T", None, {})
        seed = store.upsert_work({"title": "A review", "s2_id": "bad", "doi": "10.1/good"})
        saved = (mining.seeds, mining.edges, mining.Http)
        tried = []

        def edges(http, pid, edge, api_key=None):
            tried.append(pid)
            if pid == "bad":
                raise ApiError("citation_search", 404, "not found")
            return [{"paperId": "n1", "externalIds": {}, "title": "Programmatic assessment in nursing",
                     "abstract": "", "year": 2024, "authors": []}]

        class NoHttp:
            def __init__(self, *a, **k):
                pass

            def sleep(self, s):
                pass
        try:
            mining.seeds = lambda s, r, p: {"references": [seed], "citations": [], "reviews": [seed], "core": []}
            mining.edges, mining.Http = edges, NoHttp
            out = mining.mine(store, run, {"query": '"programmatic assessment"'}, log=lambda *a: None)
            mining.mine(store, run, {"query": '"programmatic assessment"'}, log=lambda *a: None)
        finally:
            mining.seeds, mining.edges, mining.Http = saved
        self.assertEqual(tried[:2], ["bad", "DOI:10.1/good"])
        self.assertEqual((out["failed"], out["new"], out["matched_works"]), (0, 1, 1))
        notes = json.loads(store.db.execute("SELECT notes FROM run_sources").fetchone()["notes"])
        self.assertEqual([p["new"] for p in notes["passes"]], [1, 0])


if __name__ == "__main__":
    unittest.main()
