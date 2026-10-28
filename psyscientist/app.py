"""The local app: the pipeline in the browser, for people who do not use a terminal.

Start it by double-clicking Psych-Scientist.bat, or with `python -m psyscientist app`.
It serves the review page on http://127.0.0.1:<port> and adds what a static page
cannot do:

- screening decisions, insight statuses, notes and new insights are saved
  straight into the evidence store, under the reviewer's name;
- buttons run the pipeline steps as background jobs: rebuild the report, find
  open-access full texts, prepare agent tasks, import their answers.

Safety: the server listens on 127.0.0.1 only, answers only requests addressed
to that host (so another website cannot reach it by DNS tricks), and every
change needs a per-session token that only pages it served can read.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import time
import tomllib
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import checks, document, insights, library
from .paper import render as render_paper
from .agents import KINDS, import_task, kept_ids, task_state, write_tasks
from .config import ROOT, Settings, load_protocol
from .fulltext import retrieve
from .report import build_report, refresh
from .review import build_data, render_page
from .store import Store

MAX_BODY = 200_000
MAX_EXPORT = 60_000_000          # a library export with abstracts runs to several MB


def clean_name(name: str | None) -> str:
    return re.sub(r"[^a-z0-9_.-]+", "_", (name or "").strip().lower()).strip("_")


class App:
    def __init__(self, settings: Settings, root: Path = ROOT, reviewer: str | None = None):
        self.settings, self.root = settings, root
        self.reviewer = clean_name(reviewer)
        self.token = secrets.token_urlsafe(24)
        self.jobs: list[dict] = []
        self.lock = threading.Lock()
        self.port: int | None = None
        self.pages: dict[str, float] = {}       # open windows, by page id: when each last reported itself
        self.ever_paged = False

    def seen(self, page: str | None, gone: bool = False) -> None:
        if not page:
            return
        with self.lock:
            if gone:
                self.pages.pop(page[:64], None)
            else:
                self.pages[page[:64]] = time.time()
                self.ever_paged = True

    def store(self) -> Store:
        return Store(self.settings.db_path)

    # What the page needs besides the run's data

    def runs(self, store: Store) -> list[dict]:
        out = []
        for r in store.db.execute(
                """SELECT r.run_id, r.corpus, r.started_at, (SELECT count(*) FROM samples s WHERE s.run_id = r.run_id) AS n
                   FROM runs r ORDER BY r.run_id DESC"""):
            out.append({"id": r["run_id"], "corpus": r["corpus"], "n": r["n"],
                        "label": f"Corpus {r['corpus']} · run {r['run_id']} · {r['n']} records · {r['started_at'][:10]}"})
        return out

    def agents_dir(self, store: Store, run_id: int) -> Path:
        run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return self.root / "output" / f"corpus_{run['corpus']}" / f"run_{run_id:03d}" / "agents"

    def tasks(self, store: Store, run_id: int) -> list[dict]:
        folder = self.agents_dir(store, run_id)
        out = []
        for meta_file in sorted(folder.glob("*/task.json")) if folder.exists() else []:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
            out.append({"folder": meta_file.parent.name, "kind": meta["kind"], "agent": meta["agent"],
                        "records": meta.get("records", 0), "state": task_state(meta_file.parent)})
        return out

    def page(self, run_id: int, reviewer: str | None) -> str:
        with_store = self.store()
        try:
            reviewer = clean_name(reviewer) or self.reviewer
            from . import projects
            data = build_data(with_store, run_id, reviewer or None, root=self.root)
            live = {"token": self.token, "reviewer": reviewer, "runs": self.runs(with_store),
                    "projects": [{k: p[k] for k in ("corpus", "name", "run", "runs")}
                                 for p in projects.brief(with_store, self.root)],
                    "tasks": self.tasks(with_store, run_id), "loaded": time.time()}
            return render_page(data, live)
        finally:
            with_store.close()

    # Saving

    def decision(self, store: Store, body: dict) -> dict:
        run_id, work_id = int(body["run"]), int(body["work_id"])
        who = clean_name(body.get("reviewer"))
        if not who:
            raise ValueError("enter your name first")
        run = store.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise ValueError(f"no run {run_id}")
        if not store.db.execute("SELECT 1 FROM samples WHERE run_id = ? AND work_id = ?", (run_id, work_id)).fetchone():
            raise ValueError(f"record {work_id} is not on run {run_id}'s sheet")
        rules = tomllib.loads(store.db.execute("SELECT content FROM protocols WHERE sha256 = ?",
                                               (run["protocol_sha256"],)).fetchone()["content"])["screening"]
        decision, reason = (body.get("decision") or "").strip(), body.get("reason") or None
        screener = f"human:{who}"
        if not decision:
            with store.db:
                store.db.execute("DELETE FROM screening WHERE run_id = ? AND work_id = ? AND screener = ?",
                                 (run_id, work_id, screener))
            return {"ok": True, "removed": True}
        if decision not in rules["decisions"]:
            raise ValueError(f"decision must be one of {rules['decisions']}")
        if decision == "exclude" and reason not in rules["exclude_reasons"]:
            raise ValueError("choose a reason for the exclusion")
        store.add_screening(run_id, work_id, run["corpus"], rules["stage"], screener, decision,
                            reason if decision == "exclude" else None, (body.get("note") or "").strip() or None)
        return {"ok": True}

    def insight_status(self, store: Store, body: dict) -> dict:
        who = clean_name(body.get("reviewer"))
        if not who:
            raise ValueError("enter your name first")
        insights.set_status(store, int(body["id"]), body.get("status", ""), f"human:{who}",
                            (body.get("note") or "").strip() or None)
        self._log(store, int(body["id"]))
        return {"ok": True}

    def direction_status(self, store: Store, body: dict) -> dict:
        """The author chooses a direction to explore next, or sets it aside."""
        from . import directions
        who = clean_name(body.get("reviewer")) or "author"
        directions.set_status(store, int(body["id"]), str(body.get("status") or ""), f"human:{who}",
                              (body.get("note") or "").strip() or None)
        return {"ok": True}

    def discovery_status(self, store: Store, body: dict) -> dict:
        """The author chooses a discovery to explore, sets it aside, or opens it again."""
        from . import discovery
        who = clean_name(body.get("reviewer")) or self.reviewer or "author"
        discovery.set_status(store, int(body["id"]), str(body.get("status") or ""), f"human:{who}",
                             (body.get("note") or "").strip() or None)
        discovery.write_log(store, self.root / "notes" / "discoveries.md")
        return {"ok": True}

    def insight_add(self, store: Store, body: dict) -> dict:
        who = clean_name(body.get("reviewer"))
        statement = (body.get("statement") or "").strip()
        if not who or not statement:
            raise ValueError("enter your name and a statement")
        works = [int(w) for w in body.get("works") or []]
        known = {r[0] for r in store.db.execute(
            f"SELECT work_id FROM works WHERE work_id IN ({','.join('?' * len(works))})", tuple(works))} if works else set()
        if set(works) - known:
            raise ValueError(f"no study numbered {sorted(set(works) - known)}")
        new = insights.add(store, body["corpus"], f"human:{who}", "manual", body.get("kind"), statement,
                           (body.get("rationale") or "").strip() or None, works,
                           run_id=int(body["run"]) if body.get("run") else None)
        self._log(store, new)
        row = store.db.execute("SELECT * FROM insights WHERE insight_id = ?", (new,)).fetchone()
        return {"ok": True, "insight": {"id": new, "kind": row["kind"], "status": row["status"],
                                        "statement": row["statement"], "rationale": row["rationale"],
                                        "author": row["author"], "stage": row["stage"], "run": row["run_id"],
                                        "created": row["created_at"][:10], **json.loads(row["support"])}}

    def open_url(self, store: Store, body: dict) -> dict:
        """Open a study's link in the person's normal browser (where their library login works), not in the
        app window's own profile."""
        url = str(body.get("url") or "")
        if not re.match(r"https://[\w.-]+(/|$)", url):
            raise ValueError("only https links open")
        webbrowser.open(url)
        return {"ok": True}

    def open_inbox(self, store: Store, body: dict) -> dict:
        from .library import INBOX
        folder = (self.root / INBOX).resolve()
        folder.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(folder)              # noqa: S606 (a local folder of this project)
        else:
            webbrowser.open(folder.as_uri())
        return {"ok": True, "folder": str(folder)}

    def add_export(self, store: Store, body: dict) -> dict:
        """Register a library database export (RIS, CSV or Web of Science text) for the run's corpus, as
        `add-export` does; the page then runs the `sources` job, which loads it into the run."""
        from . import imports, query
        run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (int(body.get("run") or 0),)).fetchone()
        if run is None:
            raise ValueError("no such run")
        platform = body.get("platform")
        if platform not in query.LIBRARY_DIALECTS:
            raise ValueError(f"database must be one of {', '.join(query.LIBRARY_DIALECTS)}")
        content = str(body.get("content") or "")
        if not content.strip():
            raise ValueError("the file is empty")
        hits = str(body.get("hits") or "").strip()
        base = {"ebsco": "psycinfo", "ovid": "psycinfo", "scopus": "scopus", "wos": "wos"}[platform]
        _, _, protocol = load_protocol(run["corpus"], self.root)
        taken = set(protocol.get("sources", [])) | {e.get("source") for e in protocol.get("imports", [])}
        source, n = base, 2
        while source in taken:
            source, n = f"{base}_{n}", n + 1
        folder = self.root / "imports" / "_uploads"
        folder.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.\-]+", "_", Path(str(body.get("filename") or "export.ris")).name)[:80] or "export.ris"
        path = folder / name
        path.write_text(content, encoding="utf-8")
        notes: list[str] = []
        try:
            imports.add_export(run["corpus"], [path], source, platform, hits=int(hits) if hits.isdigit() else None,
                               root=self.root, log=notes.append)
        finally:
            path.unlink(missing_ok=True)
        return {"ok": True, "source": source, "log": notes}

    def import_checks(self, store: Store, body: dict) -> dict:
        from . import checkpack
        out = checkpack.import_bytes(store, str(body.get("content") or "").encode("utf-8"),
                                     clean_name(body.get("checker")) or None)
        return {"ok": True, **out}

    def finding_check(self, store: Store, body: dict) -> dict:
        who = clean_name(body.get("reviewer"))
        if not who:
            raise ValueError("enter your name first")
        fid = int(body["finding_id"])
        checks.record(store, fid, f"human:{who}", body.get("status") or "", body.get("against"), body.get("note"),
                      body.get("correction"), int(body["paper"]) if body.get("paper") else None)
        return {"ok": True, "check": checks.latest(store, [fid]).get(fid)}

    def _log(self, store: Store, insight_id: int) -> None:
        corpus = store.db.execute("SELECT corpus FROM insights WHERE insight_id = ?", (insight_id,)).fetchone()["corpus"]
        insights.write_log(store, corpus, self.root / "notes" / f"insights_corpus_{corpus}.md")

    # Jobs: long steps run in a thread; the page polls /api/jobs.

    def start_job(self, label: str, fn) -> dict:
        with self.lock:
            if any(j["status"] == "running" for j in self.jobs):
                raise ValueError("another step is still running; wait for it to finish")
            job = {"id": len(self.jobs) + 1, "label": label, "status": "running", "log": [], "error": None,
                   "started": time.time(), "finished": None}
            self.jobs.append(job)

        def run():
            store = self.store()
            try:
                result = fn(store, lambda *parts: job["log"].append(" ".join(str(p) for p in parts)))
                job["result"] = result if isinstance(result, (dict, list, str, int)) else str(result)
                job["status"] = "done"
            except Exception as err:  # shown on the page; the traceback goes to the app's window
                job["status"], job["error"] = "failed", str(err)
                traceback.print_exc()
            finally:
                job["finished"] = time.time()
                store.close()

        threading.Thread(target=run, daemon=True).start()
        return job

    def job(self, body: dict) -> dict:
        action, run_id = body.get("action"), int(body.get("run") or 0)
        if action == "discover":                # across every project: no run
            def discover(s, log):
                from . import discovery
                counts = discovery.sweep(s, self.root, log=log)
                discovery.write_log(s, self.root / "notes" / "discoveries.md")
                return counts
            return self.start_job("discovery", discover)
        if action in ("discovery_task", "discovery_search", "discovery_import"):
            return self.discovery_job(action, body)
        with_store = self.store()
        try:
            if not with_store.db.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone():
                raise ValueError(f"no run {run_id}")
            agents_dir = self.agents_dir(with_store, run_id)
        finally:
            with_store.close()
        if action == "report":
            return self.start_job("rebuild report", lambda s, log: str(refresh(s, run_id, self.root)))
        if action == "fulltext":
            def fulltext(s, log):
                corpus = s.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
                counts = retrieve(s, kept_ids(s, run_id), self.settings, corpus, run_id, root=self.root, log=log)
                refresh(s, run_id, self.root)
                return counts
            return self.start_job("find full texts", fulltext)
        if action == "pdfs":
            def pdfs(s, log):
                counts = library.add_from_inbox(s, run_id, self.settings, self.root, log=log)
                corpus = s.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
                library.write_todo(s, run_id, checks.current_protocol(s, corpus), self.root, self.settings)
                for cmd in library.next_steps(s, run_id, sorted(set(counts["added"]))):
                    log(f"  next: {cmd}")
                refresh(s, run_id, self.root)
                return {k: (len(v) if isinstance(v, list) else v) for k, v in counts.items()}
            return self.start_job("add downloaded PDFs", pdfs)
        if action == "sources":
            def sources(s, log):
                from .pilot import add_sources
                stats: dict = {}
                new = add_sources(s, run_id, self.settings, self.root, log=log, stats=stats)
                refresh(s, run_id, self.root)
                return {"new records on the sheet": len(new), "abstracts filled": stats.get("abstracts", 0)}
            return self.start_job("run the missing searches", sources)
        if action == "prepare":
            kind, agent = body.get("kind"), (body.get("agent") or "").strip()
            if kind not in KINDS or not re.fullmatch(r"[\w.:\-]{3,60}", agent):
                raise ValueError("choose a task and give the agent a name such as model:claude-opus-5-5")
            return self.start_job(f"prepare {kind} task",
                                  lambda s, log: str(write_tasks(s, run_id, kind, agent, root=self.root)))
        if action == "import":
            name = body.get("folder") or ""
            folder = agents_dir / name
            if not re.fullmatch(r"[\w.\-]+", name) or not (folder / "task.json").exists():
                raise ValueError("unknown task folder")

            def do_import(s, log):
                counts = import_task(folder, s)
                if counts["problems"]:
                    raise ValueError("; ".join(counts["problems"][:5]))
                refresh(s, run_id, self.root)
                return {k: v for k, v in counts.items() if k != "problems"}
            return self.start_job(f"import {name}", do_import)
        raise ValueError(f"unknown step {action!r}")


    def discovery_job(self, action: str, body: dict) -> dict:
        """The chosen discoveries' steps: prepare an agent task, run the novelty searches, import answers."""
        from . import discovery
        if action == "discovery_task":
            kind, agent = body.get("kind"), (body.get("agent") or "").strip()
            if kind not in discovery.TASK_KINDS or not re.fullmatch(r"[\w.:\-]{3,60}", agent):
                raise ValueError("choose a task and give the agent a name such as model:claude-opus-5-5")
            return self.start_job(f"prepare {kind} task",
                                  lambda s, log: str(discovery.write_task(s, kind, agent, self.root)))
        if action == "discovery_search":
            return self.start_job("novelty searches", lambda s, log: discovery.search(s, self.settings, log=log))
        name = body.get("folder") or ""
        folder = self.root / "output" / "discovery" / "agents" / name
        if not re.fullmatch(r"[\w.\-]+", name) or not (folder / "task.json").exists():
            raise ValueError("unknown task folder")

        def do_import(s, log):
            counts = import_task(folder, s)
            if counts["problems"]:
                raise ValueError("; ".join(counts["problems"][:5]))
            discovery.write_log(s, self.root / "notes" / "discoveries.md")
            return {k: v for k, v in counts.items() if k != "problems"}
        return self.start_job(f"import {name}", do_import)


def last_activity(store: Store, run_id: int) -> str:
    """When a run was last worked on: its newest decision, finding or manuscript, or its start. The app opens the
    run worked on most recently, not the one with the most work in it (which kept an older corpus in front)."""
    times = [store.db.execute("SELECT started_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0] or ""]
    for table in ("screening", "findings", "papers"):
        try:
            row = store.db.execute(f"SELECT max(created_at) FROM {table} WHERE run_id = ?", (run_id,)).fetchone()
        except sqlite3.OperationalError:            # a store without that table yet
            continue
        times.append(row[0] or "")
    return max(times)


def handler_for(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PsyScientist"

        def log_message(self, fmt, *args):  # keep the app's window quiet
            pass

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _send(self, status: int, body, content_type: str = "application/json; charset=utf-8",
                  headers: dict | None = None) -> None:
            data = body if isinstance(body, bytes) else (
                body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8"))
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._host_ok():
                return self._send(403, {"error": "wrong host"})
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            try:
                if url.path in ("/", "/projects"):
                    from . import projects
                    store = app.store()
                    try:
                        if not any(r["n"] for r in app.runs(store)):
                            return self._send(200, "<p>No run with a screening sheet yet. Run a search first.</p>",
                                              "text/html; charset=utf-8")
                        text = projects.page(store, app.root, app.token)
                    finally:
                        store.close()
                    return self._send(200, text, "text/html; charset=utf-8")
                if url.path == "/discover":
                    from . import discovery
                    store = app.store()
                    try:
                        text = discovery.page(store, app.root, app.token)
                    finally:
                        store.close()
                    return self._send(200, text, "text/html; charset=utf-8")
                m = re.fullmatch(r"/run/(\d+)", url.path)
                if m:
                    html = app.page(int(m.group(1)), (query.get("reviewer") or [None])[0])
                    return self._send(200, html, "text/html; charset=utf-8")
                m = re.fullmatch(r"/paper/(\d+)", url.path)
                if m:
                    store = app.store()
                    try:
                        row = store.db.execute("SELECT title FROM papers WHERE paper_id = ?", (int(m.group(1)),)).fetchone()
                        if row is None:
                            return self._send(404, {"error": "no such paper"})
                        text = document.to_html(render_paper(store, int(m.group(1)), full=True), row["title"])
                    finally:
                        store.close()
                    return self._send(200, text, "text/html; charset=utf-8")
                m = re.fullmatch(r"/checkpack/(\d+)", url.path)
                if m:
                    from . import checkpack
                    run_id = int(m.group(1))
                    store = app.store()
                    try:
                        run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
                        if run is None:
                            return self._send(404, {"error": "no such run"})
                        _, _, protocol = load_protocol(run["corpus"], app.root)
                        sample = max(0, min(500, int((query.get("sample") or ["60"])[0])))
                        out = app.root / "output" / f"corpus_{run['corpus']}" / f"run_{run_id:03d}" / "check_packs"
                        path = checkpack.write(store, run_id, protocol, out, sample=sample)
                    finally:
                        store.close()
                    return self._send(200, path.read_bytes(), "text/html; charset=utf-8",
                                      headers={"Content-Disposition": f'attachment; filename="{path.name}"'})
                m = re.fullmatch(r"/report/(\d+)", url.path)
                if m:
                    store = app.store()
                    try:
                        text = build_report(store, int(m.group(1)))
                    finally:
                        store.close()
                    return self._send(200, text, "text/plain; charset=utf-8")
                if url.path == "/api/jobs":
                    return self._send(200, [{k: v for k, v in j.items() if k != "result"} | {"log": j["log"][-5:]}
                                            for j in app.jobs])
                if url.path == "/api/hello":
                    from . import __version__
                    return self._send(200, {"app": "psyscientist", "version": __version__})
                if url.path in ("/favicon.ico", "/icon.ico"):
                    from .desktop import ICON
                    return self._send(200, ICON.read_bytes(), "image/x-icon") if ICON.exists() else \
                        self._send(204, b"", "image/x-icon")
                m = re.fullmatch(r"/icon-(\d+)\.png", url.path)
                if m:
                    from .desktop import ASSETS
                    f = ASSETS / f"icon-{int(m.group(1))}.png"
                    return self._send(200, f.read_bytes(), "image/png") if f.exists() else \
                        self._send(404, {"error": "no such icon"})
                if url.path == "/manifest.webmanifest":
                    return self._send(200, {"name": "Psych-Scientist", "short_name": "Psych-Scientist", "start_url": "/",
                                            "display": "standalone", "background_color": "#131315",
                                            "theme_color": "#4361ee",
                                            "icons": [{"src": f"/icon-{n}.png", "sizes": f"{n}x{n}", "type": "image/png"}
                                                      for n in (192, 512)]},
                                      "application/manifest+json")
                return self._send(404, {"error": "not found"})
            except (ValueError, KeyError, TypeError) as err:
                return self._send(400, {"error": str(err)})

        def do_POST(self):
            if not self._host_ok():
                return self._send(403, {"error": "wrong host"})
            url = urllib.parse.urlparse(self.path)
            if url.path == "/api/bye":              # a window closing (sendBeacon: no headers, token in the query)
                q = urllib.parse.parse_qs(url.query)
                if (q.get("token") or [""])[0] == app.token:
                    app.seen((q.get("page") or [""])[0], gone=True)
                return self._send(204, b"")
            if self.headers.get("X-Token") != app.token:
                return self._send(403, {"error": "this page is out of date; reload it"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > (MAX_EXPORT if urllib.parse.urlparse(self.path).path == "/api/export" else MAX_BODY):
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                path = urllib.parse.urlparse(self.path).path
                if path == "/api/job":
                    job = app.job(body)
                    return self._send(200, {"ok": True, "job": job["id"]})
                if path == "/api/ping":
                    app.seen(str(body.get("page") or ""))
                    return self._send(200, {"ok": True})
                actions = {"/api/decision": app.decision, "/api/insight/status": app.insight_status,
                           "/api/direction": app.direction_status, "/api/discovery": app.discovery_status,
                           "/api/insight/add": app.insight_add, "/api/check": app.finding_check,
                           "/api/checks/import": app.import_checks, "/api/open-url": app.open_url,
                           "/api/open-inbox": app.open_inbox, "/api/export": app.add_export}
                if path not in actions:
                    return self._send(404, {"error": "not found"})
                store = app.store()
                try:
                    return self._send(200, actions[path](store, body))
                finally:
                    store.close()
            except (ValueError, KeyError, TypeError) as err:
                return self._send(400, {"error": str(err)})

    return Handler


class AppServer(ThreadingHTTPServer):
    """One app per port. On Windows SO_REUSEADDR lets a second server bind a port that is already in use while
    the first keeps answering, so a restarted app would show stale pages; take the port exclusively instead."""
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(settings: Settings, root: Path = ROOT, port: int = 8766, open_browser: bool = True,
          reviewer: str | None = None) -> None:
    app = App(settings, root, reviewer)
    server = None
    for candidate in range(port, port + 25):
        try:
            server = AppServer(("127.0.0.1", candidate), handler_for(app))
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"No free port between {port} and {port + 24}.")
    app.port = server.server_address[1]
    url = f"http://127.0.0.1:{app.port}/"
    if app.port != port:
        print(f"Port {port} is in use, perhaps by another Psych-Scientist window; close that one if you do not need it.")
    print(f"Psych-Scientist is running at {url}\nIt works only on this computer. Close this window to stop it.")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
