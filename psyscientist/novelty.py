"""Literature novelty check for the lens layer's credited hypotheses.

"New" means more than new to this project: has a study already stated or tested the hypothesis? Every step
leaves a trail:

1. novelty_queries: an agent writes, for each hypothesis, a strict and a broad search string in the pipeline's
   boolean syntax (checked with query.parse) and a short plain-text query. Recall controls are mixed in:
   hypotheses made from findings of the review's own studies, so the search should find their source.
   novelty_plain: a fresh agent rewrites only the plain query of checks whose plain search found nothing; the
   check, its key and its boolean strings stay, and each change is kept in novelty_rewrites.
2. search (code): OpenAlex (strict and broad, relevance-ranked, titles and abstracts) and Semantic Scholar
   (plain text); every call is logged in `calls`; hits are de-duplicated and marked when already in the corpus.
3. novelty_judge: two judges label every hit, blind to each other: states_it, tests_it, tests_neighbour,
   related, unrelated or unreadable. states_it and tests_it need a verbatim quote from the hit, which code
   checks.
4. verdicts (code): prior (both judges found a hit that states or tests it), contested (they disagree),
   near (both found a test of a close neighbour: the same mechanism with another outcome or population),
   not_searchable (nothing readable came back) or novel ("no prior or neighbouring test found in the logged
   searches", which is not proof that none exists).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from . import query, tables
from .config import ROOT, Settings
from .http import ApiError, Http
from .store import Store, normalize_doi, now, title_key

KINDS = ("novelty_queries", "novelty_plain", "novelty_judge")
LABELS = ("states_it", "tests_it", "tests_neighbour", "related", "unrelated", "unreadable")
PRIOR = ("states_it", "tests_it")
SCHEMA = """
CREATE TABLE IF NOT EXISTS novelty_checks (
    check_id    INTEGER PRIMARY KEY,
    tournament  TEXT NOT NULL,
    run_id      INTEGER REFERENCES runs(run_id),
    item        TEXT NOT NULL,                   -- the key the agents saw
    output_id   INTEGER,                         -- the lens output the hypothesis came from (NULL for controls)
    lens        TEXT,
    hypothesis  TEXT,
    control_work INTEGER,                        -- for a recall control: the study whose finding it restates
    statement   TEXT NOT NULL,
    strict      TEXT,
    broad       TEXT,
    plain       TEXT,
    searched_at TEXT,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS novelty_hits (
    hit_id      INTEGER PRIMARY KEY,
    check_id    INTEGER NOT NULL REFERENCES novelty_checks(check_id),
    source      TEXT NOT NULL,
    query_kind  TEXT NOT NULL,                   -- strict, broad or plain
    rank        INTEGER,
    doi         TEXT,
    external_id TEXT,
    title       TEXT,
    abstract    TEXT,
    year        INTEGER,
    in_corpus   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS novelty_labels (
    label_id    INTEGER PRIMARY KEY,
    check_id    INTEGER NOT NULL REFERENCES novelty_checks(check_id),
    hit_id      INTEGER NOT NULL REFERENCES novelty_hits(hit_id),
    label       TEXT NOT NULL,
    quote       TEXT,
    grounded    INTEGER,
    judge       TEXT NOT NULL,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS novelty_rewrites (
    rewrite_id  INTEGER PRIMARY KEY,
    check_id    INTEGER NOT NULL REFERENCES novelty_checks(check_id),
    field       TEXT NOT NULL,                   -- plain (the boolean strings are never rewritten)
    old         TEXT,
    new         TEXT NOT NULL,
    agent       TEXT NOT NULL,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
"""
PLAIN_WORDS = 8          # OpenAlex's plain search needs every word in the title or abstract
SYNTAX =("Boolean syntax: AND, OR and NOT written out; \"exact phrases\" in double quotes; a trailing * truncates "
          "a word (retire*); parentheses group; no commas. Example: (\"meaning in life\" OR purpose) AND "
          "(unemploy* OR \"job loss\") AND (\"income replacement\" OR benefit*)")


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def check_key(row) -> tuple:
    """How a check is known: a recall control by its study; a lens hypothesis by (output, lens, hypothesis); a
    discovery by (None, "discovery", its id)."""
    return ("control", row["control_work"]) if row["control_work"] else (row["output_id"], row["lens"], row["hypothesis"])


# 1. Queries

def candidates(store: Store, run_id: int, root: Path, tournament: str, controls: int = 4) -> list[dict]:
    from . import lenses
    result = lenses.evaluate(store, run_id, root, tournament)
    out = []
    for source, items in result.get("hypothesis_items", {}).items():
        if not source.startswith("tournament:"):
            continue
        lens = source.split(":", 1)[1]
        out += [{"output_id": h["output_id"], "lens": lens, "hypothesis": h["id"], "statement":
                 f"{h['statement']} (condition: {h.get('condition') or '-'}; forbids: {h.get('forbids') or '-'})"}
                for h in items]
    # recall controls: findings of core studies with a DOI, so the search should find their source
    core = [e["work_id"] for e in tables.eligibility(store, run_id, _protocol(store, run_id))
            if e["core"] and e["findings"]] if tables.criteria(_protocol(store, run_id)) else []
    works = tables.works(store, core)
    rows = [r for r in tables.finding_rows(store, run_id) if r["work_id"] in works and works[r["work_id"]]["doi"]]
    seen = set()
    for r in sorted(rows, key=lambda r: r["finding_id"]):
        if r["work_id"] in seen or len(seen) >= controls:
            continue
        seen.add(r["work_id"])
        out.append({"output_id": None, "lens": None, "hypothesis": None, "control_work": r["work_id"],
                    "statement": r["finding"]})
    return sorted(out, key=lambda c: hashlib.sha256(f"{tournament}-{c['statement']}".encode()).hexdigest())


def _protocol(store: Store, run_id: int) -> dict:
    from .checks import current_protocol
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    return current_protocol(store, corpus)


PLAIN_RULES = [
    f"- 3-5 key terms, at most {PLAIN_WORDS} words in all: the outcome, the predictor or condition, and the population "
    "or setting when it is distinctive. The search returns only titles and abstracts that contain every word, so "
    "each extra word narrows it;",
    "- a fixed phrase is one term and stays together in its usual order (meaning in life, job loss, bridge "
    "employment);",
    "- leave out design and method words (randomised, longitudinal, matched controls) and theory or author names, "
    "unless the hypothesis is about them;",
    "- plain words only: no quotes, AND/OR/NOT, parentheses, asterisks or commas."]


def queries_instructions() -> str:
    return "\n".join([
        "# Search strings for a novelty check", "",
        "batch_01.json lists `items`: hypotheses (or statements) whose prior literature we want to find. For each, "
        "write the searches a careful reviewer would run to find any study that already states or tests it:", "",
        "- strict: all its key concepts (outcome AND predictor or condition AND population or setting), each as an "
        "OR-group of synonyms and common measures;",
        "- broad: the same without the population or setting group, to catch tests in neighbouring populations;",
        "- plain: a short plain-text query for a relevance-ranked search:", "", *("  " + r for r in PLAIN_RULES), "",
        SYNTAX, "", "Searches run on titles and abstracts. Use only batch_01.json; open no other file.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"queries": [{"key": "q1", "strict": "...", "broad": "...", "plain": "..."}]}, indent=1), "```"]) + "\n"


def plain_instructions() -> str:
    return "\n".join([
        "# Short plain-text queries for a novelty check", "",
        "batch_01.json lists `items`: hypotheses (or statements) whose prior literature we want to find. For each, "
        "write one plain-text query that a careful reviewer would type into a relevance-ranked search of titles and "
        "abstracts to find any study that already states or tests it:", "", *PLAIN_RULES, "",
        "Use only batch_01.json; open no other file, do not search the web.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"queries": [{"key": "q1", "plain": "..."}]}, indent=1), "```"]) + "\n"


def judge_instructions() -> str:
    return "\n".join([
        "# Has this already been stated or tested?", "",
        "Each batch file lists `checks`: a hypothesis and the `hits` a literature search returned for it (title, "
        "year, abstract). Label every hit for its hypothesis:", "",
        "- states_it: the record already states this hypothesis or its conclusion as a claim;",
        "- tests_it: the record reports a study that tests this hypothesis (same outcome, predictor or condition and "
        "population type), whatever the result;",
        "- tests_neighbour: it tests something close (another outcome or population, the same mechanism);",
        "- related: same topic, but it neither states nor tests the hypothesis;",
        "- unrelated;",
        "- unreadable: no abstract and the title alone does not allow a judgement.", "",
        "For states_it and tests_it give `quote`: the exact words of the title or abstract that show it. Be strict: "
        "a similar topic is not a test.", "",
        "Use only your batch file; open no other file, do not search the web.", "",
        "Output: one JSON object per batch file, written to batch_NN_result.json:", "", "```json",
        json.dumps({"labels": [{"check": "c1", "hit": 12, "label": "related", "quote": None}]}, indent=1), "```"]) + "\n"


def write_task(store: Store, run_id: int, kind: str, agent: str, protocol: dict, sha: str, root: Path,
               options: dict) -> Path:
    from . import lenses
    from .agents import _new_folder
    ensure(store)
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    _, _, cfg = lenses.load(corpus, root)
    tour = options.get("tournament") or cfg["interpret"].get("tournament", "pilot")
    meta = {"kind": kind, "agent": agent, "run_id": run_id, "corpus": corpus, "protocol_sha256": sha,
            "tournament": tour}
    out = _new_folder(root, corpus, run_id, kind, agent)
    if kind == "novelty_queries":
        # only what has no search strings yet (newly credited hypotheses), numbered on from the existing checks
        have = store.db.execute("SELECT item, output_id, lens, hypothesis, control_work FROM novelty_checks WHERE "
                                "tournament = ? AND run_id = ?", (tour, run_id)).fetchall()
        done = {check_key(r) for r in have}
        items = [c for c in candidates(store, run_id, root, tour)
                 if ((c["output_id"], c["lens"], c["hypothesis"]) if c["output_id"] else
                     ("control", c.get("control_work"))) not in done]
        if not items:
            raise ValueError("every credited hypothesis already has search strings" if have
                             else "no credited hypotheses to check")
        start = max([int(r["item"][1:]) for r in have if re.fullmatch(r"q\d+", r["item"] or "")] or [0])
        keys = {f"q{n}": c for n, c in enumerate(items, start + 1)}
        data = {"items": [{"key": k, "statement": c["statement"]} for k, c in keys.items()]}
        (out / "batch_01.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "instructions.md").write_text(queries_instructions(), encoding="utf-8")
        meta.update(keys=keys, batches=1, records=len(items))
    elif kind == "novelty_plain":
        # only the checks whose plain search found nothing; the agent sees the statement, not the old queries
        rows = store.db.execute("SELECT check_id, item, statement FROM novelty_checks WHERE tournament = ? AND "
                                "run_id = ? AND searched_at IS NOT NULL AND check_id NOT IN (SELECT check_id FROM "
                                "novelty_hits WHERE query_kind = 'plain') ORDER BY check_id", (tour, run_id)).fetchall()
        if not rows:
            raise ValueError("every searched check has plain-search hits" if store.db.execute(
                "SELECT 1 FROM novelty_checks WHERE tournament = ? AND run_id = ? AND searched_at IS NOT NULL",
                (tour, run_id)).fetchone() else "no searched checks yet: write and import the queries, then search")
        data = {"items": [{"key": r["item"], "statement": r["statement"]} for r in rows]}
        (out / "batch_01.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "instructions.md").write_text(plain_instructions(), encoding="utf-8")
        meta.update(keys={r["item"]: r["check_id"] for r in rows}, batches=1, records=len(rows))
    else:
        searched = store.db.execute("SELECT * FROM novelty_checks WHERE tournament = ? AND run_id = ? AND searched_at "
                                    "IS NOT NULL ORDER BY item", (tour, run_id)).fetchall()
        if not searched:
            raise ValueError("no searched checks yet: write and import the queries, then run the searches")
        batches = judge_batches(store, searched, agent, int(options.get("per_batch") or 6))
        for n, data in enumerate(batches, 1):
            (out / f"batch_{n:02d}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "instructions.md").write_text(judge_instructions(), encoding="utf-8")
        meta.update(batches=len(batches), records=sum(len(b["checks"]) for b in batches))
    (out / "task.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def judge_batches(store: Store, searched: list, agent: str, size: int = 6) -> list[dict]:
    """The judge's batch files for searched checks: only the hits this judge has not labelled yet (new checks, or
    hits added by searching again)."""
    todo = {c["check_id"]: store.db.execute(
        "SELECT * FROM novelty_hits h WHERE check_id = ? AND NOT EXISTS (SELECT 1 FROM novelty_labels l WHERE "
        "l.hit_id = h.hit_id AND l.judge = ?) ORDER BY hit_id", (c["check_id"], agent)).fetchall() for c in searched}
    checks = [c for c in searched if todo[c["check_id"]]]
    if not checks:
        raise ValueError(f"{agent} has labelled every hit already")
    return [{"checks": [{"check": c["item"], "hypothesis": c["statement"],
                         "hits": [{"hit": h["hit_id"], "title": h["title"], "year": h["year"],
                                   "abstract": (h["abstract"] or "")[:1500] or None} for h in todo[c["check_id"]]]}
                        for c in checks[i:i + size]]}
            for i in range(0, len(checks), size)]


def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions: str) -> dict:
    from .agents import _strip_fences, batch_files, grounded
    ensure(store)
    problems, answers = [], []
    for batch in batch_files(folder):
        result = batch.with_name(batch.stem + "_result.json")
        if not result.exists():
            problems.append(f"{result.name} is missing")
            continue
        raw = result.read_text(encoding="utf-8")
        try:
            answer = json.loads(_strip_fences(raw))
        except ValueError as err:
            problems.append(f"{result.name}: not valid JSON ({err})")
            continue
        data = json.loads(batch.read_text(encoding="utf-8"))
        errs = _check(meta["kind"], data, answer)
        problems += [f"{result.name}: {e}" for e in errs]
        answers.append((batch, raw, data, answer))
    if meta["kind"] == "novelty_plain":
        # a check that got plain hits since the task was written keeps the query that found them
        have = {r[0] for r in store.db.execute("SELECT DISTINCT check_id FROM novelty_hits WHERE query_kind = 'plain'")}
        problems += [f"{k}: has plain-search hits now; write the task again" for k, cid in meta["keys"].items()
                     if cid in have]
    if problems:
        return {"stored": 0, "problems": problems}
    stored = 0
    for batch, raw, data, answer in answers:
        call_id = store.log_call(meta["run_id"], "llm", meta["agent"], f"{instructions}\n\n[{meta['kind']} batch, sha256 "
                                 f"{hashlib.sha256(batch.read_bytes()).hexdigest()}]", 200, 0, raw.encode("utf-8"),
                                 {"agent": meta["agent"], "task": meta["kind"]})
        with store.db:
            if meta["kind"] == "novelty_queries":
                for q in answer["queries"]:
                    c = meta["keys"][q["key"]]
                    store.db.execute(
                        "INSERT INTO novelty_checks (tournament, run_id, item, output_id, lens, hypothesis, control_work, "
                        "statement, strict, broad, plain, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (meta["tournament"], c.get("run_id") or meta["run_id"], q["key"], c.get("output_id"), c.get("lens"),
                         c.get("hypothesis"), c.get("control_work"), c["statement"], q["strict"], q["broad"],
                         q["plain"], now()))
                    stored += 1
            elif meta["kind"] == "novelty_plain":
                for q in answer["queries"]:
                    cid = meta["keys"][q["key"]]
                    old = store.db.execute("SELECT plain FROM novelty_checks WHERE check_id = ?", (cid,)).fetchone()[0]
                    new = " ".join(q["plain"].split())
                    store.db.execute("UPDATE novelty_checks SET plain = ? WHERE check_id = ?", (new, cid))
                    store.db.execute("INSERT INTO novelty_rewrites (check_id, field, old, new, agent, call_id, "
                                     "created_at) VALUES (?, 'plain', ?, ?, ?, ?, ?)",
                                     (cid, old, new, meta["agent"], call_id, now()))
                    stored += 1
            else:
                # a task of no single run (discoveries) finds its checks by item, which is unique in its tournament
                checks = {c["item"]: c["check_id"] for c in store.db.execute(
                    "SELECT item, check_id FROM novelty_checks WHERE tournament = ? AND (run_id = ? OR ? IS NULL)",
                    (meta["tournament"], meta["run_id"], meta["run_id"]))}
                texts = {h["hit"]: f"{h.get('title') or ''} {h.get('abstract') or ''}"
                         for c in data["checks"] for h in c["hits"]}
                for x in answer["labels"]:
                    ok = grounded(x["quote"], texts[x["hit"]]) if x.get("quote") else None
                    store.db.execute(
                        "INSERT INTO novelty_labels (check_id, hit_id, label, quote, grounded, judge, call_id, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (checks[x["check"]], x["hit"], x["label"], x.get("quote"), None if ok is None else int(ok),
                         meta["agent"], call_id, now()))
                    stored += 1
    return {"stored": stored, "problems": []}


def _plain_problems(text) -> list[str]:
    words = (text or "").split() if isinstance(text, str) else []
    if not words:
        return ["needs a plain query"]
    errs = []
    if len(words) > PLAIN_WORDS:
        errs.append(f"plain: at most {PLAIN_WORDS} words ({len(words)})")
    if re.search(r"[\"“”*(),]", text) or {"AND", "OR", "NOT"} & set(words):
        errs.append("plain: plain words only (no quotes, operators, parentheses, asterisks or commas)")
    return errs


def _check(kind: str, data: dict, answer) -> list[str]:
    if not isinstance(answer, dict):
        return ["the answer must be a JSON object"]
    if kind in ("novelty_queries", "novelty_plain"):
        want = {i["key"] for i in data["items"]}
        got = [q.get("key") for q in answer.get("queries") or []]
        errs = [] if sorted(got) == sorted(want) else ["queries: every item key exactly once"]
        for q in answer.get("queries") or []:
            errs += [f"{q.get('key')}: {e}" for e in _plain_problems(q.get("plain"))]
            if kind == "novelty_plain":
                continue
            for field in ("strict", "broad"):
                text = q.get(field) or ""
                if "," in text:
                    errs.append(f"{q.get('key')} {field}: no commas")
                    continue
                try:
                    query.render(query.parse(text), "openalex")
                except (query.QueryError, ValueError, IndexError) as err:
                    errs.append(f"{q.get('key')} {field}: does not parse ({err})")
        return errs
    want = {(c["check"], h["hit"]) for c in data["checks"] for h in c["hits"]}
    got = [(x.get("check"), x.get("hit")) for x in answer.get("labels") or []]
    errs = [] if sorted(got) == sorted(want) else [f"labels: every hit of every check exactly once "
                                                  f"({len(set(want) - set(got))} missing)"]
    for x in answer.get("labels") or []:
        if x.get("label") not in LABELS:
            errs.append(f"{x.get('check')}/{x.get('hit')}: label {x.get('label')!r}")
        if x.get("label") in PRIOR and not (x.get("quote") or "").strip():
            errs.append(f"{x.get('check')}/{x.get('hit')}: {x['label']} needs a quote")
    return errs


# 2. Searching (code)

def _openalex(http: Http, text: str, settings: Settings, n: int) -> list[dict]:
    from .sources import openalex
    rendered = query.render(query.parse(text), "openalex")
    params = {"filter": f"title_and_abstract.search.exact:{rendered}", "select": openalex.SELECT, "per_page": n,
              "sort": "relevance_score:desc"}
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key
    if settings.contact_email:
        params["mailto"] = settings.contact_email
    try:
        data = http.get_json("openalex", openalex.URL, params)
    except ApiError as err:
        if err.status != 400:
            raise
        params.pop("sort")                   # some filters do not support relevance sorting
        data = http.get_json("openalex", openalex.URL, params)
    return [openalex.parse_work(w) for w in data.get("results") or []]


def _s2(http: Http, text: str, settings: Settings, n: int) -> list[dict]:
    from .sources import semantic_scholar
    headers = {"x-api-key": settings.s2_api_key} if settings.s2_api_key else None
    data = http.get_json("semantic_scholar", "https://api.semanticscholar.org/graph/v1/paper/search",
                         {"query": text, "limit": n, "fields": semantic_scholar.FIELDS}, headers)
    return [semantic_scholar.parse_paper(p) for p in data.get("data") or []]


def _openalex_plain(http: Http, text: str, settings: Settings, n: int) -> list[dict]:
    """OpenAlex's ranked, stemmed title-and-abstract search: the fallback when Semantic Scholar is rate-limited."""
    from .sources import openalex
    words = " ".join(w for w in text.replace(",", " ").split() if w.isalnum() or "-" in w)
    params = {"filter": f"title_and_abstract.search:{words}", "select": openalex.SELECT, "per_page": n,
              "sort": "relevance_score:desc"}
    if settings.openalex_api_key:
        params["api_key"] = settings.openalex_api_key
    if settings.contact_email:
        params["mailto"] = settings.contact_email
    return [openalex.parse_work(w) for w in http.get_json("openalex", openalex.URL, params).get("results") or []]


def fill_plain(store: Store, run_id: int, settings: Settings, tournament: str, per_query: int = 10, log=print,
               http: Http | None = None) -> Counter:
    """For checks whose plain search returned nothing (e.g. Semantic Scholar refused it), run it on OpenAlex.
    OpenAlex matches only titles and abstracts containing every word of the plain query, so long queries
    often come back empty even when the request succeeds (novelty_plain rewrites them shorter). Counts
    `empty` (no work came back) and `only_known` (every work was a hit of the check already), which leave
    the check without plain hits."""
    ensure(store)
    http = http or Http(store, run_id, max_retries=2)
    known_doi = _known_dois(store, run_id)
    counts = Counter()
    for c in store.db.execute("SELECT * FROM novelty_checks WHERE tournament = ? AND run_id = ? AND searched_at IS NOT "
                              "NULL AND check_id NOT IN (SELECT check_id FROM novelty_hits WHERE query_kind = 'plain')",
                              (tournament, run_id)).fetchall():
        old = store.db.execute("SELECT doi, title FROM novelty_hits WHERE check_id = ?", (c["check_id"],)).fetchall()
        seen = {r["doi"] for r in old if r["doi"]} | {title_key(r["title"] or "") for r in old}
        try:
            found = _openalex_plain(http, c["plain"], settings, per_query)
        except ApiError as err:
            counts["failed"] += 1
            log(f"  {c['item']} plain (OpenAlex): {err}")
            continue
        new = 0
        with store.db:
            for rank, w in enumerate(found, 1):
                doi = normalize_doi(w.get("doi"))
                if (doi and doi in seen) or title_key(w.get("title") or "") in seen:
                    continue
                seen |= {doi, title_key(w.get("title") or "")}
                store.db.execute(
                    "INSERT INTO novelty_hits (check_id, source, query_kind, rank, doi, external_id, title, abstract, "
                    "year, in_corpus) VALUES (?, 'openalex', 'plain', ?, ?, ?, ?, ?, ?, ?)",
                    (c["check_id"], rank, doi, w.get("openalex_id"), w.get("title"), w.get("abstract"), w.get("year"),
                     int(bool(doi and doi in known_doi))))
                new += 1
        counts["checks"] += 1
        counts["hits"] += new
        if not found:
            counts["empty"] += 1
        elif not new:
            counts["only_known"] += 1
        log(f"  {c['item']} plain (OpenAlex): {len(found)} works, {new} new")
        http.sleep(0.2)
    return counts


def _known_dois(store: Store, run_id: int) -> set[str]:
    """The DOIs of every record any run of this run's corpus retrieved: a hit with one is marked in_corpus."""
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    return {r[0] for r in store.db.execute("SELECT DISTINCT w.doi FROM works w JOIN retrievals r ON "
                                           "r.work_id = w.work_id JOIN runs u ON u.run_id = r.run_id "
                                           "WHERE u.corpus = ? AND w.doi IS NOT NULL", (corpus,))}


def search(store: Store, run_id: int, settings: Settings, tournament: str, per_query: int = 10, log=print,
           http: Http | None = None) -> Counter:
    """Run the strict, broad and plain searches for every check not yet searched."""
    ensure(store)
    http = http or Http(store, run_id, max_retries=2)
    known_doi = _known_dois(store, run_id)
    counts = Counter()
    for c in store.db.execute("SELECT * FROM novelty_checks WHERE tournament = ? AND run_id = ? AND searched_at IS NULL",
                              (tournament, run_id)).fetchall():
        hits, seen = [], set()
        for kind, text, fn in (("strict", c["strict"], _openalex), ("broad", c["broad"], _openalex),
                               ("plain", c["plain"], _s2)):
            try:
                found = fn(http, text, settings, per_query)
            except (ApiError, ValueError, query.QueryError) as err:
                counts[f"{kind} failed"] += 1
                log(f"  {c['item']} {kind}: {err}")
                continue
            for rank, w in enumerate(found, 1):
                key = normalize_doi(w.get("doi")) or title_key(w.get("title") or "")
                if not key or key in seen:
                    continue
                seen.add(key)
                hits.append((kind, rank, w))
            http.sleep(1.1 if fn is _s2 else 0.2)
        with store.db:
            for kind, rank, w in hits:
                doi = normalize_doi(w.get("doi"))
                store.db.execute(
                    "INSERT INTO novelty_hits (check_id, source, query_kind, rank, doi, external_id, title, abstract, year, "
                    "in_corpus) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (c["check_id"], "semantic_scholar" if kind == "plain" else "openalex", kind, rank, doi,
                     w.get("openalex_id") or w.get("s2_id"), w.get("title"), w.get("abstract"), w.get("year"),
                     int(bool(doi and doi in known_doi))))
            store.db.execute("UPDATE novelty_checks SET searched_at = ? WHERE check_id = ?", (now(), c["check_id"]))
        counts["checks"] += 1
        counts["hits"] += len(hits)
        log(f"  {c['item']}: {len(hits)} hits")
    return counts


# 3. Verdicts (code)

def hit_link(h) -> str | None:
    """Where a reader can open the hit: its DOI, else its OpenAlex or Semantic Scholar page."""
    if h["doi"]:
        return f"https://doi.org/{h['doi']}"
    ext = h["external_id"] or ""
    if h["source"] == "openalex" and re.fullmatch(r"W\d+", ext):
        return f"https://openalex.org/{ext}"
    if h["source"] == "semantic_scholar" and re.fullmatch(r"[0-9a-f]{40}", ext):
        return f"https://www.semanticscholar.org/paper/{ext}"
    return None


def verdicts(store: Store, tournament: str) -> dict[tuple, dict]:
    """Each check's verdict from the labels of every novelty judge of the tournament, in this order:
    not_searched: queries written, search not run yet;
    prior: every judge says a hit states or tests it, each with a quote found in the hit;
    contested: only some judges say so (or, below near, only some say a hit tests a close neighbour);
    unjudged: a hit with an abstract still lacks a judge's label (for example after searching again), so a prior
      could hide among them;
    near: every judge says a hit tests a close neighbour (a claimed direct test whose quote was not found counts
      here, not as prior);
    not_searchable: no hit came back with an abstract;
    novel: none of these."""
    ensure(store)
    judges = [r[0] for r in store.db.execute(
        "SELECT DISTINCT l.judge FROM novelty_labels l JOIN novelty_checks c ON c.check_id = l.check_id "
        "WHERE c.tournament = ? ORDER BY l.judge", (tournament,))]
    out = {}
    for c in store.db.execute("SELECT * FROM novelty_checks WHERE tournament = ?", (tournament,)).fetchall():
        hits = store.db.execute("SELECT * FROM novelty_hits WHERE check_id = ?", (c["check_id"],)).fetchall()
        latest = {}
        for r in store.db.execute("SELECT * FROM novelty_labels WHERE check_id = ? ORDER BY label_id",
                                  (c["check_id"],)):
            latest[(r["hit_id"], r["judge"])] = r          # a judge's latest label counts
        prior_hits, contested_hits, near_hits, split_near = [], [], [], []
        unlabelled = 0
        for h in hits:
            got = [latest.get((h["hit_id"], j)) for j in judges]
            if (h["abstract"] or "").strip() and (not judges or any(g is None for g in got)):
                unlabelled += 1
            says = [g is not None and g["label"] in PRIOR and bool(g["grounded"]) for g in got]
            close = [g is not None and (g["label"] == "tests_neighbour" or g["label"] in PRIOR) for g in got]
            seen = {"title": h["title"], "year": h["year"], "doi": h["doi"], "link": hit_link(h),
                    "labels": [g["label"] if g is not None else None for g in got],
                    "quote": next((g["quote"] for g in got if g is not None and g["quote"] and g["grounded"]), None)}
            if judges and all(says):
                prior_hits.append(seen)
            elif any(says):
                contested_hits.append(seen)
            elif judges and all(close):
                near_hits.append(seen)
            elif any(close):
                split_near.append(seen)
        readable = [h for h in hits if (h["abstract"] or "").strip()]
        if c["searched_at"] is None and not hits:
            verdict = "not_searched"
        elif prior_hits:
            verdict = "prior"
        elif contested_hits:
            verdict = "contested"
        elif unlabelled:
            verdict = "unjudged"
        elif near_hits:
            verdict = "near"                      # a close neighbour was tested: known mechanism, new outcome
        elif split_near:
            verdict = "contested"
        elif not readable:
            verdict = "not_searchable"            # nothing came back that could be judged
        else:
            verdict = "novel"
        control_found = control_prior = None
        if c["control_work"]:
            doi = normalize_doi(store.db.execute("SELECT doi FROM works WHERE work_id = ?",
                                                 (c["control_work"],)).fetchone()[0] or "")
            control_found = bool(doi) and any(normalize_doi(h["doi"] or "") == doi for h in hits)
            control_prior = bool(doi) and any(normalize_doi(p["doi"] or "") == doi for p in prior_hits)
        contested = contested_hits + split_near
        entry = {"check_id": c["check_id"], "item": c["item"], "verdict": verdict, "hits": len(hits),
                 "readable": len(readable), "unlabelled": unlabelled, "judges": judges,
                 "searched": c["searched_at"] is not None,
                 "plain_searched": any(h["query_kind"] == "plain" for h in hits),
                 "prior": prior_hits[:3], "contested": contested[:3], "near": near_hits[:3],
                 "counts": {"prior": len(prior_hits), "contested": len(contested), "near": len(near_hits)},
                 "control_work": c["control_work"], "control_found": control_found,
                 "control_judged_prior": bool(control_prior)}
        out[check_key(c)] = entry
    return out


def summary(store: Store, tournament: str, keys: set | None = None, v: dict | None = None) -> dict:
    """Counts over the hypotheses' checks (only `keys`, the hypotheses credited now, when given); the recall
    controls are counted apart."""
    ensure(store)
    v = verdicts(store, tournament) if v is None else v
    hyp = [e for k, e in v.items() if k[0] != "control" and (keys is None or k in keys)]
    ctrl = [e for k, e in v.items() if k[0] == "control"]
    ids = [e["check_id"] for e in hyp]
    rewritten = {r[0] for r in store.db.execute("SELECT DISTINCT check_id FROM novelty_rewrites WHERE field = 'plain'")}
    labels = store.db.execute(f"SELECT judge, hit_id, label FROM novelty_labels WHERE check_id IN "
                              f"({','.join('?' * len(ids))})", ids).fetchall() if ids else []
    judges = sorted({r["judge"] for r in labels})
    agree = None
    if len(judges) >= 2:
        a = {r["hit_id"]: r["label"] in PRIOR for r in labels if r["judge"] == judges[0]}
        b = {r["hit_id"]: r["label"] in PRIOR for r in labels if r["judge"] == judges[1]}
        both = [h for h in a if h in b]
        agree = {"n": len(both), "agree": round(sum(a[h] == b[h] for h in both) / len(both), 3) if both else None,
                 "both_prior": sum(a[h] and b[h] for h in both), "either_prior": sum(a[h] or b[h] for h in both)}
    return {"verdicts": dict(Counter(e["verdict"] for e in hyp)), "checked": len(hyp),
            "searched": sum(e["searched"] for e in hyp),
            "without_plain": sum(e["searched"] and not e["plain_searched"] for e in hyp),
            "plain_rewritten": sum(e["check_id"] in rewritten for e in hyp),
            "controls_plain_rewritten": sum(e["check_id"] in rewritten for e in ctrl),
            "controls": len(ctrl), "controls_found_by_search": sum(bool(e["control_found"]) for e in ctrl),
            "controls_judged_prior": sum(e["control_judged_prior"] for e in ctrl), "judges": judges,
            "judge_agreement_on_prior": agree,
            "hits": sum(e["hits"] for e in hyp), "readable": sum(e["readable"] for e in hyp),
            "unlabelled": sum(e["unlabelled"] for e in hyp),
            "control_hits": sum(e["hits"] for e in ctrl)}
