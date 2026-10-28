"""The desktop app: the icon and manifest routes, windows reporting themselves, stopping when the last one closes."""

import json
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

from psyscientist import desktop
from psyscientist.app import App, handler_for
from psyscientist.config import Settings
from tests.test_agents_map import Base


class Desktop(Base):
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

    def post(self, path, body=None, token=True):
        req = urllib.request.Request(self.base + path, data=json.dumps(body or {}).encode(), method="POST",
                                     headers={"Content-Type": "application/json",
                                              **({"X-Token": self.app.token} if token else {})})
        with urllib.request.urlopen(req) as r:
            return r.status

    def test_icon_manifest_and_hello(self):
        with urllib.request.urlopen(self.base + "/api/hello") as r:
            self.assertEqual(json.loads(r.read())["app"], "psyscientist")
        with urllib.request.urlopen(self.base + "/favicon.ico") as r:
            self.assertEqual(r.read()[:4], b"\x00\x00\x01\x00")          # an .ico file
        with urllib.request.urlopen(self.base + "/manifest.webmanifest") as r:
            self.assertEqual(json.loads(r.read())["icons"][0]["src"], "/icon-192.png")
        self.assertTrue(desktop.running(self.app.port))

    def test_pages_report_and_leave(self):
        self.post("/api/ping", {"page": "w1"})
        self.assertIn("w1", self.app.pages)
        self.post("/api/bye?token=wrong&page=w1", token=False)            # someone else's goodbye is ignored
        self.assertIn("w1", self.app.pages)
        self.post(f"/api/bye?token={self.app.token}&page=w1", token=False)
        self.assertNotIn("w1", self.app.pages)


class StopsWithItsWindow(Base):
    def test_run_stops_after_the_last_window_closes(self):
        saved = (desktop.open_window, desktop.GRACE_S, desktop.PORT)
        port = {}

        def fake_window(url, root=None):
            def page():
                time.sleep(0.5)
                base = url.rstrip("/")
                with urllib.request.urlopen(base + "/api/hello") as r:
                    port["ok"] = r.status
                # a page: report, then close (the token is read from the app object below)
                app = fake_window.app_ref()
                app.seen("w1")
                time.sleep(0.3)
                app.seen("w1", gone=True)
            threading.Thread(target=page, daemon=True).start()
            return None

        import psyscientist.app as appmod
        made = []
        real_app = appmod.App

        class Spy(real_app):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                made.append(self)
        fake_window.app_ref = lambda: made[0]
        desktop.open_window, desktop.GRACE_S = fake_window, 0
        appmod.App = Spy
        try:
            t0 = time.time()
            desktop.run(Settings(None, None, None, self.root / "evidence.sqlite"), self.root, "Tester", port=18766)
            self.assertLess(time.time() - t0, 20)
            self.assertEqual(port.get("ok"), 200)
        finally:
            desktop.open_window, desktop.GRACE_S, _ = saved
            appmod.App = real_app
