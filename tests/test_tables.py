"""Generated tables: eligibility at charting, abbreviations, overlaps, references and record fixes."""

import json
import unittest

from ichnos import tables, writer
from ichnos.store import Store

SETTING = {"id": "setting", "dimension": "setting", "values": ["retirement", "job_loss"], "metric": "setting"}
EMPIRICAL = {"id": "empirical", "metric": "empirical"}


class Judging(unittest.TestCase):
    def test_record_types_and_unconfirmed_quotes(self):
        crit = dict(EMPIRICAL, fail_types=["dataset"])
        self.assertEqual(tables.judge(crit, None, {"empirical": 2}, "dataset"), "no")
        unconfirmed = {"setting": {"values": ["retirement"], "text": "x", "grounded": 0}}
        self.assertEqual(tables.judge(SETTING, unconfirmed, {"setting": 2}), "unclear")
        self.assertEqual(tables.judge(SETTING, unconfirmed, {"setting": 0}), "no")

    def test_charting_with_the_screening_score_as_a_check(self):
        chart = {"setting": {"values": ["retirement"], "text": "retirees", "grounded": 1}}
        self.assertEqual(tables.judge(SETTING, chart, {"setting": 2}), "yes")
        self.assertEqual(tables.judge(SETTING, chart, {"setting": 0}), "screen")
        other = {"setting": {"values": ["other"], "text": "older adults", "grounded": 1}}
        self.assertEqual(tables.judge(SETTING, other, {"setting": 2}), "no")

    def test_unknown_charting_falls_back_to_the_score(self):
        self.assertEqual(tables.judge(SETTING, None, {"setting": 2}), "yes")
        self.assertEqual(tables.judge(SETTING, None, {"setting": 1}), "unclear")
        unconfirmed = {"setting": {"values": ["retirement"], "text": "x", "grounded": 0}}
        self.assertEqual(tables.judge(SETTING, unconfirmed, {"setting": 1}), "unclear")
        blank = {"setting": {"values": ["not_reported"], "text": "", "grounded": 1}}
        self.assertEqual(tables.judge(SETTING, blank, {"setting": 0}), "no")
        self.assertEqual(tables.judge(EMPIRICAL, None, {"empirical": 0}), "no")


class Abbreviations(unittest.TestCase):
    def test_names_must_spell_the_acronym(self):
        text = ("Data from the US Health and Retirement Study (HRS) and Midlife in the United States (MIDUS); "
                "professors at the University of Santa Maria (UFSM) in a vocational rehabilitation programme (VRP).")
        found = tables.abbreviations(text)
        self.assertEqual(found["HRS"], "Health and Retirement Study")
        self.assertEqual(found["MIDUS"], "Midlife in the United States")
        self.assertEqual(found["VRP"], "vocational rehabilitation programme")
        self.assertNotIn("UFSM", found)


class References(unittest.TestCase):
    def test_names(self):
        self.assertEqual(writer.cite_label('["McNamara, N"]', 2025), "McNamara, 2025")
        self.assertEqual(writer.cite_label('["Janske H. W. van Eersel", "Paul A. Boelen"]', 2024),
                         "van Eersel & Boelen, 2024")
        self.assertEqual(writer.apa_authors('["Janske H. W. van Eersel", "Jean-Paul Sartre", "JS Mill"]'),
                         "van Eersel, J. H. W., Sartre, J.-P., & Mill, J. S.")
        self.assertEqual(writer.apa_authors('["Huzaifah"]'), "Huzaifah")
        self.assertEqual(writer.cite_label('["Vercammen A", "Wray B", "Crider YS"]', 2023), "Vercammen et al., 2023")
        self.assertEqual(writer.apa_authors('["Xu b", "Farb NAS"]'), "Xu, B., & Farb, N. A. S.")
        self.assertEqual(writer.cite_label('["J. K.", "M. Sulphey"]', 2021), "K. & Sulphey, 2021")

    def test_entries(self):
        thesis = {"authors": '["Anh Thi Phuong Nguyen"]', "year": 2025, "title": "Uncovering the pathway.",
                  "venue": "Ghent University Academic Bibliography (Ghent University)", "type": "dissertation",
                  "doi": None}
        self.assertEqual(writer.reference(thesis), "Nguyen, A. T. P. (2025). Uncovering the pathway [Thesis]. "
                                                   "Ghent University Academic Bibliography.")
        abstract = dict(thesis, title="SENSE OF PURPOSE IN LIFE", venue=None, type="conference-abstract", doi="10.1/x")
        self.assertEqual(writer.reference(abstract), "Nguyen, A. T. P. (2025). Sense of purpose in life "
                                                     "[Conference abstract]. https://doi.org/10.1/x")
        article = dict(thesis, title="Crisis or opportunity?", venue="Psychological Science", type="article")
        self.assertIn("Crisis or opportunity? *Psychological Science*.", writer.reference(article))
        self.assertNotIn("**", writer.reference(dict(article, venue=None)))


class SentenceCase(unittest.TestCase):
    def test_titles(self):
        self.assertEqual(writer.sentence_case("The Effects of Retirement on Sense of Purpose in Life: Crisis or "
                                              "Opportunity?"),
                         "The effects of retirement on sense of purpose in life: Crisis or opportunity?")
        self.assertEqual(writer.sentence_case("Social Value in Contemporary Kenya", {"Kenya"}),
                         "Social value in contemporary Kenya")
        self.assertEqual(writer.sentence_case("COVID-19 and AI in the U.S. Labor Market"),
                         "COVID-19 and AI in the U.S. labor market")
        self.assertEqual(writer.sentence_case("After Retirement: is Meaning-Making Protective?"),
                         "After retirement: Is meaning-making protective?")
        lexicon = writer.case_lexicon(["Data from Kenya and from contemporary kenya-based firms; workers in Europe."])
        self.assertIn("Europe", lexicon)
        self.assertNotIn("Kenya", lexicon)

    def test_reference_with_crossref_details(self):
        work = {"authors": '["Ayse Yemiscigil"]', "year": 2021, "title": "Crisis or Opportunity?",
                "venue": "Psychological Science", "type": "article", "doi": "10.1/x", "abstract": "", "language": "en"}
        bib = {"container": "Psychological Science", "volume": "32", "issue": "11", "pages": "1856–1864",
               "publisher": None}
        self.assertEqual(writer.reference(work, bib), "Yemiscigil, A. (2021). Crisis or opportunity? *Psychological "
                                                      "Science*, *32*(11), 1856–1864. https://doi.org/10.1/x")
        german = dict(work, title="Der Prozess der Selbstständigkeit", language="de", type="dissertation", venue=None)
        self.assertIn("Der Prozess der Selbstständigkeit [Thesis].", writer.reference(german))


class Fixes(unittest.TestCase):
    def test_metadata_fix_by_doi(self):
        store = Store(":memory:")
        w = store.upsert_work({"title": "Early retirement", "year": 2008, "doi": "10.4321/ABC",
                               "authors": ["Kerstin Isaksson Gunn Johansson"]})
        protocol = {"metadata": {"fix": [{"doi": "10.4321/abc", "authors": ["Kerstin Isaksson", "Gunn Johansson"],
                                          "note": "run together"}]}}
        self.assertEqual(tables.apply_fixes(store, protocol), 1)
        self.assertEqual(tables.apply_fixes(store, protocol), 0)       # already applied
        row = store.db.execute("SELECT authors, year FROM works WHERE work_id = ?", (w,)).fetchone()
        self.assertEqual(json.loads(row["authors"]), ["Kerstin Isaksson", "Gunn Johansson"])
        with self.assertRaises(ValueError):
            tables.apply_fixes(store, {"metadata": {"fix": [{"authors": ["x"]}]}})


if __name__ == "__main__":
    unittest.main()
