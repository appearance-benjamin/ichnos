"""Check packs: a pack anyone can open, filled by a person and read back into the review."""

import json

from psyscientist import checkpack, checks, paper
from tests.test_agents_map import Base
from tests.test_checks import HumanChecks


class CheckPack(HumanChecks):
    def test_pack_fill_and_import(self):
        f1, pid = self.write_paper()
        path = checkpack.write(self.store, self.run, {}, self.root / "packs", sample=4, seed=7)
        page = path.read_text(encoding="utf-8")
        self.assertIn('id="pack"', page)
        data = json.loads(page.split('id="pack">', 1)[1].split("</script>", 1)[0].replace(r"<\/", "</"))
        self.assertEqual([f["id"] for f in data["findings"]], [f1])
        self.assertNotIn("strata", data)                      # the page must not reveal the agents' calls
        answer = {"format": checkpack.FORMAT, "pack_id": data["pack_id"], "checker": "Ann Example",
                  "findings": [{"id": f1, "status": "corrected", "against": "full text",
                                "correction": "Mood improved only in girls.", "note": ""}],
                  "core": [], "screening": [{"work_id": s["work_id"], "decision": "include", "note": ""}
                                            for s in data["screening"]]}
        bad = dict(answer, findings=[{"id": f1, "status": "corrected", "against": "full text", "correction": ""}])
        (self.root / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            checkpack.import_file(self.store, self.root / "bad.json")
        (self.root / "checks.json").write_text(json.dumps(answer), encoding="utf-8")
        dry = checkpack.import_file(self.store, self.root / "checks.json", dry_run=True)
        self.assertTrue(dry["dry_run"])
        self.assertEqual(checks.latest(self.store, [f1]), {})
        out = checkpack.import_file(self.store, self.root / "checks.json")
        self.assertEqual(out["counts"]["findings"], 1)
        self.assertEqual(checks.latest(self.store, [f1])[f1]["checker"], "human:Ann Example")
        with self.assertRaises(ValueError):                   # the same file twice
            checkpack.import_file(self.store, self.root / "checks.json")
        text = paper.render(self.store, pid, full=True)
        self.assertIn("Ann Example checked 1 of the 1 findings", text)
        if data["screening"]:
            cal = checkpack.calibration(self.store, self.run)
            self.assertEqual(cal["n"], len(data["screening"]))
            self.assertIn("screened a random sample", text)


class CheckPackApp(HumanChecks):
    def test_app_download_and_import(self):
        import threading
        import urllib.request
        from psyscientist.app import App, AppServer, handler_for
        from psyscientist.config import Settings
        f1, pid = self.write_paper()
        app = App(Settings(None, None, None, self.root / "evidence.sqlite"), self.root, "Tester")
        server = AppServer(("127.0.0.1", 0), handler_for(app))
        app.port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            base = f"http://127.0.0.1:{server.server_address[1]}"
            with urllib.request.urlopen(f"{base}/checkpack/{self.run}?sample=2") as r:
                page = r.read().decode("utf-8")
                self.assertIn("attachment", r.headers.get("Content-Disposition", ""))
            data = json.loads(page.split('id="pack">', 1)[1].split("</script>", 1)[0].replace(r"<\/", "</"))
            answer = {"format": checkpack.FORMAT, "pack_id": data["pack_id"], "checker": "Ann",
                      "findings": [{"id": f1, "status": "confirmed", "against": "passage"}], "core": [], "screening": []}
            out = app.import_checks(app.store(), {"content": json.dumps(answer)})
            self.assertEqual(out["counts"]["findings"], 1)
        finally:
            server.shutdown()
            server.server_close()


class CodingPart(Base):
    """Blind coding: chosen studies coded by a person without seeing any coder's codes, compared on import."""

    def test_a_person_codes_blind_and_is_compared_with_both_coders(self):
        import tomllib
        from psyscientist import patterns
        from psyscientist.store import now
        from tests.test_agents_map import PROTOCOL
        protocol = tomllib.loads(PROTOCOL)
        for w, first, second in ((1, "uk", "uk"), (2, "us", "uk")):
            self.store.db.execute("INSERT INTO study_features (work_id, extractor, dimension, value, canonical, "
                                  "evidence, grounded, run_id, created_at) VALUES (?, 'model:a', 'country', ?, ?, '', "
                                  "1, ?, ?)", (w, first, first, self.run, now()))
            patterns.add_second(self.store, self.run, w, "model:b", "country", second, second, None, None)
        self.store.db.commit()
        corpus = self.store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (self.run,)).fetchone()[0]
        folder = self.root / "output" / f"corpus_{corpus}" / f"run_{self.run:03d}" / "agents" / "extract__model-a"
        folder.mkdir(parents=True)
        (folder / "task.json").write_text(json.dumps({"dims": ["country"]}), encoding="utf-8")
        (folder / "batch_01.json").write_text(json.dumps([{"work_id": 1, "abstract": "Seen abstract.",
                                                           "fulltext": "Methods seen.", "byline": "A. Author (Uni)"}]),
                                              encoding="utf-8")
        path = checkpack.write(self.store, self.run, protocol, self.root / "packs", parts=("coding",), seed=3,
                               coding_works=[1, 2], root=self.root)
        data = json.loads(path.read_text(encoding="utf-8").split('id="pack">', 1)[1].split("</script>", 1)[0]
                          .replace(r"<\/", "</"))
        self.assertEqual([d["name"] for d in data["coding_dims"]], ["country"])     # from the second coding
        by = {c["work_id"]: c for c in data["coding"]}
        self.assertEqual((by[1]["byline"], by[1]["fulltext"], by[1]["seen"]),
                         ("A. Author (Uni)", "Methods seen.", "the text the coders saw"))
        self.assertEqual(by[2]["seen"], "title and abstract from the store")
        self.assertNotIn("model:a", json.dumps(data))                                # no coder's codes are shown
        answer = {"format": checkpack.FORMAT, "pack_id": data["pack_id"], "checker": "Ann Example",
                  "findings": [], "core": [], "screening": [],
                  "coding": [{"work_id": 1, "codes": {"country": ["uk"]}}, {"work_id": 2, "codes": {"country": ["us"]}}]}
        bad = dict(answer, coding=[{"work_id": 1, "codes": {"country": ["fr"]}}])
        (self.root / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
        with self.assertRaises(ValueError):
            checkpack.import_file(self.store, self.root / "bad.json")
        (self.root / "coding.json").write_text(json.dumps(answer), encoding="utf-8")
        out = checkpack.import_file(self.store, self.root / "coding.json")
        self.assertEqual(out["counts"]["coding"], 2)
        agree = out["coding_agreement"]["country"]
        self.assertEqual((agree["primary"]["exact"], agree["primary"]["kappa"]), (2, 1.0))
        self.assertEqual((agree["second"]["n"], agree["second"]["exact"]), (2, 1))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM coding_checks WHERE checker = 'human:Ann Example'"
                                               ).fetchone()[0], 2)


class FindChecksFile(Base):
    def test_the_newest_checks_file_in_downloads_is_found_and_a_missing_one_is_explained(self):
        import os
        import time
        down = self.root / "Downloads"
        down.mkdir()
        with self.assertRaises(FileNotFoundError) as e:
            checkpack.find_checks_file(None, down)
        self.assertIn("Download my checks", str(e.exception))
        old, new = down / "checks_C_a_x.json", down / "checks_C_b_x.json"
        old.write_text("{}", encoding="utf-8")
        new.write_text("{}", encoding="utf-8")
        os.utime(old, (time.time() - 100, time.time() - 100))
        self.assertEqual(checkpack.find_checks_file(None, down), new)
        with self.assertRaises(FileNotFoundError) as e:
            checkpack.find_checks_file(down / "checks_C_typo.json", down)
        self.assertIn("checks_C_b_x.json", str(e.exception))
