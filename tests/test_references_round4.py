"""Reference fixes after the round-4 editorial decision on corpus B: author strings as the sources garble them,
names in capitals, entities, works without authors (APA: listed by title) and titles in capitals."""

import json
import unittest

from ichnos import writer


class Names(unittest.TestCase):
    def test_garbled_author_strings_are_cleaned(self):
        clean = writer.clean_names
        self.assertEqual(clean(["Sidra Raza, Shumaila Naz, Shiza Kazmi"]), ["Sidra Raza", "Shumaila Naz", "Shiza Kazmi"])
        self.assertEqual(clean(["Mahrukh Akhtar,Attaya Hassan,Dr. Mahboob Ullah"]),
                         ["Mahrukh Akhtar", "Attaya Hassan", "Mahboob Ullah"])
        self.assertEqual(clean(["Riam Rahim  Abd         Prof. Dr. Haider Lazim Khunaisir"]),
                         ["Riam Rahim Abd", "Haider Lazim Khunaisir"])
        self.assertEqual(clean(["Ankita Kumari Dr"]), ["Ankita Kumari"])
        self.assertEqual(clean(["Gennadii DZHEGUR", "Adem ÖZDEMİR", "ZEKİ BİRCAN", "'AISYAH WARDATUL FIRDAUS"]),
                         ["Gennadii Dzhegur", "Adem Özdemir", "Zeki Bircan", "Aisyah Wardatul Firdaus"])
        # left as they are: family-comma names, PubMed initials, a single token
        self.assertEqual(clean(["Kaur, Sukhwinder", "McNamara, N", "Farb NAS", "WHO"]),
                         ["Kaur, Sukhwinder", "McNamara, N", "Farb NAS", "WHO"])
        self.assertEqual(writer.apa_authors(json.dumps(["Manish Raj", "Ankita Kumari Dr"])), "Raj, M., & Kumari, A.")

    def test_repository_ids_and_double_family_names(self):
        # corpus E, round 1: a repository's person id became the surname; a double family name lost its first part
        self.assertEqual(writer.cite_label(json.dumps(["R Layfield (7743791)"]), 2025), "Layfield, 2025")
        self.assertEqual(writer.name_parts("Monique Borgerhoff Mulder"), ("Borgerhoff Mulder", "Monique"))
        self.assertEqual(writer.name_parts("Raoul A. Mulder"), ("Mulder", "Raoul A."))


class WorksWithoutAuthors(unittest.TestCase):
    def test_listed_and_cited_by_title(self):
        work = {"authors": "[]", "year": 2025, "title": "NEUROTICISM AND ARTIFICIAL INTELLIGENCE ANXIETY. THE ROLE",
                "venue": "Psychological Applications &amp; Trends", "type": "conference-paper", "doi": "10.1/x",
                "abstract": "We studied AI anxiety."}
        self.assertEqual(writer.cite_label("[]", 2025, work["title"]), "“Neuroticism and Artificial Intelligence,” 2025")
        self.assertEqual(writer.cite_label("[]", 2025), "Anonymous, 2025")          # no title known
        ref = writer.reference(work)
        self.assertTrue(ref.startswith("Neuroticism and artificial intelligence anxiety. The role. (2025)."), ref)
        self.assertIn("*Psychological Applications & Trends*", ref)

    def test_titles_in_capitals_keep_acronyms_and_proper_nouns(self):
        work = {"authors": '["Mahrukh Akhtar"]', "year": 2026, "type": "article", "doi": None, "venue": None,
                "title": "AI ANXIETY TO AI AGENCY IN AI-ENABLED PAKISTANI ORGANIZATIONS",
                "abstract": "Employees in Pakistan use AI tools."}
        self.assertIn("AI anxiety to AI agency in AI-enabled Pakistani organizations.",
                      writer.reference(work, None, {"Pakistani"}))
        self.assertEqual(writer.cite_label(json.dumps(["Sidra Raza, Shumaila Naz, Shiza Kazmi"]), 2026),
                         "Raza et al., 2026")


class IndexVenues(unittest.TestCase):
    def test_the_index_a_record_came_through_is_not_its_journal(self):
        work = {"authors": '["Wei Zhai"]', "year": 2025, "type": "article", "doi": "10.12114/x", "title": "A study",
                "venue": "DOAJ (DOAJ: Directory of Open Access Journals)", "abstract": ""}
        self.assertNotIn("DOAJ", writer.reference(work))
        bib = {"container": "Chinese General Practice", "volume": "28", "issue": "1", "pages": "1–5",
               "publisher": None, "kind": "journal-article"}
        self.assertIn("*Chinese General Practice*", writer.reference(work, bib))      # Crossref's journal, if known


if __name__ == "__main__":
    unittest.main()
