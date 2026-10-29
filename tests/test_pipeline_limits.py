"""Task folders of more than 99 batches, exclusions by a source's own record id, organisation names that are not data
sets, the store's wait for another writer, and a record charted and coded in two corpora."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ichnos import agents, tables
from ichnos.store import BUSY_TIMEOUT_S, Store

PROTOCOL = '''
corpus = "Y"
title = "Toy corpus: assessment stakes"
version = "test"
query = """"pass/fail" AND students"""
sources = []

[screening]
stage = "title_abstract"
rule = "Include studies that compare pass/fail with graded assessment."
decisions = ["include", "exclude", "unsure"]
exclude_reasons = ["not_empirical", "no_stakes", "other"]

[[screening.metrics]]
id = "empirical"
question = "Original data?"
fail_reason = "not_empirical"

[[screening.metrics]]
id = "stakes"
question = "Compares stakes?"
fail_reason = "no_stakes"

[[exclusions]]
source = "eric"
source_id = "EJ0001"
date = "2026-10-20"
who = "the reviewer"
reason = "duplicate report: an ERIC record, without DOI, of a retained article"
'''

RECORDS = [
    ("Pass/fail grading and stress in medical students", "Students at the National Resident Matching Program (NRMP) "
     "era schools graded pass/fail reported less stress. Data came from the Health and Retirement Study (HRS)."),
    ("Step 1 pass/fail and research output", "Applicants in the National Resident Matching Program (NRMP) published "
     "more after Step 1 went pass/fail."),
    ("Retirement and learning in older students", "We used the Health and Retirement Study (HRS) to compare graded "
     "and ungraded courses."),
    ("Grading and stress (ERIC record)", "An ERIC copy of the first record, without DOI."),
]


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "protocol").mkdir()
        (self.root / "protocol" / "corpus_y.toml").write_text(PROTOCOL, encoding="utf-8")
        self.saved, agents.ROOT = agents.ROOT, self.root
        self.store = Store(self.root / "evidence.sqlite")
        sha = self.store.add_protocol("Y", "protocol/corpus_y.toml", PROTOCOL)
        self.run = self.store.start_run("pilot", "Y", sha, {})
        self.ids = []
        for n, (title, abstract) in enumerate(RECORDS, 1):
            rec = {"title": title, "abstract": abstract, "year": 2020 if n < 4 else 2021}
            if n < 4:
                rec["doi"] = f"10.9/{n}"
            w = self.store.upsert_work(rec)
            self.store.add_retrieval(self.run, w, "eric" if n == 4 else "openalex", "EJ0001" if n == 4 else str(n), n)
            self.ids.append(w)
        self.store.add_sample(self.run, self.ids, "pilot_screening")

    def tearDown(self):
        agents.ROOT = self.saved
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)


class ManyBatches(Base):
    def test_batch_files_come_in_numeric_order_without_results(self):
        folder = self.root / "task"
        folder.mkdir()
        for n in (1, 2, 10, 99, 100, 101):
            (folder / f"batch_{n:02d}.json").write_text("[]", encoding="utf-8")
        (folder / "batch_100_result.json").write_text("[]", encoding="utf-8")
        self.assertEqual([p.name for p in agents.batch_files(folder)],
                         ["batch_01.json", "batch_02.json", "batch_10.json", "batch_99.json", "batch_100.json",
                          "batch_101.json"])
        self.assertEqual(agents.task_state(folder), "partly answered")

    def test_a_three_digit_batch_is_imported(self):
        folder = agents.write_tasks(self.store, self.run, "screen", "model:test", root=self.root, batch_size=1)
        last = agents.batch_files(folder)[-1]
        last.rename(folder / "batch_100.json")       # a task of more than 99 batches names its batches this way
        for batch in agents.batch_files(folder):
            records = json.loads(batch.read_text(encoding="utf-8"))
            batch.with_name(batch.stem + "_result.json").write_text(json.dumps([
                {"work_id": r["work_id"], "kind": "study",
                 "scores": {"empirical": {"score": 2, "evidence": r["title"][:12]},
                            "stakes": {"score": 1, "evidence": ""}}} for r in records]), encoding="utf-8")
        self.assertEqual(agents.task_state(folder), "answered")
        counts = agents.import_task(folder, self.store)
        self.assertEqual(counts["problems"], [])
        self.assertEqual(counts["stored"], 4)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM screening").fetchone()[0], 4)


class ExclusionBySourceId(Base):
    def test_a_record_without_any_identifier_is_excluded_by_its_eric_id(self):
        for w in self.ids:
            self.store.add_screening(self.run, w, "Y", "title_abstract", "model:x", "include")
        self.assertEqual(list(agents.protocol_exclusions(self.store, self.run)), [self.ids[3]])
        self.assertEqual(agents.kept_ids(self.store, self.run), self.ids[:3])

    def test_the_source_must_match_when_it_is_named(self):
        (self.root / "protocol" / "corpus_y.toml").write_text(
            PROTOCOL.replace('source = "eric"', 'source = "openalex"'), encoding="utf-8")
        agents._EXCLUSIONS.clear()
        self.assertEqual(agents.protocol_exclusions(self.store, self.run), {})


class OrganisationsAreNotDataSets(Base):
    def test_a_shared_data_set_links_reports_but_an_organisation_does_not(self):
        for w in self.ids[:3]:
            self.store.add_screening(self.run, w, "Y", "title_abstract", "model:x", "include")
        self.store.add_screening(self.run, self.ids[3], "Y", "title_abstract", "model:x", "exclude", "other")
        ov = tables.overlaps(self.store, self.run, {"paper": {"overlap_by_authors": False}})
        self.assertEqual(ov["datasets"], {"HRS": "Health and Retirement Study"})
        groups = [sorted(g["works"]) for g in ov["groups"]]
        self.assertEqual(groups, [[self.ids[0], self.ids[2]]])


class BusyTimeout(unittest.TestCase):
    def test_the_store_waits_for_another_writer(self):
        root = Path(tempfile.mkdtemp())
        try:
            store = Store(root / "evidence.sqlite")
            self.assertEqual(store.db.execute("PRAGMA busy_timeout").fetchone()[0], int(BUSY_TIMEOUT_S * 1000))
            store.close()
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TwoCorporaOneRecord(unittest.TestCase):
    """A record charted and coded in two corpora by the same extractor keeps each corpus's values."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.store = Store(self.root / "evidence.sqlite")
        self.c = self.store.start_run("pilot", "C", None, {})
        self.d = self.store.start_run("pilot", "D", None, {})
        self.w = self.store.upsert_work({"title": "Perceived stakes of assessment", "doi": "10.9/shared"})

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def test_charting_in_one_run_leaves_the_other_run_alone(self):
        for run, value in ((self.c, "undergraduate_medicine"), (self.d, "non_health_higher_education")):
            self.store.add_feature(run, self.w, "model:x", "setting", value, value, None, None)
        self.assertEqual(tables.charting(self.store, self.c)[self.w]["setting"]["values"], ["undergraduate_medicine"])
        self.assertEqual(tables.charting(self.store, self.d)[self.w]["setting"]["values"],
                         ["non_health_higher_education"])

    def test_findings_in_one_run_do_not_supersede_the_other_runs(self):
        row = {"finding": "Learners felt the stakes were high.", "evidence": "felt the stakes were high"}
        first = self.store.replace_findings(self.c, self.w, "model:x", [row])
        self.store.replace_findings(self.d, self.w, "model:x", [row])
        status = self.store.db.execute("SELECT status FROM findings WHERE finding_id = ?", first).fetchone()[0]
        self.assertEqual(status, "extracted")
        again = self.store.replace_findings(self.c, self.w, "model:x", [row])     # re-extraction in the same run
        self.assertEqual(self.store.db.execute("SELECT status FROM findings WHERE finding_id = ?", first).fetchone()[0],
                         "superseded")
        self.assertNotEqual(again, first)

    def test_an_older_store_is_migrated_with_its_rows(self):
        path = self.root / "old.sqlite"
        old = Store(path)
        run = old.start_run("pilot", "C", None, {})
        w = old.upsert_work({"title": "An old record", "doi": "10.9/old"})
        old.add_feature(run, w, "model:x", "setting", "a", "a", None, None)
        sql = old.db.execute("SELECT sql FROM sqlite_master WHERE name = 'study_features'").fetchone()[0]
        old._rebuild("study_features", sql.replace("PRIMARY KEY (run_id, work_id, extractor, dimension)",
                                                   "PRIMARY KEY (work_id, extractor, dimension)"))
        old.close()
        new = Store(path)
        sql = new.db.execute("SELECT sql FROM sqlite_master WHERE name = 'study_features'").fetchone()[0]
        self.assertIn("PRIMARY KEY (run_id, work_id, extractor, dimension)", sql)
        self.assertEqual(new.db.execute("SELECT count(*) FROM study_features").fetchone()[0], 1)
        new.close()


if __name__ == "__main__":
    unittest.main()
