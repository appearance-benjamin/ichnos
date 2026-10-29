"""Getting every text a record can have: pacing and waits, identifiers by title, fallbacks, and the wrong-file check."""

import unittest
from datetime import datetime, timezone
from unittest import mock

from ichnos import abstracts, fulltext, resolve, texts
from ichnos.config import Settings
from ichnos.fulltext import Candidate
from ichnos.http import ApiError, Http
from ichnos.store import Store
from tests.test_abstracts import LONG, FakeHttp
from tests.test_agents_map import Base

TITLE = "Grandparental childcare and the timing of second births in Europe"


class Pacing(unittest.TestCase):
    def test_a_paced_source_waits_its_turn_and_others_do_not(self):
        clock, slept = [100.0], []
        h = Http(Store(":memory:"), None, max_retries=0, sleep=lambda s: (slept.append(round(s, 1)),
                                                                          clock.__setitem__(0, clock[0] + s)),
                 clock=lambda: clock[0])
        h.pace("core", 6.5)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            for source in ("core", "core", "openalex", "core_fulltext"):
                with self.assertRaises(ApiError):
                    h.get(source, "https://x/y", {})
        self.assertEqual(slept, [6.5, 6.5])          # the second CORE call and the CORE download; not OpenAlex

    def test_a_source_that_keeps_refusing_is_paused_for_the_run(self):
        h = Http(Store(":memory:"), None, max_retries=0, sleep=lambda s: None)
        h.breaker("core", after=2)
        calls = []

        def refuse(req, timeout=0):
            calls.append(req.full_url)
            raise __import__("urllib.error").error.HTTPError(req.full_url, 429, "Too Many", {}, None)
        with mock.patch("urllib.request.urlopen", refuse):
            for _ in range(4):
                with self.assertRaises(ApiError) as caught:
                    h.get("core", "https://api.core.ac.uk/v3/search/works", {})
        self.assertEqual(len(calls), 2)                     # the last two never reached CORE
        self.assertIn("paused for this run", str(caught.exception))

    def test_a_resumed_job_reuses_a_logged_answer_and_asks_only_the_rest(self):
        store = Store(":memory:")
        run = store.start_run("pilot", "T", None, {})
        h = Http(store, run, max_retries=0, sleep=lambda s: None)

        class Resp:
            status, headers = 200, {}

            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        asked = []

        def answer(req, timeout=0):
            asked.append(req.full_url)
            return Resp(b'{"data": [1]}')
        with mock.patch("urllib.request.urlopen", answer):
            self.assertEqual(h.get_json("citation_search", "https://s2/p/1/references", {"offset": 0}, reuse=True),
                             {"data": [1]})
            self.assertEqual(h.get_json("citation_search", "https://s2/p/1/references", {"offset": 0}, reuse=True),
                             {"data": [1]})
            h.get_json("citation_search", "https://s2/p/2/references", {"offset": 0}, reuse=True)
        self.assertEqual(len(asked), 2)                     # the repeated request came from the log

    def test_waits_follow_what_the_service_says(self):
        now = datetime(2026, 10, 25, 10, 56, 40, tzinfo=timezone.utc)
        self.assertEqual(Http._wait(0, "7", None, now), 7.0)
        self.assertEqual(Http._wait(0, None, "2026-10-25T10:56:52+0000", now), 13.0)
        self.assertEqual(Http._wait(0, None, "2026-10-25T09:00:00+0000", now), 1.0)
        self.assertEqual(Http._wait(2, None, "garbled", now), 8.0)


class Identifiers(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.run = self.store.start_run("pilot", "T", None, {})
        self.bare = self.store.upsert_work({"title": TITLE, "year": 2018, "s2_id": "s2a"})
        self.short = self.store.upsert_work({"title": "Fertility", "year": 2018, "s2_id": "s2b"})
        self.other = self.store.upsert_work({"title": "Kin support and fertility intentions in Italy and Spain",
                                             "year": 2010, "s2_id": "s2c"})
        # the same paper indexed elsewhere with its subtitle: a record of its own until the lookup finds the DOI
        self.held = self.store.upsert_work({"title": "Kin support and fertility intentions in Italy and Spain: "
                                                     "a comparison", "doi": "10.1/held", "year": 2010})

    def work(self, w):
        return self.store.db.execute("SELECT * FROM works WHERE work_id = ?", (w,)).fetchone()

    def test_a_match_needs_the_same_title_and_a_close_year(self):
        w = self.work(self.bare)
        self.assertIsNone(resolve.match(w, [{"title": TITLE, "publication_year": 2012}]))
        hit = resolve.match(w, [{"title": TITLE + " revisited", "publication_year": 2018},
                                {"title": TITLE.upper(), "publication_year": 2019, "id": "W1"}])
        self.assertEqual(hit["id"], "W1")
        self.assertIsNone(resolve.match(self.work(self.short), [{"title": "Fertility", "publication_year": 2018}]))

    def test_ids_are_added_a_held_one_marks_a_duplicate_and_errors_are_asked_again(self):
        http = FakeHttp(self.store, {
            "Grandparental": {"results": [{"id": "https://openalex.org/W9", "doi": "https://doi.org/10.1/NEW",
                                           "title": TITLE, "publication_year": 2018}]},
            "Kin support": {"results": [{"id": "https://openalex.org/W8", "doi": "10.1/held",
                                         "title": "Kin support and fertility intentions in Italy and Spain",
                                         "publication_year": 2010}]},
        })
        counts = resolve.resolve(self.store, self.run, [self.bare, self.short, self.other], http=http,
                                 log=lambda *a: None)
        self.assertEqual(counts, {"found": 1, "not_found": 1, "duplicate": 1})
        self.assertEqual((self.work(self.bare)["openalex_id"], self.work(self.bare)["doi"]), ("W9", "10.1/new"))
        self.assertIsNone(self.work(self.other)["openalex_id"])            # nothing merged
        self.assertEqual(resolve.duplicates(self.store, self.run), [(self.other, self.held)])
        self.assertFalse(any("Fertility" == a[2].get("filter", "")[-9:] for a in http.asked))  # too short to ask
        failing = FakeHttp(self.store, {"Kin support": ApiError("openalex_resolve", 429, "slow down")})
        self.store.db.execute("DELETE FROM id_resolutions WHERE work_id = ?", (self.other,))
        self.assertEqual(resolve.resolve(self.store, self.run, [self.other], http=failing, log=lambda *a: None),
                         {"error": 1})
        again = resolve.resolve(self.store, self.run, [self.other], http=http, log=lambda *a: None)
        self.assertEqual(again, {"duplicate": 1})                            # the error did not settle it


class AbstractRetries(unittest.TestCase):
    def test_a_lookup_stopped_by_an_error_is_asked_again_and_core_comes_last(self):
        store = Store(":memory:")
        run = store.start_run("pilot", "T", None, {})
        w = store.upsert_work({"title": TITLE, "doi": "10.1/a", "year": 2018})
        first = FakeHttp(store, {"api.semanticscholar.org": ApiError("semantic_scholar", 429, "too many")})
        counts = abstracts.fill(store, run, [w], http=first, log=lambda *a: None)
        self.assertEqual(list(counts), ["not_found (an error at a source: tried again next time)"])
        second = FakeHttp(store, {"api.core.ac.uk": {"results": [{"doi": "10.1/A", "title": TITLE,
                                                                  "abstract": LONG}]}})
        self.assertEqual(abstracts.fill(store, run, [w], http=second, log=lambda *a: None), {"core": 1})
        self.assertEqual(abstracts.fill(store, run, [w], http=second, log=lambda *a: None), {})


class Fallbacks(Base):
    def settings(self):
        return Settings(None, None, None, self.root / "evidence.sqlite", grobid_url="http://127.0.0.1:9")

    def setUp(self):
        super().setUp()
        with self.store.db:
            self.store.db.execute("UPDATE works SET title = ?, doi = '10.1/x' WHERE work_id = 1", (TITLE,))

    def run_one(self, located, answers, core_hit=None, first=TITLE):
        def fake_get(http, source, url, params, headers=None, keep_body=True):
            for key, value in answers.items():
                if key in url:
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise ApiError(source, 404, "not found")

        def fake_json(http, source, url, params, headers=None):
            if source == "wayback":
                return answers.get("wayback-api", {})
            raise ApiError(source, 404, "not found")

        with mock.patch.object(fulltext, "locate", lambda http, work, settings, errors=None: list(located)), \
             mock.patch("ichnos.http.Http.get", fake_get), \
             mock.patch("ichnos.http.Http.get_json", fake_json), \
             mock.patch("ichnos.sources.core.find", lambda http, work, key=None: core_hit), \
             mock.patch.object(fulltext, "first_pages", lambda pdf, n=2: first), \
             mock.patch.object(fulltext, "parse_pdf", lambda pdf, grobid: ([{"head": "Results", "text": "x" * 50}],
                                                                           "pypdf", None)):
            fulltext.retrieve(self.store, [1], self.settings(), "Z", self.run, root=self.root, log=lambda *_: None)
        return self.store.get_fulltext(1)

    def test_core_text_when_every_located_copy_fails(self):
        body = TITLE + "\nIntroduction\n" + "Grandparents help. " * 300
        row = self.run_one([Candidate("unpaywall", "pdf", "https://pub.example/1.pdf", "cc by", None)],
                           {"pub.example": ApiError("unpaywall_fulltext", 403, "web page: Just a moment...")},
                           core_hit={"id": 77, "fullText": body})
        self.assertEqual((row["status"], row["source"], row["parser"]), ("parsed", "core", "core text"))
        self.assertIn("bot check", row["error"])
        self.assertTrue((self.root / row["file_path"]).exists())

    def test_an_archived_copy_of_a_dead_link_and_a_wrong_file_is_refused(self):
        located = [Candidate("openalex", "pdf", "https://repo.example/gone.pdf", None, "acceptedVersion")]
        snap = {"archived_snapshots": {"closest": {"available": True, "status": "200",
                                                   "timestamp": "20200101000000"}}}
        answers = {"web.archive.org": b"%PDF-1.4 x", "repo.example/gone": ApiError("openalex_fulltext", 404, "gone"),
                   "wayback-api": snap}
        row = self.run_one(located, answers)
        self.assertEqual((row["status"], row["source"]), ("parsed", "wayback"))
        self.assertTrue(row["url"].startswith("https://web.archive.org/web/20200101000000id_/https://repo.example"))
        with self.store.db:
            self.store.db.execute("DELETE FROM fulltexts WHERE work_id = 1")
        wrong = self.run_one(located, answers, first="Labour supply of mothers in Norway: a panel study of wages")
        self.assertEqual(wrong["status"], "failed")
        self.assertIn("another publication", wrong["error"])

    def test_nothing_located_and_no_fallback_stays_no_open_access(self):
        self.assertEqual(self.run_one([], {})["status"], "no_open_access")

    def test_a_settled_record_is_not_fetched_again_but_one_stopped_by_an_error_is(self):
        self.store.set_fulltext(1, "no_open_access")
        self.store.set_fulltext(2, "no_open_access", error="core: core: HTTP 429: ")
        asked = []
        with mock.patch.object(fulltext, "_retrieve_one",
                               lambda http, w, *a: asked.append(w["work_id"]) or {"status": "no_open_access"}):
            counts = fulltext.retrieve(self.store, [1, 2], self.settings(), "Z", self.run, root=self.root,
                                       log=lambda *_: None)
        self.assertEqual((asked, counts["already settled (no_open_access)"]), ([2], 1))
        with self.store.db:
            self.store.db.execute("UPDATE fulltexts SET updated_at = '2026-10-01T00:00:00' WHERE work_id = 1")
        self.assertFalse(fulltext.settled(self.store.get_fulltext(1)))   # before CORE joined: asked again


class Matching(unittest.TestCase):
    def work(self, title, doi=None):
        return {"title": title, "doi": doi}

    def test_same_document_by_doi_or_most_title_words(self):
        w = self.work(TITLE, "10.1080/abc")
        self.assertTrue(fulltext.same_document(w, "Population Studies https://doi.org/10.1080/ABC. Received"))
        self.assertTrue(fulltext.same_document(w, "GRANDPARENTAL CHILDCARE AND THE TIMING OF SECOND BIRTHS IN "
                                                  "EUROPE A. Author"))
        self.assertFalse(fulltext.same_document(w, "Childcare costs and second births: evidence from Sweden"))
        self.assertTrue(fulltext.same_document(w, "   "))                  # unreadable: left to the parser
        self.assertFalse(fulltext.same_document(self.work("Fertility"), "Mortality decline in Europe"))

    def test_a_page_link_that_is_plainly_its_pdf(self):
        page = (b'<a href="/about">About</a><a href="https://other.org/x.pdf">PDF</a>'
                b'<a class="btn" href="/bitstream/123/paper.pdf?sequence=1">Download <b>PDF</b></a>')
        self.assertEqual(fulltext.pdf_link(page, "https://repo.example.org/handle/123"),
                         "https://repo.example.org/bitstream/123/paper.pdf?sequence=1")
        self.assertIsNone(fulltext.pdf_link(b'<a href="/files/data.csv">Download data</a>', "https://r.org/i"))

    def test_crossref_links_only_under_an_open_licence(self):
        http = FakeHttp(None, {})
        open_msg = {"message": {"license": [{"URL": "http://creativecommons.org/licenses/by/4.0/"}],
                                "link": [{"URL": "https://pub.example/a.pdf", "content-type": "application/pdf",
                                          "intended-application": "text-mining"},
                                         {"URL": "https://pub.example/sim", "content-type": "application/pdf",
                                          "intended-application": "similarity-checking"}]}}
        closed = {"message": {"license": [{"URL": "https://www.elsevier.com/tdm/userlicense/1.0/"}],
                              "link": [{"URL": "https://api.elsevier.com/x", "content-type": "text/xml"}]}}
        with mock.patch.object(FakeHttp, "get_json", lambda self, *a, **k: open_msg):
            self.assertEqual([c.url for c in fulltext.crossref_links(http, "10.1/a")], ["https://pub.example/a.pdf"])
        with mock.patch.object(FakeHttp, "get_json", lambda self, *a, **k: closed):
            self.assertEqual(fulltext.crossref_links(http, "10.1/b"), [])


class Coverage(unittest.TestCase):
    def test_coverage_counts_abstracts_full_texts_and_neither(self):
        store = Store(":memory:")
        a = store.upsert_work({"title": "A", "abstract": LONG})
        b = store.upsert_work({"title": "B"})
        c = store.upsert_work({"title": "C"})
        store.set_fulltext(b, "parsed", "core", sections=[{"head": "Body", "text": "t"}])
        self.assertEqual(texts.coverage(store, [a, b, c]), {"records": 3, "abstract": 1, "fulltext": 1, "neither": 1})


if __name__ == "__main__":
    unittest.main()
