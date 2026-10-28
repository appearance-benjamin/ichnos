"""The local app over real HTTP: pages, saving, the safety checks, jobs."""

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from psyscientist import insights
from psyscientist.app import App, AppServer, handler_for
from psyscientist.config import Settings
from tests.test_agents_map import Base


class AppOverHttp(Base):
    def setUp(self):
        super().setUp()
        self.app = App(Settings(None, None, None, self.root / "evidence.sqlite"), self.root, "Tester")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.app))
        self.app.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.app.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def get(self, path, host=None):
        req = urllib.request.Request(self.base + path, headers={"Host": host} if host else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def post(self, path, body, token=True, host=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-Token"] = self.app.token
        if host:
            headers["Host"] = host
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_a_second_app_cannot_take_the_same_port(self):
        first = AppServer(("127.0.0.1", 0), handler_for(self.app))
        try:
            with self.assertRaises(OSError):
                AppServer(("127.0.0.1", first.server_address[1]), handler_for(self.app))
        finally:
            first.server_close()

    def test_page_is_live_and_carries_the_token(self):
        status, html = self.get(f"/run/{self.run}")
        self.assertEqual(status, 200)
        self.assertIn(self.app.token, html)
        self.assertIn('"reviewer": "tester"', html)
        self.assertNotIn("__LIVE__", html)

    def test_saving_a_decision_needs_token_host_and_a_reason(self):
        body = {"run": self.run, "work_id": 1, "reviewer": "Tester", "decision": "exclude", "reason": None}
        self.assertEqual(self.post("/api/decision", body, token=False)[0], 403)
        self.assertEqual(self.post("/api/decision", body, host="evil.example:80")[0], 403)
        status, out = self.post("/api/decision", body)
        self.assertEqual(status, 400)
        self.assertIn("reason", out["error"])
        body["reason"] = "cross_sectional"
        self.assertEqual(self.post("/api/decision", body), (200, {"ok": True}))
        row = self.store.db.execute("SELECT screener, decision, reason FROM screening").fetchone()
        self.assertEqual(tuple(row), ("human:tester", "exclude", "cross_sectional"))
        body["decision"] = ""
        self.assertEqual(self.post("/api/decision", body)[1]["removed"], True)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM screening").fetchone()[0], 0)
        self.assertEqual(self.post("/api/decision", dict(body, work_id=999, decision="include"))[0], 400)

    def test_insights_can_be_added_and_moved_along(self):
        status, out = self.post("/api/insight/add", {"corpus": "Z", "run": self.run, "kind": "gap",
                                                     "statement": "No study of night shifts.", "works": [1, 2],
                                                     "reviewer": "Tester"})
        self.assertEqual(status, 200)
        iid = out["insight"]["id"]
        self.assertEqual(out["insight"]["works"], [1, 2])
        self.assertEqual(self.post("/api/insight/status", {"id": iid, "status": "promoted", "note": "into section 2",
                                                           "reviewer": "Tester"}), (200, {"ok": True}))
        self.assertEqual(insights.rows(self.store, "Z")[0]["status"], "promoted")
        self.assertIn("promoted", (self.root / "notes" / "insights_corpus_Z.md").read_text(encoding="utf-8"))
        self.assertEqual(self.post("/api/insight/add", {"corpus": "Z", "kind": "gap", "statement": "x",
                                                        "works": [12345], "reviewer": "Tester"})[0], 400)

    def test_jobs_run_in_the_background(self):
        self.assertEqual(self.post("/api/job", {"action": "report", "run": self.run})[0], 200)
        for _ in range(100):
            jobs = json.loads(self.get("/api/jobs")[1])
            if jobs[-1]["status"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(jobs[-1]["status"], "done", jobs[-1].get("error"))
        self.assertTrue((self.root / "output" / "corpus_Z" / f"run_{self.run:03d}" / "review.html").exists())
        self.assertEqual(self.post("/api/job", {"action": "import", "run": self.run, "folder": "../../etc"})[0], 400)
        self.assertEqual(self.post("/api/job", {"action": "rm -rf", "run": self.run})[0], 400)


class StartPage(AppOverHttp):
    def test_the_start_page_lists_the_projects_most_recently_worked_on_first(self):
        old = self.run
        self.store.db.execute("UPDATE runs SET started_at = '2026-01-01T00:00:00+00:00' WHERE run_id = ?", (old,))
        self.store.db.commit()
        sha = self.store.add_protocol("Y", "protocol/corpus_y.toml", 'corpus = "Y"\ntitle = "A newer review"\n')
        new = self.store.start_run("pilot", "Y", sha, {})
        self.store.add_sample(new, [1], "pilot_screening")
        status, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertLess(html.index("A newer review"), html.index("Toy corpus"))
        self.assertIn(f'href="/run/{new}"', html)
        self.assertIn("Everything that needs you", html)

    def test_the_start_page_keeps_the_window_app_alive(self):
        status, html = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("/api/ping", html)                     # without it the app stopped under an open start page
        self.assertIn(self.app.token, html)
        self.assertEqual(self.post("/api/ping", {"page": "start-1"})[0], 200)
        self.assertIn("start-1", self.app.pages)

    def test_a_run_page_offers_every_project_in_its_switcher(self):
        status, html = self.get(f"/run/{self.run}")
        self.assertEqual(status, 200)
        live = json.loads(html.split('<script id="live" type="application/json">', 1)[1].split("</script>", 1)[0])
        self.assertEqual([(p["corpus"], p["run"]) for p in live["projects"]], [("Z", self.run)])


class ExportUpload(AppOverHttp):
    def test_an_uploaded_export_is_registered_for_the_corpus(self):
        ris = "TY  - JOUR\nTI  - Sleep and mood in teens\nAB  - A cohort.\nDO  - 10.9/x1\nPY  - 2020\nER  - \n"
        status, body = self.post("/api/export", {"run": self.run, "platform": "scopus", "filename": "my export.ris",
                                                 "content": ris, "hits": "1"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"], "scopus")
        text = (self.root / "protocol" / "corpus_z.toml").read_text(encoding="utf-8")
        self.assertIn('source = "scopus"', text)
        self.assertIn("reported_total = 1", text)
        self.assertFalse(list((self.root / "imports" / "_uploads").iterdir()))   # the upload copy is removed
        status, body = self.post("/api/export", {"run": self.run, "platform": "scopus", "filename": "b.ris",
                                                 "content": ris})
        self.assertEqual(body["source"], "scopus_2")                            # a second export gets its own name
        status, body = self.post("/api/export", {"run": self.run, "platform": "pubmed", "content": ris})
        self.assertEqual(status, 400)
