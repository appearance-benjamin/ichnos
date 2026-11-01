"""Human checks of the findings behind the headline claims: saving, the Declaration, the next revision."""

import json

from ichnos import agents, checks, document, library, paper, tables, writer
from ichnos.app import App
from ichnos.config import Settings
from ichnos.review import build_data
from tests.test_paper import Papers


class HumanChecks(Papers):
    def write_paper(self):
        self.findings_task()
        folder = agents.write_tasks(self.store, self.run, "paper", "model:writer", root=self.root)
        f1 = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))["findings"][0]["id"]
        return f1, self.answer(folder, self.manuscript(f1))["paper"]

    def test_headline_findings_and_validation(self):
        f1, pid = self.write_paper()
        row = checks.latest_paper(self.store, self.run)
        items = checks.headline_findings(self.store, row, {})
        self.assertEqual([i["id"] for i in items], [f1])
        self.assertEqual([u["part"] for u in items[0]["uses"]], ["Abstract", "Conclusion"])
        with self.assertRaises(ValueError):
            checks.record(self.store, f1, "human:", "confirmed", "passage")
        with self.assertRaises(ValueError):
            checks.record(self.store, f1, "human:ann", "confirmed", None)
        with self.assertRaises(ValueError):
            checks.record(self.store, f1, "human:ann", "corrected", "passage")
        with self.assertRaises(ValueError):
            checks.record(self.store, f1, "human:ann", "maybe", "passage")

    def test_declaration_appendix_and_revision_follow_the_checks(self):
        f1, pid = self.write_paper()
        text = paper.render(self.store, pid, full=True)
        self.assertIn("No person has yet checked the 1 extracted finding", text)
        self.assertNotIn("responsible for all content", text)
        self.assertIn("**Competing interests.** [To be stated by the author.]", text)

        checks.record(self.store, f1, "human:ann", "confirmed", "passage", "fine", paper_id=pid)
        self.assertIn("Ann checked 1 of the 1 findings behind the headline claims", paper.render(self.store, pid))
        checks.record(self.store, f1, "human:ann", "", paper_id=pid)                # withdrawn
        self.assertEqual(checks.latest(self.store, [f1]), {})

        checks.record(self.store, f1, "human:ann", "corrected", "full text", correction="Mood improved only in girls.")
        text = paper.render(self.store, pid, full=True)
        self.assertIn("0 confirmed and 1 corrected, as listed in the appendix", text)
        self.assertIn("## Appendix: human checks of the headline findings", text)
        self.assertIn("Correction: Mood improved only in girls.", text)
        self.assertNotIn("## Appendix: human checks", paper.render(self.store, pid, full=False))

        audit = agents.write_tasks(self.store, self.run, "audit", "model:auditor", root=self.root)
        items = json.loads((audit / "batch_01.json").read_text(encoding="utf-8"))["sentences"]
        self.assertTrue(any(c.get("human_check", {}).get("status") == "corrected" for i in items for c in i["cited"]))
        self.store.add_screening(self.run, 1, "Z", "title_abstract", "model:editor", "include")
        meta = agents.write_tasks(self.store, self.run, "meta", "model:editor", root=self.root)
        (meta / "batch_01_result.json").write_text(json.dumps({"summary": "s", "decision": "major_revision",
                                                               "required_changes": []}), encoding="utf-8")
        agents.import_task(meta, self.store)
        rev = agents.write_tasks(self.store, self.run, "revise", "model:writer", root=self.root)
        data = json.loads((rev / "batch_01.json").read_text(encoding="utf-8"))
        checked = [f for f in data["findings"] if f.get("human_check")]
        self.assertEqual(checked[0]["human_check"]["correction"], "Mood improved only in girls.")
        self.assertIn("`correction` is what the source says", (rev / "instructions.md").read_text(encoding="utf-8"))

    def test_an_essay_prints_a_source_again_only_after_a_sentence_without_it(self):
        f1, pid = self.write_paper()
        row = self.store.db.execute("SELECT content, method FROM papers WHERE paper_id = ?", (pid,)).fetchone()
        content, method = json.loads(row["content"]), json.loads(row["method"])
        s = [{"text": t, "cites": [f1]} for t in ("Sleep went with mood.", "It did so in girls.")]
        content["sections"] = [
            {"name": "Argument", "title": "Argument: sleep carries mood", "paragraphs": [{"sentences": s + [
                {"text": "Rivals agree.", "background": ["B1"]}, {"text": "Mood followed sleep.", "cites": [f1]}]}]},
            {"name": "Method", "paragraphs": [{"sentences": [{"text": "Two studies.", "method": ["M1"]}]}]}]
        self.store.db.execute("UPDATE papers SET content = ?, method = ? WHERE paper_id = ?",
                              (json.dumps(content), json.dumps(dict(method, aim_layout="essay")), pid))
        text = paper.render(self.store, pid, full=False)
        para = next(line for line in text.splitlines() if line.startswith("Sleep went with mood"))
        label = para[para.index("(") + 1:para.index(")")]
        self.assertEqual(para.count(f"({label})"), 2)          # the first sentence and the last, not the second
        self.assertIn("It did so in girls. Rivals", para)
        self.assertIn("## 1. Argument: sleep carries mood", text)
        abstract = text[text.index("## Abstract"):text.index("## 1.")]
        self.assertIn("Mood improved as sleep lengthened.", abstract)
        self.assertNotIn(f"({label})", abstract)
        self.assertIn("## Appendix: The evidence review", text)
        self.assertLess(text.index("## Declarations"), text.index("**Declaration of AI use:**"))

    def test_the_page_and_the_app_save_checks(self):
        f1, pid = self.write_paper()
        page = build_data(self.store, self.run)
        self.assertEqual(page["check"]["summary"]["headline"], 1)
        app = App(Settings(None, None, None, self.root / "evidence.sqlite"), self.root, "Tester")
        with self.assertRaises(ValueError):
            app.finding_check(self.store, {"finding_id": f1, "status": "confirmed", "against": "passage"})
        out = app.finding_check(self.store, {"finding_id": f1, "paper": pid, "status": "confirmed",
                                             "against": "full text", "reviewer": "Ann B"})
        self.assertEqual((out["check"]["status"], out["check"]["checker"]), ("confirmed", "human:ann_b"))
        self.assertEqual(build_data(self.store, self.run)["check"]["summary"]["full_text"], 1)
        full, _ = paper.write_files(self.store, pid, self.root)
        page = full.with_suffix(".html").read_text(encoding="utf-8")
        self.assertIn("<h2>Abstract</h2>", page)
        self.assertIn("<table>", page)
        self.assertIn("<strong>Declaration of AI use:</strong>", page)
        source = "\n\n".join(["# T", "**Authors:** Ann Bee (Example University); Cy Dee", "## Abstract", "Text.",
                              "## References", "- Ref one."])
        printed = document.to_html(source, "T", layout="paper")
        self.assertIn('<div class="name">Ann Bee</div><div class="aff">Example University</div>', printed)
        self.assertIn('<p class="abstract">Text.</p>', printed)
        self.assertIn('<ul class="refs">', printed)
        self.assertEqual(paper.author_line({"name": "Ann Bee", "affiliation": "EU"}), "Ann Bee (EU)")

    def test_reextraction_keeps_old_findings_for_old_papers(self):
        f1, pid = self.write_paper()
        checks.record(self.store, f1, "human:ann", "confirmed", "passage")
        row = self.store.db.execute("SELECT * FROM findings WHERE finding_id = ?", (f1,)).fetchone()
        new = self.store.replace_findings(self.run, row["work_id"], row["extractor"], [
            {"finding": "From the full text: mood improved.", "evidence": "the SMFQ fell", "source": "fulltext"}])
        self.assertEqual(self.store.db.execute("SELECT status FROM findings WHERE finding_id = ?", (f1,)).fetchone()[0],
                         "superseded")
        current = [f["id"] for f in writer.build_input(self.store, self.run, {})["findings"]]
        self.assertIn(new[0], current)
        self.assertNotIn(f1, current)
        self.assertIn("“Sleep Duration and Mood,” 2020", paper.render(self.store, pid))   # still cited (no authors: APA title label)
        self.assertEqual(list(checks.latest(self.store, [f1])), [f1])         # and the check keeps its history


class Library(HumanChecks):
    def test_todo_and_matching(self):
        self.findings_task()
        rows = library.todo(self.store, self.run, {})
        self.assertEqual({r["work_id"] for r in rows}, {1, 2, 4})
        self.assertTrue(all("Google Scholar" in r["links"] for r in rows))
        with self.store.db:
            self.store.db.execute("UPDATE works SET doi = '10.1234/sleep.4' WHERE work_id = 4")
        works = tables.works(self.store, [1, 2, 4])
        self.assertEqual(library.match("2_insomnia", b"%PDF-", works), (2, "file name"))
        doi = works[4]["doi"]
        library._first_pages = lambda pdf, n=2: f"Journal of Sleep. https://doi.org/{doi.upper()}. Received 2020."
        try:
            self.assertEqual(library.match("download", b"%PDF-", works), (4, "DOI"))
            title = works[1]["title"]
            library._first_pages = lambda pdf, n=2: f"Original article {title.upper()} by A. Author"
            self.assertEqual(library.match("download (1)", b"%PDF-", works), (1, "title"))
            library._first_pages = lambda pdf, n=2: "something else entirely"
            self.assertEqual(library.match("download (2)", b"%PDF-", works), (None, ""))
        finally:
            import importlib
            importlib.reload(library)
        md, page, n = library.write_todo(self.store, self.run, {}, self.root)
        self.assertEqual(n, 3)
        self.assertTrue((self.root / "fulltext" / "inbox").is_dir())
        self.assertIn('target="_blank" rel="noopener">Google Scholar</a>', page.read_text(encoding="utf-8"))

    def test_your_marked_studies_come_first_with_a_library_link(self):
        self.findings_task()
        self.store.add_screening(self.run, 4, "Z", "title_abstract", "human:reviewer", "include")
        rows = library.todo(self.store, self.run, {}, "https://ezproxy.example.edu/login?url=")
        self.assertEqual((rows[0]["work_id"], rows[0]["group"]), (4, 0))
        self.assertEqual(rows[0]["links"]["Library"], "https://ezproxy.example.edu/login?url=https://doi.org/10.9/4")
        self.assertEqual(library.links({"doi": "10.1/x"}, "https://proxy.example.org/login?qurl={url}")["Library"],
                         "https://proxy.example.org/login?qurl=https%3A%2F%2Fdoi.org%2F10.1%2Fx")
        self.assertNotIn("Library", library.links({"doi": "10.1/x"}))                # no proxy set: no library link

    def test_a_download_named_by_its_doi_is_not_taken_for_a_record_number(self):
        works = {1472: {"doi": "10.1/a", "title": "Something"}, 20: {"doi": "10.1/b", "title": "Other"}}
        library._first_pages = lambda pdf, n=2: ""
        try:
            self.assertEqual(library.match("1472-6920-14-197", b"%PDF-", works), (None, ""))
            self.assertEqual(library.match("20_smith 2021", b"%PDF-", works), (20, "file name"))
            self.assertEqual(library.match("20", b"%PDF-", works, by_name=False), (None, ""))
            generic = {1897: {"doi": "10.1007/978-3-032-12952-9_21", "title": "Competency-Based Assessment"}}
            body = ("The reliability and validity of a portfolio designed as a programmatic assessment of performance "
                    "in an integrated clinical placement. " + "Background text. " * 80 +
                    "Keywords: competency-based assessment")
            library._first_pages = lambda pdf, n=2: body
            self.assertEqual(library.match("1472-6920-14-197", b"%PDF-", generic, by_name=False), (None, ""))
        finally:
            import importlib
            importlib.reload(library)

    def test_pdfs_in_downloads_are_copied_when_they_match(self):
        self.findings_task()
        with self.store.db:
            self.store.db.execute("UPDATE works SET doi = '10.1234/sleep.4' WHERE work_id = 4")
        downloads = self.root / "Downloads"
        downloads.mkdir()
        (downloads / "article.pdf").write_bytes(b"%PDF-1.7 the sleep paper")
        (downloads / "holiday.pdf").write_bytes(b"%PDF-1.7 something else")
        settings = Settings(openalex_api_key=None, s2_api_key=None, contact_email=None, db_path=self.root / "x",
                            downloads_dir=downloads)
        library._first_pages = lambda pdf, n=2: "https://doi.org/10.1234/sleep.4" if b"sleep" in pdf else "nothing"
        library.parse_pdf = lambda pdf, grobid: ([{"head": "Results", "text": "The SMFQ fell."}], "pypdf", None)
        library.grobid_url = lambda settings: None
        try:
            result = library.add_all(self.store, settings, self.root, log=lambda *a: None)
            self.assertEqual(result[self.run]["added"], [4])
            self.assertEqual(result[self.run]["gained"], [4])
            self.assertTrue((downloads / "article.pdf").exists())                      # copied, never moved
            self.assertEqual(self.store.get_fulltext(4)["source"], "library")
            again = library.add_all(self.store, settings, self.root, log=lambda *a: None)
            self.assertEqual((again[self.run]["added"], again[self.run]["downloads_read"]), ([], 0))
            self.assertTrue(any("--works 4" in c for c in library.next_steps(self.store, self.run, [4])))

            # the PDF turns out to be another article that cites the record: set aside, never matched to it again
            library.reject(self.store, 4, "another article citing it", self.root)
            self.assertEqual(self.store.get_fulltext(4)["status"], "failed")
            self.assertIn(4, {r["work_id"] for r in library.todo(self.store, self.run, {})})
            (self.root / library.CHECKED).unlink()           # so that the downloads are compared again
            third = library.add_all(self.store, settings, self.root, log=lambda *a: None)
            self.assertEqual(third[self.run]["added"], [])
            (downloads / "right.pdf").write_bytes(b"%PDF-1.7 the sleep paper itself")     # the right one, later
            fourth = library.add_all(self.store, settings, self.root, log=lambda *a: None)
            self.assertEqual(fourth[self.run]["added"], [4])
        finally:
            import importlib
            importlib.reload(library)
