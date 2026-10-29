"""Tables added after the third editorial decision on corpus B: counts by sample and by text source, codes split
across cells, each finding of a subset with its effect, and the repository records with the check for a published
version."""

import unittest
from unittest import mock

from ichnos import tables
from ichnos.store import Store

PROTOCOL = {
    "map": {"dimensions": [
        {"name": "anxiety_type", "categories": ["ai_anxiety", "climate_eco_anxiety"]},
        {"name": "process", "categories": ["mindfulness", "none"]},
        {"name": "design", "categories": ["cross_sectional"]}, {"name": "population"}],
        "match_across": "anxiety_type", "tree": ["anxiety_type", "process"]},
    "findings": {"fields": [
        {"name": "anxiety_type", "categories": ["ai_anxiety", "climate_eco_anxiety"]},
        {"name": "outcome", "categories": ["depression", "well_being"]},
        {"name": "direction", "categories": ["positive_association", "negative_association", "no_association"]}]},
}


class RoundFourTables(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.run = self.store.start_run("pilot", "T", None, {})
        studies = {
            "A": ("ai_anxiety", "none", [("ai_anxiety", "depression", "positive_association; no_association")]),
            "B": ("ai_anxiety", "none", [("ai_anxiety", "depression", "positive_association")]),
            "C": ("climate_eco_anxiety", "mindfulness", [("climate_eco_anxiety", "well_being", "negative_association")]),
        }
        self.ids = {}
        for name, (anx, proc, finds) in studies.items():
            w = self.store.upsert_work({"title": f"Study {name} of anxiety", "year": 2020, "doi": f"10.1/{name.lower()}",
                                        "authors": [{"A": "Ann Smith", "B": "Bob Jones", "C": "Cy Brown"}[name]]})
            self.ids[name] = w
            for d, v in (("anxiety_type", anx), ("process", proc), ("design", "cross_sectional"),
                         ("population", f"{name} 100 students")):
                self.store.add_feature(self.run, w, "model:c", d, v, v, None, None)
            self.store.replace_findings(self.run, w, "model:f", [
                {"finding": f"{o} {d}", "evidence": "passage", "effect": "r = .30" if name == "B" else None,
                 "fields": {"anxiety_type": a, "outcome": o, "direction": d}} for a, o, d in finds])
        self.store.set_fulltext(self.ids["A"], "parsed")
        # A and B may share a sample
        self.ov = {"root": {self.ids["A"]: self.ids["A"], self.ids["B"]: self.ids["A"], self.ids["C"]: self.ids["C"]}}

    def test_count_table_gives_full_text_counts_samples_and_splits_codes(self):
        text = tables.count_table(self.store, self.run, PROTOCOL, "outcome", "direction", "Table 5",
                                  where={"anxiety_type": ["ai_anxiety"]}, ov=self.ov, text=True,
                                  overlaps_label="Table 6")
        self.assertIn("Number of studies with findings", text)            # not "core studies" without a core subset
        self.assertIn("In brackets, how many of the studies in a cell were charted from a full text", text)
        self.assertIn("reports that may share a sample (Table 6) once", text)
        self.assertIn("| Outcome | positive association | no association | Studies | Samples |", text)
        self.assertIn("| depression | 2 (1) | 1 (1) | 2 (1) | 1 |", text)    # A counts under both of its codes
        self.assertNotIn("well being", text)                               # climate finding left out by `where`
        plain = tables.count_table(self.store, self.run, PROTOCOL, "outcome", "direction", "Table 5")
        self.assertIn("| depression | 2 | 0 | 1 | 2 |", plain)
        self.assertNotIn("Samples", plain)

    def test_evidence_counts_give_studies_and_samples_per_row(self):
        text = tables.evidence_counts(self.store, self.run, PROTOCOL, {self.ids["C"]}, "Table 7", "Table S3", self.ov)
        self.assertIn("| ai anxiety | 0 | 2 (0) | 2 (0) | 1 |", text)
        self.assertIn("| climate eco anxiety | 1 (1) | 0 | 1 (1) | 1 |", text)

    def test_findings_list_shows_each_finding_with_its_effect(self):
        spec = {"title": "AI-anxiety findings", "where": {"anxiety_type": ["ai_anxiety"]}}
        text = tables.findings_list_table(self.store, self.run, PROTOCOL, spec, "Table S8")
        self.assertIn("the 2 findings from 2 studies coded anxiety type ai anxiety", text)
        self.assertIn("| Jones, 2020 | cross sectional | B 100 students | depression | positive association | "
                      "depression positive_association | r = .30 | title only |", text)
        self.assertIn("| full text |", text)
        self.assertNotIn("Brown", text)

    def test_repository_table_lists_the_check_and_decisions(self):
        protocol = dict(PROTOCOL, repository={"date": "2026-10-19", "who": "the operator", "records": [
            {"doi": "10.1/a", "kind": "data deposit", "published": "10.1/b", "decision": "excluded: copy"},
            {"doi": "10.1/c", "kind": "repository preprint", "published": "", "decision": "kept"},
            {"doi": "10.9/none", "kind": "data deposit", "published": "", "decision": "kept"}]})
        kept, dropped = [self.ids["B"], self.ids["C"]], {self.ids["A"]: {}}
        with mock.patch("ichnos.agents.kept_ids", return_value=kept), \
                mock.patch("ichnos.agents.sheet_ids", return_value=list(self.ids.values())), \
                mock.patch("ichnos.agents.protocol_exclusions", return_value=dropped):
            rows, n = tables.repository_records(self.store, self.run, protocol)
            text = tables.repository_table(self.store, self.run, protocol, "Table S7")
        self.assertEqual((n["all"], n["excluded"], n["retained"], n["deposit"], n["found"], n["findings"]),
                         (2, 1, 1, 1, 1, 1))
        self.assertIn("The 2 records hosted by a repository", text)
        self.assertIn("| Smith, 2020 | repository | data deposit | – | Jones, 2020 (retained) | excluded | "
                      "excluded: copy |", text)
        self.assertIn("| Brown, 2020 | repository | repository preprint | 1 | none found | retained | kept |", text)

    def test_overlap_caption_follows_the_protocol(self):
        ov = {"groups": [{"works": {self.ids["A"], self.ids["B"]}, "basis": ["same abstract"]}],
              "meta": tables.works(self.store, self.ids.values())}
        self.assertIn("shared authors alone do not group reports",
                      tables.overlap_table(ov, set(), "Table 6", by_authors=False))
        self.assertIn("or shared authors.", tables.overlap_table(ov, set(), "Table 6"))


if __name__ == "__main__":
    unittest.main()
