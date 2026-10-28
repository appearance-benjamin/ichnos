"""End to end with the network stubbed out: run, fill the sheet, import, report."""

import csv
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from psyscientist import imports, pilot
from psyscientist.config import ROOT, Settings
from psyscientist.http import ApiError
from psyscientist.sources import SearchResult
from psyscientist.store import Store

ABSTRACT = "We follow unemployed adults over five panel waves and model purpose in life trajectories."


def fake_openalex(http, query, cap, *args):
    recs = [{"source_id": f"W{i}", "openalex_id": f"W{i}", "doi": f"10.1/{i}",
             "title": f"Purpose in life after job loss, study {i}", "abstract": ABSTRACT,
             "year": 2015 + i % 8, "venue": "Journal of Vocational Behavior", "authors": ["A"]}
            for i in range(30)]
    return SearchResult("openalex", query, 30, recs)


def fake_s2(http, query, cap, *args):
    recs = [{"source_id": f"S{i}", "s2_id": f"S{i}", "doi": f"10.1/{i}",
             "title": f"Purpose in life after job loss, study {i}", "abstract": None, "year": 2015 + i % 8}
            for i in range(20, 40)]
    return SearchResult("semantic_scholar", query, 20, recs)


class PilotFlow(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "protocol").mkdir()
        text = (ROOT / "examples" / "corpus_a.toml").read_text(encoding="utf-8")
        text = text.replace("sample_size = 200", "sample_size = 25")
        text = re.sub(r"(?m)^sources = .*$", 'sources = ["openalex", "semantic_scholar"]', text)
        (self.root / "protocol" / "corpus_a.toml").write_text(text, encoding="utf-8")
        self.settings = Settings(None, None, None, self.root / "data" / "evidence.sqlite")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def run_pilot(self, s2=fake_s2, full_sheet=False):
        with mock.patch.object(pilot.openalex, "search", fake_openalex), \
             mock.patch.object(pilot.semantic_scholar, "search", s2):
            return pilot.run_pilot("A", self.settings, root=self.root, log=lambda *_: None, full_sheet=full_sheet)

    def test_merge_sample_and_report(self):
        out = self.run_pilot()
        store = Store(self.settings.db_path)
        self.assertEqual(store.db.execute("SELECT count(*) FROM works").fetchone()[0], 40)
        self.assertEqual(store.db.execute("SELECT count(*) FROM samples").fetchone()[0], 25)
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("**40 unique records**", report)
        self.assertIn("| openalex+semantic_scholar | 10 |", report)
        self.assertIn("a seeded random draw of 25 from the 40 not on an earlier sheet", report)
        store.close()

    def test_same_seed_same_sheet(self):
        first = (self.run_pilot() / "screening_sheet.csv").read_text(encoding="utf-8-sig")
        second = (self.run_pilot(full_sheet=True) / "screening_sheet.csv").read_text(encoding="utf-8-sig")
        self.assertEqual(first, second)

    def test_later_runs_leave_screened_records_off_the_sheet(self):
        self.run_pilot()                                   # sheet: 25 of 40
        out = self.run_pilot()                             # same pool again
        sheet = (out / "screening_sheet.csv").read_text(encoding="utf-8-sig").splitlines()
        self.assertEqual(len(sheet) - 1, 15)               # only the 15 never on a sheet
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("**0 records are new**", report)
        self.assertIn("The other 25 keep the decisions made on those sheets", report)

    def test_library_export_merges_and_only_new_records_reach_the_sheet(self):
        self.run_pilot()                                   # sheet: 25 of the 40 API records
        export = self.root / "psycinfo.ris"
        export.write_text("TY  - JOUR\nTI  - Purpose in life after job loss, study 3\nDO  - 10.1/3\nPY  - 2018\nER  - \n"
                          "TY  - JOUR\nTI  - Sense of purpose after furlough: a panel study\n"
                          "AB  - Furloughed workers over three waves.\nPY  - 2022\nER  - \n", encoding="utf-8")
        imports.add_export("A", [export], "psycinfo", platform="ebsco", searched="2026-10-17", hits=5,
                           root=self.root, log=lambda *_: None)
        out = self.run_pilot()
        store = Store(self.settings.db_path)
        run_id = int(out.name.split("_")[1])
        self.assertEqual(store.db.execute("SELECT count(*) FROM works").fetchone()[0], 41)
        src = store.db.execute("SELECT * FROM run_sources WHERE run_id = ? AND source = 'psycinfo'",
                               (run_id,)).fetchone()
        self.assertEqual((src["reported_total"], src["fetched"], src["status"]), (5, 2, "truncated"))
        self.assertTrue(src["query"].startswith("(TI ("))
        logged = store.db.execute("SELECT * FROM calls WHERE run_id = ? AND kind = 'import'", (run_id,)).fetchone()
        self.assertEqual(store.call_response(logged["call_id"]), export.read_bytes())
        found = {r[0] for r in store.db.execute(
            "SELECT w.doi FROM retrievals r JOIN works w ON w.work_id = r.work_id WHERE r.run_id = ? AND r.source = 'psycinfo'",
            (run_id,))}
        self.assertIn("10.1/3", found)                     # merged with the OpenAlex record by DOI
        with (out / "screening_sheet.csv").open(encoding="utf-8-sig", newline="") as fh:
            sheet = list(csv.DictReader(fh, delimiter=";"))
        self.assertEqual(len(sheet), 16)                   # 15 never on a sheet + 1 new from the export
        self.assertIn("Sense of purpose after furlough: a panel study", {r["title"] for r in sheet})
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("Library export from ebsco, searched 2026-10-17: 1 file(s), ris", report)
        store.close()

    def test_failed_source_is_reported_not_fatal(self):
        def broken(*args):
            raise ApiError("semantic_scholar", 429, "Too Many Requests")
        out = self.run_pilot(s2=broken)
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("status **partial**", report)
        self.assertIn("failed: semantic_scholar: HTTP 429", report)

    def test_import_screening_round_trip(self):
        out = self.run_pilot()
        sheet = out / "screening_sheet.csv"
        with sheet.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter=";"))
        for i, row in enumerate(rows):
            row["decision"] = ["include", "exclude", "unsure", ""][i % 4]
            row["exclude_reason"] = "cross_sectional" if row["decision"] == "exclude" else ""
        rows[0]["decision"], rows[0]["exclude_reason"] = "exclude", "bogus"
        # Saved the way German Excel does it: plain CSV, Windows code page.
        with sheet.open("w", encoding="cp1252", newline="", errors="replace") as fh:
            writer = csv.DictWriter(fh, fieldnames=pilot.SHEET_COLUMNS, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)

        counts = pilot.import_screening(sheet, self.settings, log=lambda *_: None)
        self.assertEqual(counts["invalid"], 1)
        self.assertEqual(counts["blank"], 6)
        self.assertEqual(counts["imported"], 18)
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("**human:reviewer**: 18 of 25 screened.", report)
        self.assertIn("Exclusion reasons: cross_sectional 6.", report)

    def test_import_accepts_the_review_page_export(self):
        out = self.run_pilot()
        ids = [int(r.split(";")[0]) for r in (out / "screening_sheet.csv").read_text(encoding="utf-8-sig").splitlines()[1:3]]
        export = self.root / f"screening_decisions_corpus_A_{out.name}.csv"
        export.write_text("﻿work_id;decision;exclude_reason;note\r\n"
                          f"{ids[0]};include;;\r\n{ids[1]};exclude;review;\"a; b\"\r\n", encoding="utf-8")
        counts = pilot.import_screening(export, self.settings, log=lambda *_: None)
        self.assertEqual(counts, {"imported": 2, "blank": 0, "invalid": 0})

    def test_agreement_stays_hidden_until_the_sheet_is_complete(self):
        out = self.run_pilot()
        store = Store(self.settings.db_path)
        run_id = int(out.name.split("_")[1])
        ids = sorted(r[0] for r in store.db.execute("SELECT work_id FROM samples WHERE run_id = ?", (run_id,)))
        for w in ids:
            store.add_screening(run_id, w, "A", "title_abstract", "model:test", "exclude", "wrong_setting")
        for w in ids[:10]:
            store.add_screening(run_id, w, "A", "title_abstract", "human:reviewer", "include")
        pilot.write_report(store, run_id, out / "report.md")
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("Progress: human:reviewer 10 of 25, model:test 25 of 25.", report)
        self.assertFalse((out / "disagreements.csv").exists())

        for w in ids[10:]:
            store.add_screening(run_id, w, "A", "title_abstract", "human:reviewer", "exclude", "review")
        pilot.write_report(store, run_id, out / "report.md")
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("**human:reviewer** (rows) against **model:test** (columns), 25 records", report)
        self.assertIn("model:test excluded 10 records that human:reviewer kept", report)
        self.assertIn("Both excluded 15, giving different reasons for 15.", report)
        with (out / "disagreements.csv").open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh, delimiter=";"))
        self.assertEqual(len(rows), 10)
        self.assertEqual((rows[0]["a_decision"], rows[0]["b_reason"]), ("include", "wrong_setting"))
        store.close()


if __name__ == "__main__":
    unittest.main()


def fake_eric(http, query, cap, *args):
    recs = [{"source_id": f"EJ{i}", "doi": f"10.1/{i}" if i < 32 else None,
             "title": f"Purpose in life after job loss, study {i}", "abstract": ABSTRACT, "year": 2020}
            for i in range(28, 34)]
    return SearchResult("eric", query, 6, recs)


class AddSources(PilotFlow):
    def test_a_source_added_later_puts_only_new_records_on_the_same_sheet(self):
        self.run_pilot(full_sheet=True)
        path = self.root / "protocol" / "corpus_a.toml"
        path.write_text(path.read_text(encoding="utf-8").replace(
            'sources = ["openalex", "semantic_scholar"]', 'sources = ["openalex", "semantic_scholar", "eric"]'),
            encoding="utf-8")
        store = Store(self.settings.db_path)
        before = store.db.execute("SELECT count(*) FROM samples WHERE run_id = 1").fetchone()[0]
        with mock.patch.object(pilot.eric, "search", fake_eric):
            new = pilot.add_sources(store, 1, self.settings, self.root, log=lambda *_: None)
            self.assertEqual(pilot.add_sources(store, 1, self.settings, self.root, log=lambda *_: None), [])
        self.assertEqual(len(new), 2)                     # 28-31 merge by DOI with records already on the sheet
        after = store.db.execute("SELECT count(*) FROM samples WHERE run_id = 1").fetchone()[0]
        self.assertEqual(after, before + 2)
        row = store.db.execute("SELECT * FROM run_sources WHERE run_id = 1 AND source = 'eric'").fetchone()
        self.assertEqual((row["reported_total"], row["fetched"]), (6, 6))
        self.assertIn("after the run's other sources", row["notes"])
        found = {r[0] for r in store.db.execute("SELECT work_id FROM retrievals WHERE source = 'eric'")}
        self.assertEqual(len(found), 6)
        store.close()


RIS = """TY  - JOUR
TI  - Purpose in life after job loss, study 35
AB  - A panel study of adults who lost their jobs, following purpose in life over five waves with growth models.
DO  - 10.1/35
PY  - 2018
ER  - 
TY  - JOUR
TI  - A study only the library database found
AB  - Workers who retired early reported more purpose in a three-wave cohort, especially after leaving strenuous jobs.
DO  - 10.1/library-only
PY  - 2022
ER  - 
"""


class ExportIntoRun(PilotFlow):
    def test_a_library_export_joins_the_run_and_fills_missing_abstracts(self):
        self.run_pilot(full_sheet=True)
        store = Store(self.settings.db_path)
        before = store.db.execute("SELECT abstract FROM works WHERE doi = '10.1/35'").fetchone()[0]
        self.assertFalse(before)                          # Semantic Scholar gave no abstract
        export = self.root / "scopus.ris"
        export.write_text(RIS, encoding="utf-8")
        imports.add_export("A", [export], "scopus", "scopus", hits=2, root=self.root, log=lambda *_: None)
        stats = {}
        new = pilot.add_sources(store, 1, self.settings, self.root, log=lambda *_: None, stats=stats)
        self.assertEqual(len(new), 1)                      # the library-only record
        self.assertEqual(stats["abstracts"], 1)
        self.assertIn("purpose in life over five waves",
                      store.db.execute("SELECT abstract FROM works WHERE doi = '10.1/35'").fetchone()[0])
        row = store.db.execute("SELECT status, fetched FROM run_sources WHERE run_id = 1 AND source = 'scopus'").fetchone()
        self.assertEqual((row["status"], row["fetched"]), ("ok", 2))
        self.assertEqual(pilot.add_sources(store, 1, self.settings, self.root, log=lambda *_: None), [])
        store.close()
