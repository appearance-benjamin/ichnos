import unittest

from psyscientist import query
from psyscientist.config import load_protocol

CORPUS_A = load_protocol("A")[2]["query"]


class ParseAndRender(unittest.TestCase):
    def test_corpus_a_has_three_blocks(self):
        tree = query.parse(CORPUS_A)
        self.assertEqual([len(query.terms(b)) for b in query.blocks(tree)], [5, 8, 4])

    def test_renderings_reparse_to_the_same_string(self):
        tree = query.parse(CORPUS_A)
        sent = query.render(tree, "openalex")
        self.assertEqual(query.render(query.parse(sent), "openalex"), sent)
        self.assertEqual(len(query.terms(query.parse(sent))), len(query.terms(tree)))

    def test_openalex_phrases_lose_hyphens(self):
        tree = query.parse('("short-time work" OR "school-to-work") AND retire*')
        self.assertEqual(query.render(tree, "openalex"), '("short time work" OR "school to work") AND retire*')
        self.assertEqual(query.render(tree, "s2"), '("short-time work" | "school-to-work") + retire*')

    def test_s2_rendering(self):
        tree = query.parse('("job loss" OR retire*) AND NOT review')
        self.assertEqual(query.render(tree, "s2"), '("job loss" | retire*) + -review')

    def test_requires_explicit_operators(self):
        with self.assertRaises(query.QueryError):
            query.parse('"meaning in life" unemployment')

    def test_rejects_inner_wildcard_and_unbalanced(self):
        for bad in ["un*employ", '"latent function*"', "(a OR b", "a AND", ""]:
            with self.assertRaises(query.QueryError, msg=bad):
                query.parse(bad)


class LocalMatching(unittest.TestCase):
    def test_prefix_and_phrase(self):
        tree = query.parse('"meaning in life" AND (retire* OR "short-time work")')
        self.assertTrue(query.matches(tree, "Meaning in life after retirement: a panel study"))
        self.assertTrue(query.matches(tree, "meaning in life during short time work"))
        self.assertFalse(query.matches(tree, "Meaning of life after retirement"))

    def test_word_boundaries(self):
        tree = query.parse("panel")
        self.assertTrue(query.matches(tree, "a household panel"))
        self.assertFalse(query.matches(tree, "panels"))
        self.assertFalse(query.matches(tree, "expanel"))

    def test_corpus_a_on_a_plausible_abstract(self):
        tree = query.parse(CORPUS_A)
        text = ("Purpose in life trajectories after job loss: evidence from a German household panel. "
                "We follow unemployed adults over five waves.")
        self.assertTrue(query.matches(tree, text))
        self.assertFalse(query.matches(tree, "Purpose in life after job loss: a cross-sectional survey"))


if __name__ == "__main__":
    unittest.main()
