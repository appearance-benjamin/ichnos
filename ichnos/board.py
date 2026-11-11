"""The board, step 1: choosing a direction's lanes and sources (docs/design-board-selection-and-argument-log.md).

A board reviews one or more candidate directions for a paper. Each direction rests on load-bearing propositions. Each
member of the board reads one *lane*: an evidence partition of a given type (historical, computational, institutional,
...). The lanes are declared in a pre-registered file, protocol/board_<id>.toml, and chosen by code from a coverage
matrix, not by judgement:

1. every load-bearing proposition is covered by lanes of at least `min_types` different types, including any type it
   `requires` (the author's judgement that a claim needs, say, demographic evidence, stated in the frozen file);
2. every load-bearing proposition has a lane whose search is framed against it (`against`);
3. after selection, no two lanes share more than `max_overlap` of the smaller lane's sources.

`check` shows the matrix, the lanes code proposes, the lanes it leaves out (and why) and any rule that cannot be met.
`start` freezes the file (sha256) and opens a board run; from then on the file must not change. Sources are records in
`works`, so the full-text pipeline applies to them: code runs each lane's search strings (`search`, logged); members
add what they find on the web only through a logged search (`add_source`), and screen their lane's pool (`screen`).
`todo` lists the included sources still without a full text; PDFs saved in fulltext/inbox/board/ are matched and
parsed by `add_pdfs`.

Rounds, rulings and the consensus map (steps 4 to 7 of the design) build on these tables and come later.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tomllib
from itertools import combinations
from pathlib import Path

from . import library, tables
from .config import ROOT, Settings
from .fulltext import grobid_url
from .http import ApiError, Http
from .sources import europepmc, openalex, pubmed, semantic_scholar
from .store import Store, normalize_doi, now

TYPES = {
    "store": "an Ichnos corpus, or a filter on one",
    "historical": "episodes before a cut-off year",
    "computational": "the model and its runs",
    "contemporary_for": "recent empirical studies, searched for the claim",
    "contemporary_against": "recent empirical studies, searched for contrary or null results",
    "institutional": "law, collective agreements, regulators, firms' own documents",
    "neighbouring": "a discipline the author does not work in",
    "demography": "population, flows and accounting identities",
}
DEFAULT_RULES = {"min_types": 2, "max_overlap": 0.25}
DECISIONS = ("include", "exclude")
READ_FROM = ("full text", "abstract", "summary", "title")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
INBOX = Path("fulltext") / "inbox" / "board"

SCHEMA = """
CREATE TABLE IF NOT EXISTS board_rounds (
    board       TEXT PRIMARY KEY,
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    config_path TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    config      TEXT NOT NULL,                   -- the pre-registered file as it was when the board began
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS board_lanes (
    board       TEXT NOT NULL,
    lane        TEXT NOT NULL,
    type        TEXT NOT NULL,
    title       TEXT,
    evidence    TEXT,                            -- what a member of this lane may read
    covers      TEXT NOT NULL,                   -- JSON list of proposition ids
    against     TEXT NOT NULL,                   -- JSON list: propositions its search is framed against
    queries     TEXT NOT NULL,                   -- JSON list of {source, query, cap}
    status      TEXT NOT NULL,                   -- chosen, dropped
    reason      TEXT,                            -- why it was chosen or dropped
    PRIMARY KEY (board, lane)
);
CREATE TABLE IF NOT EXISTS board_searches (
    search_id   INTEGER PRIMARY KEY,
    board       TEXT NOT NULL,
    lane        TEXT NOT NULL,
    member      TEXT NOT NULL,                   -- code, or the member who searched (e.g. M2, human:reviewer)
    source      TEXT NOT NULL,                   -- openalex, europepmc, ..., or web for a member's own search
    query       TEXT NOT NULL,                   -- exactly as sent, or as the member typed it
    reported_total INTEGER,
    fetched     INTEGER,
    new_works   INTEGER,                         -- records this search added to the lane
    status      TEXT NOT NULL,                   -- ok, failed, logged (a member's search, reported by them)
    error       TEXT,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS board_sources (
    board       TEXT NOT NULL,
    lane        TEXT NOT NULL,
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    origin      TEXT NOT NULL,                   -- search (code), web (a member's logged search), manual
    search_id   INTEGER REFERENCES board_searches(search_id),
    decision    TEXT,                            -- NULL until screened; include, exclude
    reason      TEXT,
    screener    TEXT,
    read_from   TEXT,                            -- full text, abstract, summary, title: what the member read
    label       TEXT,                            -- a file name stem the author may save its PDF under
    wanted      INTEGER NOT NULL DEFAULT 0,      -- 1: a point rests on it and its full text is requested
    note        TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (board, lane, work_id)
);
CREATE TABLE IF NOT EXISTS board_events (
    event_id    INTEGER PRIMARY KEY,
    board       TEXT NOT NULL,
    event       TEXT NOT NULL,                   -- started, searched, source_added, screened, wanted, pdfs_added
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


# The pre-registered file

def load(path: Path) -> tuple[str, dict, str]:
    text = path.read_text(encoding="utf-8")
    return text, tomllib.loads(text), hashlib.sha256(text.encode("utf-8")).hexdigest()


def rules(cfg: dict) -> dict:
    return DEFAULT_RULES | (cfg.get("rules") or {})


def validate(cfg: dict) -> list[str]:
    """Problems that make the file unusable (as opposed to coverage gaps, which `plan` reports)."""
    errors = []
    if not ID.match(str(cfg.get("board", ""))):
        errors.append("`board` must be a short id of lower-case letters, digits and hyphens, e.g. entry-door-1")
    dirs = [d.get("id") for d in cfg.get("directions") or []]
    props = [p.get("id") for p in cfg.get("propositions") or []]
    lanes = [ln.get("id") for ln in cfg.get("lanes") or []]
    for what, ids in (("direction", dirs), ("proposition", props), ("lane", lanes)):
        if not ids:
            errors.append(f"no {what}s declared")
        if any(not i for i in ids):
            errors.append(f"a {what} has no id")
        dup = sorted({i for i in ids if i and ids.count(i) > 1})
        if dup:
            errors.append(f"{what} ids used twice: {', '.join(dup)}")
    for p in cfg.get("propositions") or []:
        bad = [d for d in p.get("directions") or [] if d not in dirs]
        if not p.get("directions"):
            errors.append(f"proposition {p.get('id')} names no direction")
        if bad:
            errors.append(f"proposition {p.get('id')} names unknown directions: {', '.join(bad)}")
        if not (p.get("text") or "").strip():
            errors.append(f"proposition {p.get('id')} has no text")
        bad = [t for t in p.get("requires") or [] if t not in TYPES]
        if bad:
            errors.append(f"proposition {p.get('id')} requires unknown lane types: {', '.join(bad)}")
    for ln in cfg.get("lanes") or []:
        lid = ln.get("id")
        if ln.get("type") not in TYPES:
            errors.append(f"lane {lid}: type {ln.get('type')!r} is not one of {', '.join(TYPES)}")
        bad = [p for p in (ln.get("covers") or []) + (ln.get("against") or []) if p not in props]
        if bad:
            errors.append(f"lane {lid} names unknown propositions: {', '.join(sorted(set(bad)))}")
        if not ln.get("covers"):
            errors.append(f"lane {lid} covers no proposition")
        loose = [p for p in ln.get("against") or [] if p not in (ln.get("covers") or [])]
        if loose:
            errors.append(f"lane {lid}: `against` must be a subset of `covers` ({', '.join(loose)})")
        if ln.get("keep") and ln.get("drop"):
            errors.append(f"lane {lid} is marked both keep and drop")
        for q in ln.get("queries") or []:
            if q.get("source") not in SOURCES:
                errors.append(f"lane {lid}: query source {q.get('source')!r} is not one of {', '.join(SOURCES)}")
            if not (q.get("query") or "").strip():
                errors.append(f"lane {lid}: a query is empty")
    return errors


def load_bearing(cfg: dict) -> list[dict]:
    return [p for p in cfg["propositions"] if p.get("load_bearing", True)]


def _needs(cfg: dict, lanes: list[dict]) -> dict[str, dict]:
    """What each load-bearing proposition still lacks under the given lanes: lane types, and a lane against it."""
    need_types = rules(cfg)["min_types"]
    out = {}
    for p in load_bearing(cfg):
        types = {ln["type"] for ln in lanes if p["id"] in ln.get("covers", [])}
        against = any(p["id"] in ln.get("against", []) for ln in lanes)
        required = sorted(set(p.get("requires") or []) - types)
        out[p["id"]] = {"types": max(0, need_types - len(types)), "against": 0 if against else 1,
                        "requires": required, "have": sorted(types)}
    return out


def _unmet(needs: dict) -> int:
    return sum(n["types"] + n["against"] + len(n["requires"]) for n in needs.values())


def plan(cfg: dict) -> dict:
    """The coverage matrix, the lanes code chooses (forced ones first, then greedily the lane that meets most of
    what is still missing, then any chosen lane that turns out unnecessary is pruned), the dropped lanes with their
    reason, and the gaps no declared lane can fill."""
    lanes = cfg["lanes"]
    by_id = {ln["id"]: ln for ln in lanes}
    offered = [ln for ln in lanes if not ln.get("drop")]
    chosen = [ln for ln in offered if ln.get("keep")]
    while True:
        base = _unmet(_needs(cfg, chosen))
        if base == 0:
            break
        gains = [(base - _unmet(_needs(cfg, chosen + [ln])), -i, ln) for i, ln in enumerate(offered) if ln not in chosen]
        gains = [g for g in gains if g[0] > 0]
        if not gains:
            break
        chosen.append(max(gains, key=lambda g: (g[0], g[1]))[2])
    final_unmet = _unmet(_needs(cfg, chosen))
    for ln in reversed(list(chosen)):              # prune lanes the others made unnecessary
        if not ln.get("keep") and _unmet(_needs(cfg, [c for c in chosen if c is not ln])) == final_unmet:
            chosen.remove(ln)
    ids = {ln["id"] for ln in chosen}
    needs = _needs(cfg, chosen)
    reasons = {}
    for ln in lanes:
        if ln["id"] in ids:
            reasons[ln["id"]] = "kept by the author" if ln.get("keep") else "needed for coverage"
        elif ln.get("drop"):
            reasons[ln["id"]] = f"dropped by the author: {ln['drop']}"
        else:
            reasons[ln["id"]] = "not needed: the chosen lanes already cover its propositions"
    matrix = []
    for p in cfg["propositions"]:
        cells = {ln["id"]: ("against" if p["id"] in ln.get("against", []) else
                            "covers" if p["id"] in ln.get("covers", []) else "") for ln in lanes}
        matrix.append({"id": p["id"], "text": p["text"], "load_bearing": p.get("load_bearing", True),
                       "directions": p["directions"], "cells": cells})
    gaps = []
    for pid, n in needs.items():
        if n["types"]:
            missing = [t for t in TYPES if t not in n["have"]]
            gaps.append({"proposition": pid, "rule": "types",
                         "text": f"{pid} is covered by {len(n['have'])} lane type(s) ({', '.join(n['have']) or 'none'}); "
                                 f"it needs {rules(cfg)['min_types']}. Add a lane of another type: "
                                 f"{', '.join(missing)}"})
        if n["requires"]:
            gaps.append({"proposition": pid, "rule": "requires",
                         "text": f"{pid} requires a lane of type {', '.join(n['requires'])}, and none covers it"})
        if n["against"]:
            gaps.append({"proposition": pid, "rule": "against",
                         "text": f"{pid} has no lane whose search is framed against it"})
    return {"board": cfg["board"], "rules": rules(cfg), "matrix": matrix, "lanes": [ln["id"] for ln in lanes],
            "chosen": [ln["id"] for ln in lanes if ln["id"] in ids], "reasons": reasons, "gaps": gaps,
            "types": {ln["id"]: ln["type"] for ln in lanes}, "by_id": by_id}


def plan_text(cfg: dict, p: dict) -> str:
    lines = [f"Board {p['board']}: {cfg.get('title', '')}".rstrip(": "), ""]
    lines.append("Directions:")
    for d in cfg["directions"]:
        lines.append(f"  {d['id']}  {d.get('title', '')}")
    lines += ["", "Coverage (C covers, A searched against it; * = not load-bearing):"]
    width = max(len(i) for i in p["lanes"])
    lines.append("        " + " ".join(i.rjust(width) for i in p["lanes"]))
    for row in p["matrix"]:
        mark = " " if row["load_bearing"] else "*"
        cells = " ".join({"against": "A", "covers": "C", "": "."}[row["cells"][i]].rjust(width) for i in p["lanes"])
        lines.append(f"  {row['id']:<5}{mark} {cells}   {row['text'][:70]}")
    lines += ["", f"Rules: every load-bearing proposition covered by >= {p['rules']['min_types']} lane types (and any it "
                  "requires), and by "
                  f"one lane searched against it; lanes may share <= {p['rules']['max_overlap']:.0%} of their sources.",
              "", "Lanes:"]
    for lid in p["lanes"]:
        flag = "CHOSEN " if lid in p["chosen"] else "dropped"
        lines.append(f"  {flag} {lid:<{width}} {p['types'][lid]:<21} {p['reasons'][lid]}")
    lines += ["", "Gaps:" if p["gaps"] else "Gaps: none. Every rule is met by the chosen lanes."]
    lines += [f"  - {g['text']}" for g in p["gaps"]]
    return "\n".join(lines)


# The board

def board_row(store: Store, board: str):
    ensure(store)
    row = store.db.execute("SELECT * FROM board_rounds WHERE board = ?", (board,)).fetchone()
    if row is None:
        raise ValueError(f"board {board} has not begun; check its file, then start it with `board start <file>`")
    return row


def config(store: Store, board: str, root: Path = ROOT) -> dict:
    """The board's frozen file. The file on disk must still be the one that was frozen."""
    row = board_row(store, board)
    path = root / row["config_path"]
    if path.exists():
        text = path.read_text(encoding="utf-8")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != row["config_sha256"]:
            raise ValueError(f"{row['config_path']} changed after board {board} began; restore it, or give it a new "
                             "`board` id to start a new board")
    return tomllib.loads(row["config"])


def start(store: Store, path: Path, root: Path = ROOT, accept_gaps: str | None = None) -> dict:
    """Freeze the file and open the board's run. Coverage gaps block the start unless the author accepts them with a
    reason, which is recorded."""
    ensure(store)
    text, cfg, sha = load(path)
    errors = validate(cfg)
    if errors:
        raise ValueError("the board file has problems:\n  " + "\n  ".join(errors))
    if store.db.execute("SELECT 1 FROM board_rounds WHERE board = ?", (cfg["board"],)).fetchone():
        raise ValueError(f"board {cfg['board']} has already begun")
    p = plan(cfg)
    if p["gaps"] and not accept_gaps:
        raise ValueError("coverage gaps remain (see `board check`); add lanes, or start with --accept-gaps \"<why>\"")
    try:
        rel = path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise ValueError(f"the board file must be inside the project ({root})") from None
    corpus = f"board_{cfg['board']}"
    proto = store.add_protocol(corpus, rel, text)
    run_id = store.start_run("board", corpus, proto, {"board": cfg["board"], "config_sha256": sha})
    at = now()
    with store.db:
        store.db.execute("INSERT INTO board_rounds VALUES (?, ?, ?, ?, ?, ?)", (cfg["board"], run_id, rel, sha, text, at))
        for ln in cfg["lanes"]:
            store.db.execute("INSERT INTO board_lanes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
                cfg["board"], ln["id"], ln["type"], ln.get("title"), ln.get("evidence"),
                json.dumps(ln.get("covers") or []), json.dumps(ln.get("against") or []),
                json.dumps(ln.get("queries") or [], ensure_ascii=False),
                "chosen" if ln["id"] in p["chosen"] else "dropped", p["reasons"][ln["id"]]))
        _event(store, cfg["board"], "started", {"config": rel, "config_sha256": sha, "run_id": run_id,
                                                "chosen": p["chosen"], "gaps": [g["text"] for g in p["gaps"]],
                                                "accepted_gaps": accept_gaps})
    return {"board": cfg["board"], "run_id": run_id, "chosen": p["chosen"], "gaps": p["gaps"]}


def _event(store: Store, board: str, event: str, detail: dict) -> None:
    store.db.execute("INSERT INTO board_events (board, event, detail, created_at) VALUES (?, ?, ?, ?)",
                     (board, event, json.dumps(detail, ensure_ascii=False), now()))


def lanes(store: Store, board: str, chosen_only: bool = True) -> dict[str, dict]:
    ensure(store)
    rows = store.db.execute("SELECT * FROM board_lanes WHERE board = ? ORDER BY rowid", (board,)).fetchall()
    return {r["lane"]: dict(r, covers=json.loads(r["covers"]), against=json.loads(r["against"]),
                            queries=json.loads(r["queries"]))
            for r in rows if not chosen_only or r["status"] == "chosen"}


def _lane(store: Store, board: str, lane: str) -> dict:
    found = lanes(store, board)
    if lane not in found:
        raise ValueError(f"lane {lane} is not a chosen lane of board {board} (chosen: {', '.join(found) or 'none'})")
    return found[lane]


# Sources

SOURCES = {"openalex": openalex, "europepmc": europepmc, "pubmed": pubmed, "semantic_scholar": semantic_scholar}


def run_query(http: Http, source: str, query: str, cap: int, settings: Settings):
    """One lane query through the named source, with that source's keys."""
    mod = SOURCES[source]
    if source == "openalex":
        return mod.search(http, query, cap, settings.openalex_api_key, settings.contact_email)
    if source == "europepmc":
        return mod.search(http, query, cap, settings.contact_email)
    if source == "pubmed":
        return mod.search(http, query, cap, settings.ncbi_api_key)
    return mod.search(http, query, cap, settings.s2_api_key)


def _add(store: Store, board: str, lane: str, work_id: int, origin: str, search_id: int | None, **fields) -> bool:
    """Put a record in a lane's pool; True if it is new to the lane. Fields given for a record already in the lane
    (a decision, what was read, a label) update it."""
    at = now()
    row = store.db.execute("SELECT 1 FROM board_sources WHERE board = ? AND lane = ? AND work_id = ?",
                           (board, lane, work_id)).fetchone()
    if row is None:
        store.db.execute("INSERT INTO board_sources (board, lane, work_id, origin, search_id, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (board, lane, work_id, origin, search_id, at, at))
    fields = {k: v for k, v in fields.items() if v is not None}
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        store.db.execute(f"UPDATE board_sources SET {sets}, updated_at = ? WHERE board = ? AND lane = ? AND work_id = ?",
                         (*fields.values(), at, board, lane, work_id))
    return row is None


def search(store: Store, board: str, settings: Settings, lane: str | None = None, http: Http | None = None,
           root: Path = ROOT) -> list[dict]:
    """Run the chosen lanes' search strings (all, or one lane's) and log every search. A query that has already run
    for the lane is not run again."""
    config(store, board, root)
    run_id = board_row(store, board)["run_id"]
    http = http or Http(store, run_id)
    out = []
    for lid, ln in lanes(store, board).items():
        if lane and lid != lane:
            continue
        for q in ln["queries"]:
            done = store.db.execute("SELECT 1 FROM board_searches WHERE board = ? AND lane = ? AND member = 'code' AND "
                                    "source = ? AND query = ? AND status = 'ok'",
                                    (board, lid, q["source"], q["query"])).fetchone()
            if done:
                continue
            cap = int(q.get("cap", 50))
            try:
                result = run_query(http, q["source"], q["query"], cap, settings)
            except (ApiError, ValueError) as err:
                with store.db:
                    store.db.execute("INSERT INTO board_searches (board, lane, member, source, query, status, error, "
                                     "created_at) VALUES (?, ?, 'code', ?, ?, 'failed', ?, ?)",
                                     (board, lid, q["source"], q["query"], str(err), now()))
                out.append({"lane": lid, "source": q["source"], "query": q["query"], "status": "failed",
                            "error": str(err)})
                continue
            with store.db:
                cur = store.db.execute("INSERT INTO board_searches (board, lane, member, source, query, reported_total, "
                                       "fetched, status, created_at) VALUES (?, ?, 'code', ?, ?, ?, ?, 'ok', ?)",
                                       (board, lid, q["source"], result.query, result.reported_total,
                                        len(result.records), now()))
                sid = cur.lastrowid
            new = 0
            for rank, rec in enumerate(result.records, 1):
                w = store.upsert_work(rec)
                store.add_retrieval(run_id, w, q["source"], rec.get("source_id"), rank)
                with store.db:
                    new += _add(store, board, lid, w, "search", sid)
            with store.db:
                store.db.execute("UPDATE board_searches SET new_works = ? WHERE search_id = ?", (new, sid))
                _event(store, board, "searched", {"lane": lid, "source": q["source"], "search_id": sid,
                                                  "fetched": len(result.records), "new": new})
            out.append({"lane": lid, "source": q["source"], "query": result.query, "status": "ok",
                        "reported_total": result.reported_total, "fetched": len(result.records), "new": new})
    return out


def lookup(http: Http, doi: str, settings: Settings) -> dict | None:
    """A DOI's record from OpenAlex, or None if OpenAlex does not know it."""
    params = {"select": openalex.SELECT,
              **({"api_key": settings.openalex_api_key} if settings.openalex_api_key else {}),
              **({"mailto": settings.contact_email} if settings.contact_email else {})}
    try:
        data = http.get_json("openalex_board", f"{openalex.URL}/doi:{doi}", params)
    except ApiError as err:
        if err.status == 404:
            return None
        raise
    return openalex.parse_work(data)


def add_source(store: Store, board: str, lane: str, settings: Settings, member: str, query: str,
               doi: str | None = None, work_id: int | None = None, record: dict | None = None,
               read_from: str | None = None, label: str | None = None, note: str | None = None,
               http: Http | None = None, root: Path = ROOT) -> int:
    """A source a member found outside the lane's own searches. The search that found it is logged first (what the
    member typed, where), so nothing is cited that is not in the log. The source is included in the lane.
    Give a DOI (looked up in OpenAlex), a record already in the store, or a record by hand (title, year, authors)."""
    config(store, board, root)
    _lane(store, board, lane)
    if read_from and read_from not in READ_FROM:
        raise ValueError(f"read_from must be one of {', '.join(READ_FROM)}")
    if not (query or "").strip():
        raise ValueError("log the search that found it: give the query (or where it was found)")
    run_id = board_row(store, board)["run_id"]
    if work_id is not None:
        if not store.db.execute("SELECT 1 FROM works WHERE work_id = ?", (work_id,)).fetchone():
            raise ValueError(f"no record #{work_id}")
        w = work_id
    elif doi:
        doi = normalize_doi(doi)
        rec = lookup(http or Http(store, run_id), doi, settings) or {"doi": doi, **(record or {})}
        if not rec.get("title") and not (record or {}).get("title"):
            raise ValueError(f"OpenAlex does not know {doi}; give its title (and year, authors) as well")
        w = store.upsert_work({**(record or {}), **{k: v for k, v in rec.items() if v}})
    elif record and record.get("title"):
        w = store.upsert_work(record)
    else:
        raise ValueError("give a DOI, a record number, or a title")
    with store.db:
        cur = store.db.execute("INSERT INTO board_searches (board, lane, member, source, query, fetched, new_works, "
                               "status, note, created_at) VALUES (?, ?, ?, 'web', ?, 1, ?, 'logged', ?, ?)",
                               (board, lane, member, query, 0, note, now()))
        new = _add(store, board, lane, w, "web", cur.lastrowid, decision="include", screener=member,
                   reason="added by the member", read_from=read_from, label=label, note=note)
        store.db.execute("UPDATE board_searches SET new_works = ? WHERE search_id = ?", (int(new), cur.lastrowid))
        _event(store, board, "source_added", {"lane": lane, "work_id": w, "member": member, "new": new})
    return w


def screen(store: Store, board: str, lane: str, work_id: int, decision: str, reason: str, screener: str,
           read_from: str | None = None, root: Path = ROOT) -> None:
    """A lane member's include or exclude, with a reason, for a record in its lane's pool."""
    config(store, board, root)
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    if not (reason or "").strip():
        raise ValueError("give a reason")
    if read_from and read_from not in READ_FROM:
        raise ValueError(f"read_from must be one of {', '.join(READ_FROM)}")
    if not store.db.execute("SELECT 1 FROM board_sources WHERE board = ? AND lane = ? AND work_id = ?",
                            (board, lane, work_id)).fetchone():
        raise ValueError(f"#{work_id} is not in lane {lane}'s pool")
    with store.db:
        _add(store, board, lane, work_id, "search", None, decision=decision, reason=reason, screener=screener,
             read_from=read_from)
        _event(store, board, "screened", {"lane": lane, "work_id": work_id, "decision": decision, "screener": screener})


def want(store: Store, board: str, work_id: int, note: str | None = None, label: str | None = None,
         root: Path = ROOT) -> None:
    """Request a source's full text: a point rests on it and it was read only in part."""
    config(store, board, root)
    rows = store.db.execute("SELECT lane FROM board_sources WHERE board = ? AND work_id = ? AND decision = 'include'",
                            (board, work_id)).fetchall()
    if not rows:
        raise ValueError(f"#{work_id} is not an included source of board {board}")
    with store.db:
        store.db.execute("UPDATE board_sources SET wanted = 1, updated_at = ?" + (", label = ?" if label else "") +
                         (", note = ?" if note else "") + " WHERE board = ? AND work_id = ? AND decision = 'include'",
                         (now(), *([label] if label else []), *([note] if note else []), board, work_id))
        _event(store, board, "wanted", {"work_id": work_id, "note": note})


# Full texts

def _has_text(store: Store, w: int) -> bool:
    ft = store.get_fulltext(w)
    return bool(ft and ft["status"] == "parsed" and (ft["chars"] or 0) >= tables.FULL_MIN)


def included(store: Store, board: str) -> dict[int, dict]:
    """Every included source, with the lanes that include it, whether its full text is requested, its label, and
    what its readers read."""
    out: dict[int, dict] = {}
    for r in store.db.execute("SELECT * FROM board_sources WHERE board = ? AND decision = 'include' ORDER BY rowid",
                              (board,)):
        s = out.setdefault(r["work_id"], {"lanes": [], "wanted": False, "label": None, "read_from": set(), "notes": []})
        s["lanes"].append(r["lane"])
        s["wanted"] |= bool(r["wanted"])
        s["label"] = s["label"] or r["label"]
        if r["read_from"]:
            s["read_from"].add(r["read_from"])
        if r["note"]:
            s["notes"].append(r["note"])
    return out


def todo(store: Store, board: str) -> list[dict]:
    """Included sources still without a full text: requested ones first, then those more lanes rest on."""
    inc = included(store, board)
    meta = tables.works(store, inc)
    rows = []
    for w, s in inc.items():
        if _has_text(store, w):
            continue
        m = meta[w]
        rows.append({"work_id": w, "cite": m["cite"], "title": m["title"] or "", "year": m["year"],
                     "venue": m["venue"] or "", "doi": m["doi"], "lanes": s["lanes"], "wanted": s["wanted"],
                     "file": f"{s['label'] or w}.pdf", "read_from": sorted(s["read_from"]), "notes": s["notes"]})
    return sorted(rows, key=lambda r: (not r["wanted"], -len(r["lanes"]), r["cite"]))


def todo_markdown(board: str, rows: list[dict], inbox: Path) -> str:
    lines = [f"# Board {board}: full texts to get", "",
             f"{len(rows)} included sources have no full text yet. Save each PDF in `{inbox}` under the file name "
             "given (or any name: a PDF is also matched by the DOI or title on its first pages), then run "
             f"`python -m ichnos board add-pdfs {board}`.", "",
             "| File name | Source | Lanes | Requested | Read so far | DOI |", "| --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        title = r["title"].replace("|", "/")[:90]
        lines.append(f"| `{r['file']}` | {r['cite']}: {title} ({r['venue']}) | {', '.join(r['lanes'])} | "
                     f"{'yes' if r['wanted'] else ''} | {', '.join(r['read_from']) or ''} | {r['doi'] or ''} |")
    return "\n".join(lines) + "\n"


def write_todo(store: Store, board: str, root: Path = ROOT) -> tuple[Path, int]:
    rows = todo(store, board)
    out = root / "output" / f"board_{board}"
    out.mkdir(parents=True, exist_ok=True)
    (root / INBOX).mkdir(parents=True, exist_ok=True)
    path = out / "fulltext_todo.md"
    path.write_text(todo_markdown(board, rows, (root / INBOX).resolve()), encoding="utf-8")
    return path, len(rows)


def add_pdfs(store: Store, board: str, settings: Settings, root: Path = ROOT, log=print) -> dict:
    """Match the PDFs in fulltext/inbox/board/ to the board's included sources still without a full text (by the
    label or record number in the file name, else the DOI or title on the first pages), store and parse them, and
    move each matched file to inbox/board/done."""
    config(store, board, root)
    inbox, done = root / INBOX, root / INBOX / "done"
    inbox.mkdir(parents=True, exist_ok=True)
    folder = root / "fulltext" / f"board_{board}"
    folder.mkdir(parents=True, exist_ok=True)
    inc = included(store, board)
    missing = [w for w in inc if not _has_text(store, w)]
    candidates = tables.works(store, missing)
    labels = {inc[w]["label"].lower(): w for w in missing if inc[w]["label"]}
    grobid = grobid_url(settings)
    counts = {"added": [], "unmatched": [], "failed": []}
    for path in sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"):
        pdf = path.read_bytes()
        if not pdf.startswith(b"%PDF-"):
            counts["failed"].append(f"{path.name}: not a PDF")
            continue
        w, how = (labels[path.stem.lower()], "label") if path.stem.lower() in labels else \
            library.match(path.stem, pdf, candidates)
        if w is None:
            counts["unmatched"].append(path.name)
            continue
        ok, parser, err = library._store_pdf(store, w, pdf, folder, grobid, root)
        if not ok:
            counts["failed"].append(f"{path.name}: {err or 'no text found'}")
            continue
        counts["added"].append(w)
        log(f"  {path.name} -> #{w} {candidates[w]['cite']} (matched by {how}, {parser})")
        done.mkdir(exist_ok=True)
        shutil.move(str(path), str(done / path.name))
        candidates.pop(w, None)
    if counts["added"]:
        with store.db:
            _event(store, board, "pdfs_added", {"works": counts["added"]})
    return counts


# Independence and status

def overlap(store: Store, board: str, root: Path = ROOT) -> list[dict]:
    """For each pair of chosen lanes, the share of the smaller lane's included sources the other lane also has;
    pairs above the board's max_overlap are flagged."""
    limit = rules(config(store, board, root))["max_overlap"]
    sets = {lid: set() for lid in lanes(store, board)}
    for w, s in included(store, board).items():
        for lid in s["lanes"]:
            if lid in sets:
                sets[lid].add(w)
    out = []
    for a, b in combinations(sets, 2):
        small = min(len(sets[a]), len(sets[b]))
        shared = len(sets[a] & sets[b])
        share = shared / small if small else 0.0
        out.append({"lanes": (a, b), "shared": shared, "share": share, "flag": share > limit})
    return out


def status(store: Store, board: str, root: Path = ROOT) -> dict:
    cfg = config(store, board, root)
    row = board_row(store, board)
    per_lane = {}
    for lid, ln in lanes(store, board).items():
        c = store.db.execute("SELECT count(*) n, sum(decision = 'include') inc, sum(decision = 'exclude') exc, "
                             "sum(decision IS NULL) open FROM board_sources WHERE board = ? AND lane = ?",
                             (board, lid)).fetchone()
        s = store.db.execute("SELECT count(*) n, sum(status = 'failed') failed FROM board_searches WHERE board = ? AND "
                             "lane = ?", (board, lid)).fetchone()
        per_lane[lid] = {"type": ln["type"], "pool": c["n"], "included": c["inc"] or 0, "excluded": c["exc"] or 0,
                         "unscreened": c["open"] or 0, "searches": s["n"], "failed": s["failed"] or 0}
    return {"board": board, "title": cfg.get("title"), "run_id": row["run_id"], "config": row["config_path"],
            "sha256": row["config_sha256"], "lanes": per_lane, "overlap": overlap(store, board, root),
            "todo": len(todo(store, board))}


def status_text(s: dict) -> str:
    lines = [f"Board {s['board']} (run {s['run_id']}; {s['config']} frozen, sha256 {s['sha256'][:12]})", "",
             "  lane      type                   pool  incl  excl  open  searches"]
    for lid, c in s["lanes"].items():
        failed = f" ({c['failed']} failed)" if c["failed"] else ""
        lines.append(f"  {lid:<9} {c['type']:<21} {c['pool']:>5} {c['included']:>5} {c['excluded']:>5} "
                     f"{c['unscreened']:>5}  {c['searches']}{failed}")
    flagged = [o for o in s["overlap"] if o["flag"]]
    lines += ["", "Overlap: " + ("; ".join(f"{a}/{b} share {o['share']:.0%} ({o['shared']} sources)"
                                           for o in flagged for a, b in [o["lanes"]]) + " (above the limit)"
                                 if flagged else "no pair of lanes above the limit"),
              f"Full texts still missing for included sources: {s['todo']}"]
    return "\n".join(lines)
