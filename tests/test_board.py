"""The board's step 1: lane choice by coverage, the frozen file, logged searches and sources, screening, overlap,
the full-text list and PDF matching. No network: searches and DOI lookups are scripted."""

import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from ichnos import board
from ichnos.config import Settings
from ichnos.sources import SearchResult
from ichnos.store import Store

BOARD_FILE = '''
board = "t-board"
title = "Test board"

[rules]
min_types = 2

[[directions]]
id = "D1"
title = "Entry door"

[[directions]]
id = "D2"
title = "Retirement paradox"

[[propositions]]
id = "P1"
text = "Entry closes first"
directions = ["D1", "D2"]

[[propositions]]
id = "P2"
text = "Retirements are not refilled"
directions = ["D2"]

[[propositions]]
id = "P3"
text = "A side remark"
directions = ["D1"]
load_bearing = false

[[lanes]]
id = "hist"
type = "historical"
covers = ["P1", "P2"]
against = ["P1"]
queries = [{ source = "openalex", query = "telephone operators automation", cap = 3 }]

[[lanes]]
id = "demo"
type = "demography"
covers = ["P1", "P2"]
against = ["P2"]
queries = [{ source = "openalex", query = "replacement demand retirement", cap = 3 }]

[[lanes]]
id = "hist2"
type = "historical"
covers = ["P1"]

[[lanes]]
id = "law"
type = "institutional"
covers = ["P3"]
drop = "out of scope for this round"
'''

SETTINGS = Settings(openalex_api_key=None, s2_api_key=None, contact_email=None, db_path=Path(":memory:"),
                    downloads_dir=None)


def fake_query(http, source, query, cap, settings):
    recs = [{"title": f"{query} study {i}", "year": 2000 + i, "doi": f"10.1/{query.split()[0]}{i}",
             "source_id": f"W{i}"} for i in range(cap)]
    if query.startswith("replacement"):          # shares one record with the history lane
        recs[0] = {"title": "telephone operators automation study 0", "year": 2000, "doi": "10.1/telephone0",
                   "source_id": "W0"}
    return SearchResult(source, query, 100, recs)


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "protocol").mkdir()
        self.path = self.root / "protocol" / "board_t-board.toml"
        self.path.write_text(BOARD_FILE, encoding="utf-8")
        self.cfg = tomllib.loads(BOARD_FILE)
        self.store = Store(self.root / "evidence.sqlite")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def started(self):
        board.start(self.store, self.path, self.root)
        with mock.patch.object(board, "run_query", fake_query):
            board.search(self.store, "t-board", SETTINGS, http=object(), root=self.root)


class Plan(Base):
    def test_valid_file(self):
        self.assertEqual(board.validate(self.cfg), [])

    def test_chooses_smallest_covering_set_and_explains_drops(self):
        p = board.plan(self.cfg)
        self.assertEqual(p["chosen"], ["hist", "demo"])
        self.assertEqual(p["gaps"], [])
        self.assertIn("not needed", p["reasons"]["hist2"])
        self.assertIn("dropped by the author", p["reasons"]["law"])
        text = board.plan_text(self.cfg, p)
        self.assertIn("CHOSEN  hist", text)
        self.assertIn("Gaps: none", text)

    def test_reports_gap_when_a_type_is_missing(self):
        cfg = tomllib.loads(BOARD_FILE.replace('type = "demography"', 'type = "historical"'))
        p = board.plan(cfg)
        rules = {(g["proposition"], g["rule"]) for g in p["gaps"]}
        self.assertEqual(rules, {("P1", "types"), ("P2", "types")})
        self.assertIn("demography", next(g["text"] for g in p["gaps"] if g["proposition"] == "P2"))

    def test_reports_missing_against_lane(self):
        cfg = tomllib.loads(BOARD_FILE.replace('against = ["P2"]', 'against = []'))
        self.assertIn(("P2", "against"), {(g["proposition"], g["rule"]) for g in board.plan(cfg)["gaps"]})

    def test_requires_a_lane_type(self):
        cfg = tomllib.loads(BOARD_FILE.replace('text = "Entry closes first"',
                                               'text = "Entry closes first"\nrequires = ["computational"]'))
        gap = next(g for g in board.plan(cfg)["gaps"] if g["rule"] == "requires")
        self.assertEqual(gap["proposition"], "P1")
        cfg["lanes"].append({"id": "model", "type": "computational", "covers": ["P1"]})
        p = board.plan(cfg)
        self.assertIn("model", p["chosen"])
        self.assertEqual(p["gaps"], [])
        cfg["propositions"][0]["requires"] = ["tarot"]
        self.assertIn("unknown lane types: tarot", "\n".join(board.validate(cfg)))

    def test_keep_forces_a_lane(self):
        cfg = tomllib.loads(BOARD_FILE.replace('id = "hist2"\ntype = "historical"',
                                               'id = "hist2"\ntype = "historical"\nkeep = true'))
        self.assertIn("hist2", board.plan(cfg)["chosen"])

    def test_validation_errors(self):
        bad = tomllib.loads(BOARD_FILE.replace('covers = ["P1"]\n', 'covers = ["P9"]\n')
                            .replace('type = "institutional"', 'type = "astrology"'))
        errors = "\n".join(board.validate(bad))
        self.assertIn("unknown propositions: P9", errors)
        self.assertIn("astrology", errors)
        bad = tomllib.loads(BOARD_FILE.replace('against = ["P1"]', 'against = ["P3"]'))
        self.assertIn("subset of `covers`", "\n".join(board.validate(bad)))


class Start(Base):
    def test_start_freezes_file_and_records_lanes(self):
        r = board.start(self.store, self.path, self.root)
        self.assertEqual(r["chosen"], ["hist", "demo"])
        run = self.store.db.execute("SELECT kind, corpus FROM runs WHERE run_id = ?", (r["run_id"],)).fetchone()
        self.assertEqual((run["kind"], run["corpus"]), ("board", "board_t-board"))
        self.assertEqual(set(board.lanes(self.store, "t-board")), {"hist", "demo"})
        self.assertEqual(board.lanes(self.store, "t-board", chosen_only=False)["law"]["status"], "dropped")
        with self.assertRaises(ValueError):
            board.start(self.store, self.path, self.root)            # already begun

    def test_changed_file_is_refused(self):
        board.start(self.store, self.path, self.root)
        self.path.write_text(BOARD_FILE + "\n# edited\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after board"):
            board.config(self.store, "t-board", self.root)

    def test_gaps_block_start_unless_accepted(self):
        self.path.write_text(BOARD_FILE.replace('type = "demography"', 'type = "historical"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "coverage gaps"):
            board.start(self.store, self.path, self.root)
        r = board.start(self.store, self.path, self.root, accept_gaps="no demography data yet")
        ev = self.store.db.execute("SELECT detail FROM board_events WHERE event = 'started'").fetchone()["detail"]
        self.assertIn("no demography data yet", ev)
        self.assertTrue(r["gaps"])


class Sources(Base):
    def test_search_logs_and_fills_pools_once(self):
        self.started()
        searches = self.store.db.execute("SELECT lane, member, status, fetched, new_works FROM board_searches "
                                         "ORDER BY search_id").fetchall()
        self.assertEqual([(s["lane"], s["member"], s["status"], s["fetched"], s["new_works"]) for s in searches],
                         [("hist", "code", "ok", 3, 3), ("demo", "code", "ok", 3, 3)])
        with mock.patch.object(board, "run_query", fake_query):      # the same queries are not run again
            self.assertEqual(board.search(self.store, "t-board", SETTINGS, http=object(), root=self.root), [])
        s = board.status(self.store, "t-board", self.root)
        self.assertEqual(s["lanes"]["hist"]["unscreened"], 3)

    def test_failed_search_is_logged(self):
        board.start(self.store, self.path, self.root)

        def broken(*a):
            raise ValueError("bad query")
        with mock.patch.object(board, "run_query", broken):
            out = board.search(self.store, "t-board", SETTINGS, lane="hist", http=object(), root=self.root)
        self.assertEqual(out[0]["status"], "failed")
        self.assertEqual(self.store.db.execute("SELECT status FROM board_searches").fetchone()[0], "failed")

    def test_add_source_logs_the_search_and_includes(self):
        self.started()
        rec = {"title": "Why did the number of young farm entrants decline?", "year": 1993, "doi": "10.2307/1242962"}
        with mock.patch.object(board, "lookup", lambda http, doi, s: rec):
            w = board.add_source(self.store, "t-board", "hist", SETTINGS, "M2", "young farm entrants decline",
                                 doi="https://doi.org/10.2307/1242962", read_from="abstract", label="gale-1993-ajae",
                                 http=object(), root=self.root)
        row = self.store.db.execute("SELECT * FROM board_sources WHERE work_id = ?", (w,)).fetchone()
        self.assertEqual((row["origin"], row["decision"], row["read_from"], row["label"]),
                         ("web", "include", "abstract", "gale-1993-ajae"))
        log = self.store.db.execute("SELECT member, source, query, status FROM board_searches WHERE search_id = ?",
                                    (row["search_id"],)).fetchone()
        self.assertEqual(tuple(log), ("M2", "web", "young farm entrants decline", "logged"))
        with self.assertRaisesRegex(ValueError, "log the search"):
            board.add_source(self.store, "t-board", "hist", SETTINGS, "M2", " ", work_id=w, root=self.root)
        with self.assertRaisesRegex(ValueError, "not a chosen lane"):
            board.add_source(self.store, "t-board", "law", SETTINGS, "M5", "q", work_id=w, root=self.root)

    def test_unknown_doi_needs_a_title(self):
        self.started()
        with mock.patch.object(board, "lookup", lambda http, doi, s: None):
            with self.assertRaisesRegex(ValueError, "give its title"):
                board.add_source(self.store, "t-board", "hist", SETTINGS, "M2", "q", doi="10.9/none",
                                 http=object(), root=self.root)
            w = board.add_source(self.store, "t-board", "hist", SETTINGS, "M2", "Employment Gazette archive",
                                 doi="10.9/none", record={"title": "Job Release Scheme evaluation", "year": 1980},
                                 http=object(), root=self.root)
        self.assertEqual(self.store.db.execute("SELECT doi FROM works WHERE work_id = ?", (w,)).fetchone()[0],
                         "10.9/none")

    def test_screen_overlap_and_todo(self):
        self.started()
        pool = {lane: [r[0] for r in self.store.db.execute(
            "SELECT work_id FROM board_sources WHERE lane = ? ORDER BY rowid", (lane,))] for lane in ("hist", "demo")}
        shared = set(pool["hist"]) & set(pool["demo"])
        self.assertEqual(len(shared), 1)
        for lane in ("hist", "demo"):
            for w in pool[lane]:
                board.screen(self.store, "t-board", lane, w, "include", "on topic", "M1", root=self.root)
        with self.assertRaisesRegex(ValueError, "give a reason"):
            board.screen(self.store, "t-board", "hist", pool["hist"][0], "exclude", "", "M1", root=self.root)
        ov = board.overlap(self.store, "t-board", self.root)
        self.assertEqual(ov[0]["shared"], 1)
        self.assertTrue(ov[0]["flag"])                      # 1 of 3 = 33% > 25%
        board.want(self.store, "t-board", pool["demo"][-1], root=self.root, label="key-study")
        rows = board.todo(self.store, "t-board")
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["file"], "key-study.pdf")  # requested first
        self.assertEqual(len(next(r for r in rows if r["work_id"] in shared)["lanes"]), 2)
        path, n = board.write_todo(self.store, "t-board", self.root)
        self.assertEqual(n, 5)
        self.assertIn("key-study.pdf", path.read_text(encoding="utf-8"))

    def test_add_pdfs_matches_by_label_and_moves_the_file(self):
        self.started()
        w = self.store.db.execute("SELECT work_id FROM board_sources WHERE lane = 'demo' ORDER BY rowid DESC "
                                  "LIMIT 1").fetchone()[0]
        board.screen(self.store, "t-board", "demo", w, "include", "core", "M1", root=self.root)
        board.want(self.store, "t-board", w, label="key-study", root=self.root)
        inbox = self.root / board.INBOX
        inbox.mkdir(parents=True)
        (inbox / "key-study.pdf").write_bytes(b"%PDF-1.4 fake")
        (inbox / "stranger.pdf").write_bytes(b"%PDF-1.4 other")

        def stored(store, work, pdf, folder, grobid, root):
            store.set_fulltext(work, "parsed", source="library", sections=[{"heading": "x", "text": "y" * 3000}])
            return True, "test", None
        with mock.patch.object(board.library, "_store_pdf", stored), \
                mock.patch.object(board.library, "match", lambda name, pdf, cands: (None, "")):
            c = board.add_pdfs(self.store, "t-board", SETTINGS, self.root, log=lambda *_: None)
        self.assertEqual((c["added"], c["unmatched"]), ([w], ["stranger.pdf"]))
        self.assertTrue((inbox / "done" / "key-study.pdf").exists())
        self.assertEqual(board.todo(self.store, "t-board"), [])


if __name__ == "__main__":
    unittest.main()
