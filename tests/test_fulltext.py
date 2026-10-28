import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from psyscientist import agents, fulltext
from psyscientist.config import Settings
from psyscientist.fulltext import Candidate
from psyscientist.http import ApiError
from psyscientist.store import Store
from tests.test_agents_map import PROTOCOL, Base

JATS = b"""<?xml version="1.0"?>
<article><front><article-meta><title-group><article-title>T</article-title></title-group></article-meta></front>
<body>
 <sec><title>Introduction</title><p>Why sleep matters.</p></sec>
 <sec><title>Methods</title>
  <sec><title>Measures</title><p>Mood was measured with the <italic>SMFQ</italic> at three waves.</p></sec>
  <sec><title>Analysis</title><p>Latent growth curves.</p></sec>
 </sec>
 <sec><title>Results</title><p>Two trajectory classes.</p></sec>
 <sec><title>References</title><p>Smith 2020.</p></sec>
</body></article>"""

TEI = b"""<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
<div><head n="2">Method</head><p>Participants were 400 teens.</p></div>
<div><head n="2.1">Measures</head><p>We used the SMFQ.</p></div>
</body></text></TEI>"""


class Parsing(unittest.TestCase):
    def test_jats_sections_keep_their_path(self):
        heads = [s["head"] for s in fulltext.parse_jats(JATS)]
        self.assertEqual(heads, ["Introduction", "Methods > Measures", "Methods > Analysis", "Results", "References"])

    def test_tei(self):
        s = fulltext.parse_tei(TEI)
        self.assertEqual([x["head"] for x in s], ["2 Method", "2.1 Measures"])

    def test_heading_split_for_pypdf_text(self):
        text = "Title line\nIntroduction\nSome intro text that is long enough to keep.\n2. Method\n" \
               "Participants completed the SMFQ at baseline and twelve months later.\nReferences\nSmith J."
        s = fulltext.split_sections(text)
        self.assertEqual([x["head"] for x in s], ["Introduction", "2. Method"])

    def test_methods_text_prefers_methods_skips_references_and_caps_length(self):
        text = fulltext.methods_text(fulltext.parse_jats(JATS))
        self.assertTrue(text.startswith("## Methods > Measures"))
        self.assertIn("## Results", text)
        self.assertNotIn("Smith 2020", text)
        self.assertNotIn("Why sleep", text)
        self.assertLessEqual(len(fulltext.methods_text(fulltext.parse_jats(JATS), limit=300)), 300)

    def test_a_text_without_sections_is_read_further(self):
        body = [{"head": "Body", "text": "Intro words. " * 100 + "RESULTS mood rose by half. " * 20}]
        self.assertIn("mood rose", fulltext.findings_text(body, limit=1400))          # 3 x 1400 reaches the results
        self.assertLessEqual(len(fulltext.findings_text(body, limit=1400)), 1400 * fulltext.UNDIVIDED)
        split = [{"head": "Introduction", "text": "Intro words. " * 20}, {"head": "Results", "text": "Mood rose. " * 40}]
        self.assertTrue(fulltext.findings_text(split, limit=1400).startswith("## Results"))
        # pypdf found "Introduction" and "Conclusion" only: the conclusion first, then the paper under "Introduction"
        lumped = [{"head": "Introduction", "text": "Intro words. " * 100 + "RESULTS mood rose by half. " * 20},
                  {"head": "Conclusion", "text": "In sum, it helped."}]
        text = fulltext.findings_text(lumped, limit=1400)
        self.assertTrue(text.startswith("## Conclusion"))
        self.assertIn("mood rose by half", text)


class Retrieval(Base):
    def settings(self):
        return Settings(None, None, None, self.root / "evidence.sqlite", grobid_url="http://127.0.0.1:9")

    def test_jats_first_then_pdf_failures_are_recorded(self):
        def fake_locate(http, work, settings, errors=None):
            if work["work_id"] == 1:
                return [Candidate("europepmc", "jats", "https://x/1.xml", "cc by", "publishedVersion")]
            if work["work_id"] == 2:
                return [Candidate("openalex", "pdf", "https://x/2.pdf", None, "acceptedVersion")]
            return []

        def fake_get(self, source, url, params, headers=None, keep_body=True):
            if url.endswith("1.xml"):
                return JATS
            return b"<html>landing page</html>"

        with mock.patch.object(fulltext, "locate", fake_locate), \
             mock.patch("psyscientist.http.Http.get", fake_get):
            counts = fulltext.retrieve(self.store, [1, 2, 3], self.settings(), "Z", self.run, root=self.root,
                                       log=lambda *_: None)
        self.assertEqual(counts, {"parsed (europepmc, jats)": 1, "failed": 1, "no_open_access": 1})
        row = self.store.get_fulltext(1)
        self.assertEqual((row["license"], row["parser"]), ("cc by", "jats"))
        self.assertTrue((self.root / row["file_path"]).exists())
        self.assertIn("not a PDF", self.store.get_fulltext(2)["error"])

    def test_a_page_naming_its_pdf_is_followed_and_a_bot_check_keeps_the_free_link(self):
        page = b'<html><head><meta name="citation_pdf_url" content="/bitstream/1/paper.pdf"></head></html>'

        def fake_locate(http, work, settings, errors=None):
            if work["work_id"] == 1:
                return [Candidate("openalex", "page", "https://repo.example.org/item/1", None, "acceptedVersion")]
            return [Candidate("unpaywall", "pdf", "https://publisher.example.com/2.pdf", "cc by", "publishedVersion")]

        def fake_get(self, source, url, params, headers=None, keep_body=True):
            if url == "https://repo.example.org/item/1":
                return page
            if url == "https://repo.example.org/bitstream/1/paper.pdf":
                return b"%PDF-1.7 ..."
            raise ApiError(source, 403, "web page: Just a moment...")

        with mock.patch.object(fulltext, "locate", fake_locate), \
             mock.patch("psyscientist.http.Http.get", fake_get), \
             mock.patch.object(fulltext, "parse_pdf", lambda pdf, grobid: ([{"head": "Results", "text": "x"}],
                                                                           "pypdf", None)):
            fulltext.retrieve(self.store, [1, 2], self.settings(), "Z", self.run, root=self.root, log=lambda *_: None)
        one, two = self.store.get_fulltext(1), self.store.get_fulltext(2)
        self.assertEqual((one["status"], one["url"]), ("parsed", "https://repo.example.org/bitstream/1/paper.pdf"))
        self.assertEqual((two["status"], two["url"]), ("failed", "https://publisher.example.com/2.pdf"))
        self.assertIn("bot check", two["error"])
        self.store.add_screening(self.run, 2, "Z", "title_abstract", "model:s", "include")
        from psyscientist import library
        row = next(r for r in library.todo(self.store, self.run, {}) if r["work_id"] == 2)
        self.assertEqual(list(row["links"])[0], "Free copy")

    def test_extraction_reads_the_full_text_and_quotes_from_it_count(self):
        self.store.add_screening(self.run, 1, "Z", "title_abstract", "model:s", "include")
        self.store.set_fulltext(1, "parsed", "europepmc", "https://x", "cc by", "publishedVersion", "f.xml",
                                "abc", "jats", fulltext.parse_jats(JATS))
        folder = agents.write_tasks(self.store, self.run, "extract", "model:e", root=self.root)
        batch = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertIn("SMFQ", batch[0]["fulltext"])
        self.assertIn("europepmc, jats", batch[0]["fulltext_from"])
        self.assertIn("Quotes may come from it", (folder / "instructions.md").read_text(encoding="utf-8"))
        answer = [{"work_id": 1, "features": {
            "country": {"value": "uk", "canonical": "uk", "evidence": "UK cohort"},
            "measure": {"value": "SMFQ", "canonical": "smfq", "evidence": "measured with the SMFQ at three waves"}},
            "claims": []}]
        (folder / "batch_01_result.json").write_text(json.dumps(answer), encoding="utf-8")
        counts = agents.import_task(folder, self.store)
        self.assertEqual((counts["evidence"], counts["grounded"]), (2, 2))


class PdfLinks(unittest.TestCase):
    def test_citation_pdf_url_then_alternate_link(self):
        meta = b"""<meta content='https://x.org/a.pdf?x=1&amp;y=2' name='citation_pdf_url'/>"""
        self.assertEqual(fulltext.pdf_link(meta, "https://x.org/item"), "https://x.org/a.pdf?x=1&y=2")
        alt = b'<link rel="alternate" type="application/pdf" href="files/b.pdf">'
        self.assertEqual(fulltext.pdf_link(alt, "https://x.org/item/"), "https://x.org/item/files/b.pdf")
        self.assertIsNone(fulltext.pdf_link(b'<meta name="citation_title" content="T">', "https://x.org"))


class HttpUrls(unittest.TestCase):
    def test_urls_with_query_strings_and_spaces(self):
        store = Store(":memory:")
        from psyscientist.http import Http
        seen = []

        def fake_urlopen(req, timeout=0):
            seen.append(req.full_url)
            raise OSError("offline")
        h = Http(store, None, max_retries=0)
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            for url, params in [("https://x/a b.pdf", {}), ("https://x/get?id=1", {"k": "v"})]:
                with self.assertRaises(ApiError):
                    h.get("t", url, params)
        self.assertEqual(seen, ["https://x/a%20b.pdf", "https://x/get?id=1&k=v"])


if __name__ == "__main__":
    unittest.main()
