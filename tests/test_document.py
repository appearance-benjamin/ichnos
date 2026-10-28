"""The manuscript renderer: Figure 1's flow table is drawn as a flow diagram; other tables stay tables."""

import unittest

from psyscientist import document

FLOW = """**Figure 1.** PRISMA-ScR flow diagram: the flow of records.

| Stage | Records | Excluded or set aside |
| --- | --- | --- |
| Identification | 2,000 records | 500 duplicates removed |
| Screening | 1,500 records screened | 1,200 excluded |
| Included | 86 core studies | – |

**Table 1.** Something else.

| A | B |
| --- | --- |
| 1 | 2 |
"""


class FlowDiagram(unittest.TestCase):
    def test_the_flow_table_after_a_figure_caption_is_drawn(self):
        for layout in ("paper", "screen"):
            h = document.to_html(FLOW, layout=layout)
            self.assertIn('<div class="flow">', h)
            self.assertEqual(h.count('class="box"'), 3)          # one box per stage
            self.assertEqual(h.count('class="side"'), 2)         # what left at two stages; none at the last
            self.assertEqual(h.count("&darr;"), 2)
            self.assertEqual(h.count("<table>"), 1)               # the other table stays a table

    def test_a_flow_table_without_its_figure_caption_stays_a_table(self):
        h = document.to_html(FLOW.split("\n", 2)[2], layout="paper")
        self.assertNotIn('<div class="flow">', h)
        self.assertEqual(h.count("<table>"), 2)


if __name__ == "__main__":
    unittest.main()
