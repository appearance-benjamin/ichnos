"""Projects: the reviews the app holds, one per corpus, and the app's start page.

A project is a corpus with its runs. Its name, owner and main run come from `protocol/projects.toml`, which only
the app reads (no review protocol does, so it changes no protocol hash). A corpus with runs but no entry there is
still listed, under its protocol's title, and opens the run with the most work in it.

- projects: every project with its main run, earlier runs, last activity and the main run's Home summary;
- brief: the same, without summaries, for the project switcher on each run's page;
- info: one corpus's name, owner and description, for the run page's heading;
- strands: a project can have a comparator strand, a corpus of its own (`strand_of = "C"` in the registry), shown on
  its card from the moment it is registered, before its first run;
- drafted: a registered project without a run yet (protocol drafted, not searched), shown with its approval step;
- page: the start page: what needs you across projects, then a card per project.
"""

from __future__ import annotations

import html
import tomllib
from pathlib import Path

from .config import ROOT
from .store import Store

REGISTRY = Path("protocol") / "projects.toml"


def registry(root: Path = ROOT) -> dict[str, dict]:
    path = root / REGISTRY
    if not path.exists():
        return {}
    entries = tomllib.loads(path.read_text(encoding="utf-8")).get("project") or []
    return {str(e["corpus"]).upper(): e for e in entries if e.get("corpus")}


def _runs(store: Store) -> dict[str, list[dict]]:
    from .app import last_activity
    out: dict[str, list[dict]] = {}
    for r in store.db.execute(
            """SELECT r.run_id, r.corpus, r.started_at,
                      (SELECT count(*) FROM samples s WHERE s.run_id = r.run_id) AS n,
                      (SELECT count(*) FROM screening s WHERE s.run_id = r.run_id) +
                      (SELECT count(*) FROM findings f WHERE f.run_id = r.run_id) AS work
               FROM runs r ORDER BY r.run_id"""):
        out.setdefault(r["corpus"], []).append({"id": r["run_id"], "date": (r["started_at"] or "")[:10],
                                                "records": r["n"], "work": r["work"],
                                                "last": last_activity(store, r["run_id"])})
    return out


def _title(store: Store, corpus: str) -> str:
    from .review import latest_protocol
    return latest_protocol(store, corpus).get("title") or f"Corpus {corpus}"


def info(store: Store, corpus: str, root: Path = ROOT) -> dict:
    e = registry(root).get(corpus.upper(), {})
    return {"name": e.get("name") or _title(store, corpus), "about": e.get("about") or "",
            "owner": e.get("owner") or "", "archived": bool(e.get("archived")),
            "strand_of": str(e.get("strand_of") or "").upper()}


def brief(store: Store, root: Path = ROOT) -> list[dict]:
    """Every project with its main run and all its runs, the most recently worked on first."""
    reg, runs = registry(root), _runs(store)
    out = []
    for corpus, rs in runs.items():
        rs = [r for r in rs if r["records"]]
        if not rs:
            continue
        e = reg.get(corpus.upper(), {})
        ids = {r["id"] for r in rs}
        main = e.get("run") if e.get("run") in ids else max(rs, key=lambda r: (r["work"], r["id"]))["id"]
        out.append({"corpus": corpus, **info(store, corpus, root), "run": main,
                    "runs": [{**r, "main": r["id"] == main} for r in sorted(rs, key=lambda r: -r["id"])],
                    "last": max(r["last"] for r in rs)})
    started = {p["corpus"].upper(): p["run"] for p in out}
    for p in out:
        p["strands"] = [{"corpus": c, "name": e.get("name") or f"Corpus {c}", "run": started.get(c)}
                        for c, e in reg.items() if str(e.get("strand_of") or "").upper() == p["corpus"].upper()]
    return sorted(out, key=lambda p: (p["archived"], -_stamp(p["last"] or ""), p["corpus"]))


def drafted(store: Store, root: Path = ROOT) -> list[dict]:
    """Registered projects without a run yet (their protocol is drafted, the search has not run), strands apart."""
    reg, started = registry(root), {p["corpus"].upper() for p in brief(store, root)}
    return [{"corpus": c, "name": e.get("name") or f"Corpus {c}", "about": e.get("about") or "",
             "owner": e.get("owner") or "", "protocol": f"protocol/corpus_{c.lower()}.toml"}
            for c, e in sorted(reg.items()) if c not in started and not e.get("strand_of") and not e.get("archived")]


def _stamp(iso: str) -> float:
    from datetime import datetime
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def projects(store: Store, root: Path = ROOT) -> list[dict]:
    """brief() plus the Home summary of each project's main run: its stages, next steps and latest manuscript."""
    from .review import latest_protocol, overview
    out = []
    for p in brief(store, root):
        try:
            summary = overview(store, p["run"], latest_protocol(store, p["corpus"]))
        except Exception as err:            # one project failing to summarise must not hide the others
            summary = {"stages": [], "next": [], "paper": None, "decision": None, "error": str(err)}
        out.append({**p, "summary": summary})
    return out


# The start page

def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _when(iso: str) -> str:
    return iso[:10] if iso else "never"


def _stage_line(stages: list[dict]) -> str:
    by = {s["stage"]: s for s in stages}
    bits = []
    for name, label in (("Search", "records"), ("Screen", "screened"), ("Select", "retained"), ("Extract", "findings")):
        s = by.get(name)
        if not s:
            continue
        val = f"{s['value']:,}" + (f" of {s['of']:,}" if s.get("of") is not None and name == "Screen" else "")
        bits.append(f'<span class="kv"><b>{_e(val)}</b> {label}</span>')
    w = by.get("Write")
    if w and w.get("value"):
        bits.append(f'<span class="kv"><b>v{_e(w["value"])}</b> manuscript</span>')
    return "".join(bits)


def _card(p: dict) -> str:
    s = p["summary"]
    d = s.get("decision") or {}
    decision = ""
    if d.get("decision"):
        decision = (f'<span class="decision {_e(d["decision"])}">{_e(d["decision"].replace("_", " "))}</span>'
                    + (f' <span class="muted">{_e(d["overall"])}/10, round {_e(d["round"])}</span>'
                       if d.get("overall") is not None else ""))
    elif s.get("paper"):
        decision = '<span class="muted">manuscript not reviewed yet</span>'
    todo = [n for n in s.get("next") or [] if not n.get("optional")]
    may = [n for n in s.get("next") or [] if n.get("optional")]
    needs = ("".join(f'<li><a href="/run/{p["run"]}#{_e(n["tab"])}">{_e(n["text"])}</a></li>' for n in todo[:3])
             + (f'<li class="muted">and {len(todo) - 3} more on the project\'s Home page</li>' if len(todo) > 3 else ""))
    if may:
        needs += f'<li class="muted">{len(may)} optional checks on the project\'s Home page</li>'
    strands = "".join(
        f'<p class="runs">Comparator strand: <span class="badge-c">{_e(x["corpus"])}</span> '
        + (f'<a href="/run/{x["run"]}">{_e(x["name"])}</a>' if x["run"] else
           f'{_e(x["name"])} <span class="muted">(protocol drafted, not searched yet)</span>') + "</p>"
        for x in p.get("strands") or [])
    others = [r for r in p["runs"] if not r["main"]]
    earlier = ("<p class=\"runs\">Other runs: " + ", ".join(
        f'<a href="/run/{r["id"]}">run {r["id"]}</a> <span class="muted">({_e(r["date"])}, {r["records"]:,} records)</span>'
        for r in others) + "</p>") if others else ""
    return (f'<article class="card" data-corpus="{_e(p["corpus"])}">'
            f'<header><span class="badge-c">{_e(p["corpus"])}</span><h2><a href="/run/{p["run"]}">{_e(p["name"])}</a></h2>'
            + (f'<span class="owner">{_e(p["owner"])}</span>' if p["owner"] else "")
            + (f'<span class="owner">comparator for {_e(p["strand_of"])}</span>' if p.get("strand_of") else "")
            + "</header>"
            + (f'<p class="about">{_e(p["about"])}</p>' if p["about"] else "")
            + f'<div class="stages">{_stage_line(s.get("stages") or [])}</div>'
            + (f'<p class="ms">{decision}</p>' if decision else "")
            + (f'<div class="needs"><h3>Needs you ({len(todo)})</h3><ul>{needs}</ul></div>' if todo
               else f'<p class="muted">Nothing waiting for you.{" " + str(len(may)) + " optional checks." if may else ""}</p>')
            + (f'<p class="warn">This project could not be summarised: {_e(s["error"])}</p>' if s.get("error") else "")
            + f'<footer><a class="btn primary" href="/run/{p["run"]}">Open project</a>'
            f'<span class="muted">run {p["run"]} · last worked on {_e(_when(p["last"]))}</span></footer>'
            + strands + earlier + "</article>")


def _drafted_card(p: dict) -> str:
    return (f'<article class="card" data-corpus="{_e(p["corpus"])}">'
            f'<header><span class="badge-c">{_e(p["corpus"])}</span><h2>{_e(p["name"])}</h2>'
            + (f'<span class="owner">{_e(p["owner"])}</span>' if p["owner"] else "") + "</header>"
            + (f'<p class="about">{_e(p["about"])}</p>' if p["about"] else "")
            + f'<div class="needs"><h3>Needs you (1)</h3><ul><li>Approve the drafted protocol '
              f'(<code>{_e(p["protocol"])}</code>); then the search runs.</li></ul></div>'
            + '<footer><span class="muted">protocol drafted · not searched yet</span></footer></article>')


def page(store: Store, root: Path = ROOT, token: str | None = None) -> str:
    """The start page; with the app's `token` it reports itself to the app like a run's page does, so the window
    app does not stop while only the start page is open."""
    ps = projects(store, root)
    new = drafted(store, root)
    active = [p for p in ps if not p["archived"]]
    archived = [p for p in ps if p["archived"]]
    must = {p["corpus"]: [n for n in p["summary"].get("next") or [] if not n.get("optional")] for p in active}
    total = sum(len(v) for v in must.values()) + len(new)
    groups = "".join(
        f'<h3><span class="badge-c">{_e(p["corpus"])}</span><a href="/run/{p["run"]}">{_e(p["name"])}</a>'
        f' <span class="muted">{len(must[p["corpus"]])}</span></h3><ul class="all">'
        + "".join(f'<li><a href="/run/{p["run"]}#{_e(n["tab"])}">{_e(n["text"])}</a></li>' for n in must[p["corpus"]])
        + "</ul>" for p in active if must[p["corpus"]]) + "".join(
        f'<h3><span class="badge-c">{_e(p["corpus"])}</span>{_e(p["name"])} <span class="muted">1</span></h3>'
        f'<ul class="all"><li>Approve the drafted protocol (<code>{_e(p["protocol"])}</code>); then the search runs.</li></ul>'
        for p in new)
    needs = groups or '<p class="muted">Nothing is waiting for you in any project.</p>'
    from . import discovery
    found = discovery.open_count(store)
    explore = ('<section class="block"><h2>Discovery' + (f' <span class="count">{found}</span>' if found else "")
               + '</h2><p class="muted">Candidate directions found by code across all projects: contradictions, gaps, '
               "outcomes two projects share, insights noticed more than once, rising topics.</p>"
               '<p><a class="btn primary" href="/discover">Open Discovery</a></p></section>')
    body = ('<div class="grid">' + "".join(_card(p) for p in active) + "".join(_drafted_card(p) for p in new) + "</div>"
            + (('<h2 class="sec">Archived</h2><div class="grid">' + "".join(_card(p) for p in archived) + "</div>")
               if archived else "")
            + f'<section class="block todo"><h2>Everything that needs you <span class="count">{total}</span></h2>'
              '<p class="muted">What waits for you in each project: a finished paper to read, directions to choose. '
              'Optional checks are on each project\'s Home page. Each line opens the page where you do it.</p>'
              + needs + "</section>" + explore
            + '<section class="block new"><h2>Start a new project</h2><p class="muted">Tell Claude the question in a '
              'sentence; it drafts <code>protocol/corpus_X.toml</code> for you to approve and runs the search '
              '(<code>python -m psyscientist pilot X</code>). Names, owners and the main run of each project live in '
              '<code>protocol/projects.toml</code>.</p></section>')
    return PAGE.replace("__BODY__", body).replace("</body>", KEEPALIVE.replace("__TOKEN__", token or ""), 1)


KEEPALIVE = """<script>
// The window reports itself, so the desktop app knows a window is still open (and when the last one closes).
(function () {
  const TOKEN = "__TOKEN__";
  if (!TOKEN) return;
  const PAGE_ID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
  const ping = () => fetch("/api/ping", {method: "POST", headers: {"Content-Type": "application/json", "X-Token": TOKEN},
                                         body: JSON.stringify({page: PAGE_ID})}).catch(() => {});
  ping(); setInterval(ping, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) ping(); });
  addEventListener("pagehide", () => navigator.sendBeacon("/api/bye?token=" + encodeURIComponent(TOKEN) + "&page=" + PAGE_ID));
})();
</script>
</body>"""


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Projects · Psych-Scientist</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/favicon.ico">
<style>
:root { --bg: #f6f6f3; --panel: #ffffff; --ink: #1c1c1e; --muted: #6c6c72; --line: #e2e2dd; --chip: #efefea;
  --accent: #3451d1; --inc: #2b8a3e; --uns: #c27c0e; --exc: #c0392b; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #131315; --panel: #1c1c1f; --ink: #ececef; --muted: #9b9ba3; --line: #2f2f34; --chip: #29292e;
    --accent: #8ea2ff; --inc: #63d07a; --uns: #f2b84b; --exc: #ff7b6e; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
a { color: inherit; }
.top { background: var(--panel); border-bottom: 1px solid var(--line); }
.top .in { max-width: 1100px; margin: 0 auto; padding: 14px 16px; display: flex; align-items: center; gap: 10px; }
.top .brand { font-weight: 700; font-size: 17px; }
.top .muted { margin-left: auto; }
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
.block { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; margin-bottom: 16px; }
.block h2, h2.sec { font-size: 17px; margin: 0 0 8px; }
h2.sec { margin: 22px 0 10px; }
.count { display: inline-block; min-width: 24px; padding: 0 7px; border-radius: 12px; background: var(--accent); color: #fff;
  font-size: 13px; text-align: center; vertical-align: 2px; }
.muted { color: var(--muted); font-size: 13px; }
ul.all { list-style: none; margin: 4px 0 10px; padding: 0 0 0 34px; }
.todo h3 { display: flex; align-items: center; gap: 10px; font-size: 15px; margin: 14px 0 2px; }
.todo h3 a { text-decoration: none; }
.todo { margin-top: 18px; }
ul.all li { display: flex; gap: 10px; align-items: baseline; padding: 7px 0; border-top: 1px solid var(--line); }
ul.all li:first-child { border-top: 0; }
ul.all a { text-decoration: none; }
ul.all a:hover { text-decoration: underline; }
.badge-c { flex: none; display: inline-grid; place-items: center; width: 24px; height: 24px; border-radius: 7px;
  background: var(--chip); font-weight: 700; font-size: 13px; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 16px 18px; display: flex;
  flex-direction: column; gap: 8px; }
.card header { display: flex; align-items: center; gap: 10px; }
.card h2 { font-size: 17px; margin: 0; flex: 1; }
.card h2 a { text-decoration: none; }
.card h2 a:hover { text-decoration: underline; }
.owner { font-size: 12px; padding: 2px 8px; border-radius: 10px; background: var(--chip); color: var(--muted); }
.about { margin: 0; color: var(--muted); font-size: 14px; }
.stages { display: flex; flex-wrap: wrap; gap: 6px 14px; font-size: 13px; color: var(--muted); }
.stages b { color: var(--ink); font-size: 15px; }
.ms { margin: 0; font-size: 14px; }
.decision { font-weight: 600; padding: 1px 8px; border-radius: 10px; background: var(--chip); }
.decision.accept, .decision.minor_revision { color: var(--inc); }
.decision.major_revision { color: var(--uns); }
.decision.reject { color: var(--exc); }
.needs h3 { font-size: 13px; margin: 4px 0; text-transform: uppercase; letter-spacing: .04em; color: var(--muted); }
.needs ul { margin: 0; padding-left: 18px; font-size: 14px; }
.needs li { margin: 3px 0; }
.needs a { text-decoration: none; }
.needs a:hover { text-decoration: underline; }
.card footer { margin-top: auto; display: flex; align-items: center; gap: 12px; padding-top: 6px; }
.runs { margin: 0; font-size: 13px; }
.btn { display: inline-block; white-space: nowrap; border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 8px;
  padding: 6px 14px; font-size: 14px; text-decoration: none; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.warn { color: var(--exc); font-size: 13px; }
code { font-size: 13px; background: var(--chip); padding: 1px 5px; border-radius: 5px; }
@media (max-width: 640px) { .grid { grid-template-columns: minmax(0, 1fr); } }
</style>
</head>
<body>
<div class="top"><div class="in"><img src="/icon-32.png" width="26" height="26" alt=""><span class="brand">Psych-Scientist</span>
<span class="muted">Your projects</span></div></div>
<main>
__BODY__
</main>
</body>
</html>
"""
