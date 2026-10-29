"""Title keys: a ligature or full-width letter from a PDF keys like the plain letters, so the merge finds the twin."""

import unittest

from ichnos.store import Store, title_key


class TitleKeyTest(unittest.TestCase):
    def test_ligature(self):
        self.assertEqual(title_key("Does artiﬁcial intelligence (AI) anxiety increase deviant behavior?"),
                         title_key("Does artificial intelligence (AI) anxiety increase deviant behavior?"))

    def test_merge_finds_ligature_twin(self):
        store = Store(":memory:")
        a = store.upsert_work({"title": "Does artificial intelligence anxiety increase deviant behavior", "year": 2025})
        b = store.upsert_work({"title": "Does artiﬁcial intelligence anxiety increase deviant behavior"})
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
