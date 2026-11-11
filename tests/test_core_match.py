"""CORE repository records can carry a DOI they only cite; a DOI match must also agree on the title."""

import unittest

from ichnos.sources.core import same_work, titles_agree

WORK = {"doi": "10.1016/s0005-7894(04)80003-0", "year": 2004,
        "title": "DBT, FAP, and ACT: How empirically oriented are the new behavior therapy technologies?"}


class CoreMatch(unittest.TestCase):
    def test_doi_with_another_title_is_rejected(self):
        thesis = {"doi": WORK["doi"], "yearPublished": 2020,
                  "title": "Efecto de un protocolo breve online basado en la terapia de aceptacion y compromiso"}
        self.assertFalse(same_work(WORK, thesis))

    def test_doi_with_the_same_title_is_accepted(self):
        same = {"doi": WORK["doi"], "yearPublished": 2006, "title": WORK["title"]}
        self.assertTrue(same_work(WORK, same))
        self.assertTrue(same_work(WORK, {"doi": WORK["doi"], "title": None}))

    def test_titles_agree(self):
        self.assertTrue(titles_agree("Sleep and mood in adolescents", "Sleep and mood in adolescents: a cohort"))
        self.assertFalse(titles_agree("Sleep and mood in adolescents", "Market prices of copper"))


if __name__ == "__main__":
    unittest.main()
