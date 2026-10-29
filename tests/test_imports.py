import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from ichnos import imports, query
from ichnos.config import ROOT

EBSCO_RIS = """TY  - JOUR
AU  - Doe, Jane
AU  - Roe, Rick
T1  - Purpose in life across the retirement transition
JO  - Journal of Aging Studies
Y1  - 2021///
N2  - We followed retirees over six waves
  and modelled latent trajectories.
DO  - 10.1016/J.JAGING.2021.100001
AN  - 2021-12345-001
DB  - APA PsycInfo
LA  - English
ER  -
"""

SCOPUS_RIS = """TY  - JOUR
AU  - Smith J.
TI  - Meaning in life after job loss
T2  - Journal of Vocational Behavior
PY  - 2019
AB  - Two-wave panel of job losers.
UR  - https://www.scopus.com/inward/record.uri?eid=2-s2.0-85061234567
DO  - 10.1016/j.jvb.2019.01.002
ER  -

TY  - JOUR
TI  - Work identity among NEET youth
PY  - 2023
UR  - https://doi.org/10.1177/00018726231234567.
ER  -

TY  - JOUR
AU  - Nobody N.
PY  - 2020
ER  -
"""

SCOPUS_CSV = ('﻿"Authors","Author full names","Title","Year","Source title","DOI","Abstract","EID",'
              '"PubMed ID","Document Type","Language of Original Document"\n'
              '"Smith, J.; Doe, A.","Smith, John; Doe, Anna","Meaning in life after job loss, a ""panel"" study",'
              '"2019","Journal of Vocational Behavior","10.1016/j.jvb.2019.01.002","[No abstract available]",'
              '"2-s2.0-85061234567","31234567","Article","English"\n')

WOS_PLAIN = """FN Clarivate Analytics Web of Science
VR 1.0
PT J
AU Smith, J
   Doe, A
AF Smith, John
   Doe, Anna
TI Meaning in life after job loss: a
   two-wave panel
SO JOURNAL OF VOCATIONAL BEHAVIOR
LA English
DT Article
AB Two waves of data.
DI 10.1016/j.jvb.2019.01.002
PY 2019
UT WOS:000123456700001
PM 31234567
ER

EF
"""

WOS_TAB = ("PT\tAU\tAF\tTI\tSO\tLA\tDT\tAB\tDI\tPY\tUT\tPM\n"
           'J\tSmith, J; Doe, A\tSmith, John; Doe, Anna\t"Meaning" after job loss\tJ VOC BEHAV\tEnglish\tArticle\t'
           "Two waves.\t10.1016/j.jvb.2019.01.002\t2019\tWOS:000123456700001\t31234567\n")


class TempDir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name: str, text: str, encoding: str = "utf-8") -> Path:
        path = self.dir / name
        path.write_bytes(text.encode(encoding))
        return path


class Parsing(TempDir):
    def test_ebsco_ris(self):
        fmt, recs, notes = imports.parse_file(self.write("psycinfo.ris", EBSCO_RIS))
        self.assertEqual((fmt, len(recs), notes), ("ris", 1, []))
        r = recs[0]
        self.assertEqual(r["title"], "Purpose in life across the retirement transition")
        self.assertEqual(r["abstract"], "We followed retirees over six waves and modelled latent trajectories.")
        self.assertEqual((r["doi"], r["year"], r["venue"]), ("10.1016/j.jaging.2021.100001", 2021, "Journal of Aging Studies"))
        self.assertEqual((r["source_id"], r["authors"], r["language"]), ("2021-12345-001", ["Doe, Jane", "Roe, Rick"], "English"))

    def test_scopus_ris_doi_from_link_and_untitled_skipped(self):
        fmt, recs, notes = imports.parse_file(self.write("scopus.ris", SCOPUS_RIS))
        self.assertEqual([r["doi"] for r in recs], ["10.1016/j.jvb.2019.01.002", "10.1177/00018726231234567"])
        self.assertEqual(notes, ["1 records without a title skipped"])

    def test_scopus_csv(self):
        fmt, recs, _ = imports.parse_file(self.write("scopus.csv", SCOPUS_CSV))
        r = recs[0]
        self.assertEqual(fmt, "csv")
        self.assertEqual(r["title"], 'Meaning in life after job loss, a "panel" study')
        self.assertIsNone(r["abstract"])
        self.assertEqual((r["pmid"], r["source_id"], r["authors"]), ("31234567", "2-s2.0-85061234567", ["Smith, J.", "Doe, A."]))

    def test_wos_plain_text(self):
        fmt, recs, _ = imports.parse_file(self.write("savedrecs.txt", WOS_PLAIN))
        r = recs[0]
        self.assertEqual(fmt, "wos")
        self.assertEqual(r["title"], "Meaning in life after job loss: a two-wave panel")
        self.assertEqual((r["authors"], r["source_id"], r["pmid"]), (["Smith, John", "Doe, Anna"], "WOS:000123456700001", "31234567"))

    def test_wos_tab_delimited_utf16_keeps_quotes(self):
        fmt, recs, _ = imports.parse_file(self.write("savedrecs_tab.txt", WOS_TAB, "utf-16"))
        r = recs[0]
        self.assertEqual(fmt, "csv")
        self.assertEqual((r["title"], r["doi"], r["year"]), ('"Meaning" after job loss', "10.1016/j.jvb.2019.01.002", 2019))
        self.assertEqual(r["type"], "Article")

    def test_cp1252_fallback(self):
        fmt, recs, _ = imports.parse_file(self.write("old.ris", EBSCO_RIS.replace("Doe, Jane", "Müller, Jörg"), "cp1252"))
        self.assertEqual(recs[0]["authors"][0], "Müller, Jörg")

    def test_csv_without_title_column_is_an_error(self):
        with self.assertRaises(ValueError):
            imports.parse_file(self.write("bad.csv", "a,b\n1,2\n"))


class AddExport(TempDir):
    def setUp(self):
        super().setUp()
        (self.dir / "protocol").mkdir()
        shutil.copyfile(ROOT / "examples" / "corpus_a.toml", self.dir / "protocol" / "corpus_a.toml")
        self.export = self.write("psycinfo.ris", EBSCO_RIS)

    def add(self, **kw):
        args = dict(corpus="A", files=[self.export], source="psycinfo", platform="ebsco",
                    searched="2026-10-17", hits=3, root=self.dir, log=lambda *_: None)
        args.update(kw)
        return imports.add_export(**args)

    def protocol(self):
        return tomllib.loads((self.dir / "protocol" / "corpus_a.toml").read_text(encoding="utf-8"))

    def test_registers_entry_and_copies_file(self):
        self.add()
        entry = self.protocol()["imports"][-1]
        self.assertEqual((entry["source"], entry["platform"], entry["searched"], entry["reported_total"]),
                         ("psycinfo", "ebsco", "2026-10-17", 3))
        self.assertEqual(entry["files"], ["imports/corpus_a/psycinfo_2026-10-17_psycinfo.ris"])
        self.assertTrue((self.dir / entry["files"][0]).exists())
        tree = query.parse(self.protocol()["query"])
        self.assertEqual(entry["query"].strip(), query.render(tree, "ebsco"))

    def test_query_file_is_kept_verbatim(self):
        qf = self.write("ran.txt", "TI (purpose) AND AB (job loss) -- what I really ran\n")
        self.add(platform=None, query_file=qf, source="scopus")
        self.assertEqual(self.protocol()["imports"][-1]["query"].strip(),
                         "TI (purpose) AND AB (job loss) -- what I really ran")

    def test_refusals_leave_the_protocol_unchanged(self):
        before = (self.dir / "protocol" / "corpus_a.toml").read_text(encoding="utf-8")
        for kw in ({"source": "openalex"}, {"platform": None}, {"platform": "google"}, {"searched": "17.10.2026"},
                   {"source": "PsycINFO!"}, {"files": [self.write("empty.ris", "TY  - JOUR\nER  -\n")]}):
            with self.assertRaises(ValueError, msg=kw):
                self.add(**kw)
        self.assertEqual((self.dir / "protocol" / "corpus_a.toml").read_text(encoding="utf-8"), before)
        self.add()
        with self.assertRaises(ValueError):
            self.add()                                     # same source name twice


class Strings(unittest.TestCase):
    def test_library_dialects(self):
        tree = query.parse('("job loss" OR retire*) AND panel AND NOT review')
        self.assertEqual(query.render(tree, "ebsco"),
                         '(TI ("job loss" OR retire*) OR AB ("job loss" OR retire*)) AND (TI (panel) OR AB (panel))'
                         ' NOT (TI (review) OR AB (review))')
        self.assertEqual(query.render(tree, "ovid"),
                         '("job loss" or retire*).ti,ab. and (panel).ti,ab. not (review).ti,ab.')
        self.assertEqual(query.render(tree, "scopus"),
                         'TITLE-ABS("job loss" OR retire*) AND TITLE-ABS(panel) AND NOT TITLE-ABS(review)')
        self.assertEqual(query.render(tree, "wos"),
                         '(TI=("job loss" OR retire*) OR AB=("job loss" OR retire*)) AND (TI=(panel) OR AB=(panel))'
                         ' NOT (TI=(review) OR AB=(review))')

    def test_leading_not_refused_where_not_is_binary(self):
        for d in ("ebsco", "ovid", "wos"):
            with self.assertRaises(query.QueryError):
                query.render(query.parse("NOT review"), d)
        self.assertEqual(query.render(query.parse("NOT review"), "scopus"), "NOT TITLE-ABS(review)")


if __name__ == "__main__":
    unittest.main()
