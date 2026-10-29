import json
import unittest

from ichnos.sources import openalex, semantic_scholar
from ichnos.store import Store, normalize_doi, title_key


def record(**kw):
    base = {"title": "Purpose in life after job loss", "year": 2020}
    base.update(kw)
    return base


class Normalizing(unittest.TestCase):
    def test_doi(self):
        self.assertEqual(normalize_doi("https://doi.org/10.1037/ABC.123"), "10.1037/abc.123")
        self.assertEqual(normalize_doi("doi:10.1/x"), "10.1/x")
        self.assertIsNone(normalize_doi(""))

    def test_title_key(self):
        self.assertEqual(title_key("Purpose in Life — after Job-Loss?"), "purpose in life after job loss")


class Dedupe(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def test_doi_match_merges_and_fills_gaps(self):
        a = self.store.upsert_work(record(doi="10.1/x", openalex_id="W1", abstract=None))
        b = self.store.upsert_work(record(doi="https://doi.org/10.1/X", s2_id="S1", abstract="Text."))
        self.assertEqual(a, b)
        row = self.store.db.execute("SELECT * FROM works").fetchone()
        self.assertEqual((row["openalex_id"], row["s2_id"], row["abstract"]), ("W1", "S1", "Text."))

    def test_title_and_year_match_without_doi(self):
        a = self.store.upsert_work(record(openalex_id="W1", year=2020))
        b = self.store.upsert_work(record(s2_id="S1", year=2021, title="Purpose in life after job-loss"))
        c = self.store.upsert_work(record(s2_id="S2", year=2024))
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_existing_values_are_not_overwritten(self):
        a = self.store.upsert_work(record(doi="10.1/x", venue="Journal A"))
        self.store.upsert_work(record(doi="10.1/x", venue="Journal B"))
        venue = self.store.db.execute("SELECT venue FROM works WHERE work_id = ?", (a,)).fetchone()[0]
        self.assertEqual(venue, "Journal A")

    def test_authors_stored_as_json(self):
        a = self.store.upsert_work(record(authors=["Ä. Müller", "B. Smith"]))
        raw = self.store.db.execute("SELECT authors FROM works WHERE work_id = ?", (a,)).fetchone()[0]
        self.assertEqual(json.loads(raw), ["Ä. Müller", "B. Smith"])

    def test_call_log_round_trip(self):
        call = self.store.log_call(None, "http", "openalex", "https://x?api_key=REDACTED", 200, 12, b'{"a": 1}')
        self.assertEqual(self.store.call_response(call), b'{"a": 1}')


class Parsing(unittest.TestCase):
    def test_openalex_work(self):
        w = openalex.parse_work({
            "id": "https://openalex.org/W42",
            "doi": "https://doi.org/10.5/AB",
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/123"},
            "title": "T", "publication_year": 2019, "type": "article", "language": "en",
            "primary_location": {"source": {"display_name": "Psychology and Aging"}},
            "authorships": [{"author": {"display_name": "A"}}, {"author": {}}],
            "abstract_inverted_index": {"life": [2], "Purpose": [0], "in": [1]},
            "open_access": {"oa_url": "https://oa/x"},
        })
        self.assertEqual((w["openalex_id"], w["doi"], w["pmid"]), ("W42", "10.5/ab", "123"))
        self.assertEqual(w["abstract"], "Purpose in life")
        self.assertEqual((w["venue"], w["authors"]), ("Psychology and Aging", ["A"]))

    def test_openalex_work_with_nulls(self):
        w = openalex.parse_work({"id": "https://openalex.org/W1", "primary_location": None,
                                 "abstract_inverted_index": None, "open_access": None})
        self.assertIsNone(w["abstract"])
        self.assertIsNone(w["venue"])

    def test_s2_paper(self):
        p = semantic_scholar.parse_paper({
            "paperId": "abc", "externalIds": {"DOI": "10.5/AB", "PubMed": "9"},
            "title": "T", "abstract": None, "year": 2018, "venue": "",
            "publicationTypes": ["JournalArticle", "Review"], "authors": [{"name": "A"}],
            "openAccessPdf": {"url": ""},
        })
        self.assertEqual((p["s2_id"], p["doi"], p["pmid"]), ("abc", "10.5/ab", "9"))
        self.assertEqual((p["venue"], p["type"], p["oa_url"]), (None, "JournalArticle;Review", None))


if __name__ == "__main__":
    unittest.main()
