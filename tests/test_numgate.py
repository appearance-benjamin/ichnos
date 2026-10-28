"""The number gate: same number, same statistic, same variable."""

import unittest

from psyscientist import numgate

F4 = numgate.finding_text(
    "Work centrality and commitment went with pre-job-loss grief", "β = 0.16; p = 0.04", None,
    "Elapsed time (β = 0.13; p = 0.048), financial strain (β = 0.18; p = 0.007), communication (β = -0.22), "
    "work-centrality (β = 0.33) and commitment (β = 0.16) were associated with grief.")
F32 = numgate.finding_text("No different change in meaning in life", "F(1,2720) = 0.18, p = 0.673", None,
                           "did not predict change in meaning in life, F(1,2720) = 0.18")


class Gate(unittest.TestCase):
    def test_the_same_statistic_passes(self):
        self.assertEqual(numgate.gate("Financial strain went with grief (β = 0.18).", [F4]), {})
        self.assertEqual(numgate.gate("Meaning did not change differently, F(1,2720) = 0.18.", [F32]), {})
        self.assertEqual(numgate.gate("Commitment went with grief (β = .16).", [F4]), {})

    def test_another_statistic_or_df_is_flagged(self):
        self.assertEqual(numgate.gate("Strain went with grief (β = 0.18).", [F32]),
                         {"statistic": ["0.18 (beta in the text, F in the source)"]})
        self.assertEqual(numgate.gate("The sample had n = 2720 job losers.", [F32]), {"df_as_n": ["2720"]})

    def test_a_value_named_for_the_wrong_variable_is_ambiguous(self):
        flags = numgate.gate("Calling went with grief (β = 0.18).", [F4])
        self.assertIn("ambiguous", flags)

    def test_missing_and_exempt(self):
        self.assertEqual(numgate.gate("Meaning fell by 37.5% in 36 studies.", [F4], exempt={"36"}),
                         {"missing": ["37.5%"]})
        self.assertEqual(numgate.gate("Three studies, 2 waves.", [F4]), {})

    def test_reading_numbers(self):
        self.assertEqual([(v, s) for v, s, _ in numgate.stats("F(2,197) = 3.60, β = 0,18, n = 445")],
                         [("2", "df"), ("197", "df"), ("3.60", "F"), ("0.18", "beta"), ("445", "n")])


if __name__ == "__main__":
    unittest.main()
