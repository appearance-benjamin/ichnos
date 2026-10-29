"""Recall test: how many of the studies cited by known sources (consensus statements, prior reviews) did the search find?

The protocol names the sources whose reference lists form the known set: `[recall] sources`, ids of its
[[background]] entries, each with a DOI. Their references come from OpenAlex (Semantic Scholar where OpenAlex
lists none); every request is logged. Each reference is matched to a record in the store and classed: found by
the database search, found only by citation searching, or not found.

Which of them are relevant is decided by the review's own title-and-abstract rule, so that found and missed
studies are judged alike: a found record is relevant when the review's screening kept it; a reference the search
did not find is scored by agents on the same criteria (task kind `recall`, the screening instructions), and is
relevant when an agent includes it (`unsure` is reported apart). Recall is the share of relevant references that
the search found. The test adds nothing to the review: its judgements are kept in `recall_judgements`, not in
the screening.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from . import query, tables
from .http import ApiError, Http
from .sources.openalex import SELECT, parse_work
from .sources.semantic_scholar import PAUSE_S, parse_paper
from .store import Store, normalize_doi, now

KINDS = ("recall",)
SOURCE = "recall_test"
TRANSLATED = "openalex_translated"
SCHEMA = """
CREATE TABLE IF NOT EXISTS recall_refs (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    source      TEXT NOT NULL,                   -- the background id whose reference list it is on (B5, ...)
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    via         TEXT NOT NULL,                   -- openalex or semantic_scholar
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, source, work_id)
);
CREATE TABLE IF NOT EXISTS recall_judgements (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    judge       TEXT NOT NULL,
    decision    TEXT NOT NULL,                   -- include, exclude, unsure (the screening rule)
    reason      TEXT,
    scores      TEXT,                            -- JSON: metric -> score
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, work_id, judge)
);
CREATE TABLE IF NOT EXISTS search_trials (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    label       TEXT NOT NULL,
    query       TEXT NOT NULL,                   -- as sent to OpenAlex
    total       INTEGER,
    fetched     INTEGER,
    in_pool     INTEGER,                         -- of the fetched, records the run already had
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, label)
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def known_sources(protocol: dict) -> list[dict]:
    ids = (protocol.get("recall") or {}).get("sources") or []
    bg = {b["id"]: b for b in protocol.get("background") or []}
    missing = [i for i in ids if i not in bg or "doi.org/" not in bg[i].get("reference", "")]
    if missing:
        raise ValueError(f"[recall] sources {missing} are not background entries with a DOI")
    return [dict(bg[i], doi=normalize_doi(bg[i]["reference"].rsplit("doi.org/", 1)[1].split()[0])) for i in ids]


def references(http: Http, doi: str, openalex_key: str | None = None, contact_email: str | None = None):
    """(via, parsed records) of the works a DOI cites: OpenAlex, else Semantic Scholar."""
    keys = {**({"api_key": openalex_key} if openalex_key else {}), **({"mailto": contact_email} if contact_email else {})}
    try:
        ids = http.get_json(SOURCE, f"https://api.openalex.org/works/doi:{doi}",
                            {"select": "id,referenced_works", **keys}).get("referenced_works") or []
    except ApiError:
        ids = []
    ids = [i.rsplit("/", 1)[-1] for i in ids]
    out = []
    for i in range(0, len(ids), 50):
        data = http.get_json(SOURCE, "https://api.openalex.org/works",
                             {"filter": "openalex:" + "|".join(ids[i:i + 50]), "select": SELECT, "per_page": 50,
                              **keys})
        out += [parse_work(w) for w in data.get("results") or []]
    if out:
        return "openalex", out
    from .mining import edges
    try:
        got = edges(http, f"DOI:{doi}", "references")
    except ApiError:
        return "semantic_scholar", []
    http.sleep(PAUSE_S)
    return "semantic_scholar", [parse_paper(p) for p in got]


def fetch(store: Store, run_id: int, protocol: dict, http: Http | None = None, openalex_key: str | None = None,
          contact_email: str | None = None, log=print) -> Counter:
    """Fetch and store the reference lists of the known sources. Records join the store's works, not the run."""
    ensure(store)
    http = http or Http(store, run_id, max_retries=4)
    counts = Counter()
    for src in known_sources(protocol):
        via, recs = references(http, src["doi"], openalex_key, contact_email)
        log(f"  {src['id']} ({src['cite']}): {len(recs)} references from {via}")
        counts[src["id"]] = len(recs)
        with store.db:
            store.db.execute("DELETE FROM recall_refs WHERE run_id = ? AND source = ?", (run_id, src["id"]))
        for rec in recs:
            if not rec.get("title"):
                continue
            w = store.upsert_work(rec)
            with store.db:
                store.db.execute("INSERT OR IGNORE INTO recall_refs (run_id, source, work_id, via, created_at) "
                                 "VALUES (?, ?, ?, ?, ?)", (run_id, src["id"], w, via, now()))
    return counts


def _expand(string: str) -> str:
    """OpenAlex's exact title-and-abstract search takes no truncation: word* becomes its common endings."""
    return re.sub(r"\b(\w+)\*", lambda m: "(" + " OR ".join(m.group(1) + e for e in ("", "s", "es", "ing", "ed")) + ")",
                  string)


def trial(store: Store, run_id: int, protocol: dict, http: Http | None = None, openalex_key: str | None = None,
          contact_email: str | None = None) -> list[dict]:
    """Search trials, logged and stored but not screened: the review's string, the broader citation-search string and
    the protocol's non-English phrases (`[recall] trial_terms`) in OpenAlex titles and abstracts; for the phrases, how
    many of their records the run already had."""
    ensure(store)
    http = http or Http(store, run_id, max_retries=4)
    keys = {**({"api_key": openalex_key} if openalex_key else {}), **({"mailto": contact_email} if contact_email else {})}
    pooled = {r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ?", (run_id,))}
    items = [("the review's search string", protocol["query"], False)]
    if (protocol.get("mining") or {}).get("query"):
        items.append(("the broader string used for citation searching", protocol["mining"]["query"], False))
    items += [(f"non-English phrase {t}", t, True) for t in (protocol.get("recall") or {}).get("trial_terms") or []]
    out = []
    for label, string, fetch in items:
        sent = query.render(query.parse(" ".join(_expand(string).split())), "openalex")
        data = http.get_json(SOURCE, "https://api.openalex.org/works",
                             {"filter": f"title_and_abstract.search.exact:{sent}", "per_page": 200 if fetch else 1,
                              "select": SELECT, **keys})
        recs = [parse_work(w) for w in data.get("results") or []] if fetch else []
        row = {"label": label, "query": sent, "total": (data.get("meta") or {}).get("count"),
               "fetched": len(recs) if fetch else None,
               "in_pool": sum(1 for r in recs if (store.find_work(r) or -1) in pooled) if fetch else None}
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO search_trials (run_id, label, query, total, fetched, in_pool, "
                             "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (run_id, row["label"], row["query"], row["total"],
                                                                          row["fetched"], row["in_pool"], now()))
        out.append(row)
    return out


def add_translations(store: Store, run_id: int, protocol: dict, http: Http | None = None,
                     openalex_key: str | None = None, contact_email: str | None = None) -> list[int]:
    """The records the non-English phrases find in OpenAlex titles and abstracts join the run like a source's: new ones
    go on the screening sheet and pass every stage. Record the addition under [[deviations]]."""
    from .agents import sheet_ids
    http = http or Http(store, run_id, max_retries=4)
    keys = {**({"api_key": openalex_key} if openalex_key else {}), **({"mailto": contact_email} if contact_email else {})}
    terms = (protocol.get("recall") or {}).get("trial_terms") or []
    if not terms:
        return []
    pooled = {r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ?", (run_id,))}
    before = set(sheet_ids(store, run_id))
    total, ids, rank = 0, [], 0
    for t in terms:
        sent = query.render(query.parse(t), "openalex")
        data = http.get_json(TRANSLATED, "https://api.openalex.org/works",
                             {"filter": f"title_and_abstract.search.exact:{sent}", "per_page": 200, "select": SELECT,
                              **keys})
        total += (data.get("meta") or {}).get("count") or 0
        for w in data.get("results") or []:
            rank += 1
            rec = parse_work(w)
            work = store.upsert_work(rec)
            store.add_retrieval(run_id, work, TRANSLATED, rec["source_id"], rank)
            ids.append(work)
    new = sorted(set(ids) - pooled)
    store.add_sample(run_id, [w for w in new if w not in before], "pilot_screening")
    store.set_run_source(run_id, TRANSLATED, " OR ".join(terms), total, len(set(ids)), "ok",
                         notes=f"searched {now()[:10]}, after the second editorial decision; {len(new)} new records")
    return new


def trials(store: Store, run_id: int) -> list[dict]:
    ensure(store)
    return [dict(r) for r in store.db.execute("SELECT * FROM search_trials WHERE run_id = ? ORDER BY rowid", (run_id,))]


def classify(store: Store, run_id: int, protocol: dict) -> dict[int, dict]:
    """Each referenced record: its sources, whether and how the search found it, and the review's decisions."""
    from .agents import kept_ids, sheet_ids
    ensure(store)
    refs: dict[int, dict] = {}
    for r in store.db.execute("SELECT source, work_id FROM recall_refs WHERE run_id = ?", (run_id,)):
        refs.setdefault(r["work_id"], {"sources": []})["sources"].append(r["source"])
    by_source: dict[int, set[str]] = {}
    for r in store.db.execute("SELECT work_id, source FROM retrievals WHERE run_id = ?", (run_id,)):
        by_source.setdefault(r["work_id"], set()).add(r["source"])
    sheet, passed, kept = set(sheet_ids(store, run_id)), set(kept_ids(store, run_id, "title_abstract")), \
        set(kept_ids(store, run_id))
    core = {e["work_id"] for e in tables.eligibility(store, run_id, protocol) if e["core"]} if tables.criteria(protocol) \
        else set()
    judged: dict[int, list[str]] = {}
    for r in store.db.execute("SELECT work_id, decision FROM recall_judgements WHERE run_id = ?", (run_id,)):
        judged.setdefault(r["work_id"], []).append(r["decision"])
    tree = query.parse(" ".join(protocol["query"].split()))
    meta = tables.works(store, refs)
    for w, d in refs.items():
        srcs = by_source.get(w, set())
        d["found"] = "database" if srcs - {"citation_search"} else "citation_search" if srcs else "not_found"
        d["screened"], d["kept_ta"], d["retained"], d["core"] = w in sheet, w in passed, w in kept, w in core
        d["judgements"] = judged.get(w, [])
        d["label_in_text"] = query.matches(tree, f"{meta[w]['title'] or ''} {meta[w]['abstract'] or ''}")
        d["has_abstract"] = len((meta[w]["abstract"] or "").strip()) > 50
    return refs


def to_judge(store: Store, run_id: int, protocol: dict) -> list[int]:
    return sorted(w for w, d in classify(store, run_id, protocol).items() if d["found"] == "not_found")


def summary(store: Store, run_id: int, protocol: dict) -> dict:
    refs = classify(store, run_id, protocol)
    out = {"sources": [s["id"] for s in known_sources(protocol)], "references": len(refs),
           "by_source": dict(Counter(s for d in refs.values() for s in d["sources"])),
           "found": dict(Counter(d["found"] for d in refs.values()))}
    found = [d for d in refs.values() if d["found"] != "not_found"]
    missed = [d for d in refs.values() if d["found"] == "not_found"]
    rel_found = [d for d in found if d["kept_ta"]]
    rel_missed = [d for d in missed if "include" in d["judgements"]]
    unsure_missed = [d for d in missed if "unsure" in d["judgements"] and "include" not in d["judgements"]]
    broad = (protocol.get("mining") or {}).get("query")
    broad_tree = query.parse(" ".join(broad.split())) if broad else None
    meta = tables.works(store, [w for w, d in refs.items() if d["found"] == "not_found" and "include" in d["judgements"]])
    out.update(
        relevant_missed_with_broad=sum(1 for m in meta.values() if broad_tree and query.matches(
            broad_tree, f"{m['title'] or ''} {m['abstract'] or ''}")) if broad else None,
        found_screened=sum(d["screened"] for d in found), relevant_found=len(rel_found),
        relevant_found_by=dict(Counter(d["found"] for d in rel_found)),
        relevant_found_retained=sum(d["retained"] for d in rel_found), relevant_found_core=sum(d["core"] for d in rel_found),
        missed_judged=sum(1 for d in missed if d["judgements"]), relevant_missed=len(rel_missed),
        unsure_missed=len(unsure_missed),
        relevant_missed_with_label=sum(d["label_in_text"] for d in rel_missed),
        relevant_missed_with_abstract=sum(d["has_abstract"] for d in rel_missed),
        missed_ids=sorted(w for w, d in refs.items() if d["found"] == "not_found" and "include" in d["judgements"]),
        unsure_ids=sorted(w for w, d in refs.items() if d["found"] == "not_found" and "unsure" in d["judgements"]
                          and "include" not in d["judgements"]))
    n = out["relevant_found"] + out["relevant_missed"]
    out["recall"] = round(out["relevant_found"] / n, 3) if n else None
    m = n + out["unsure_missed"]
    out["recall_if_unsure_relevant"] = round(out["relevant_found"] / m, 3) if m else None
    out["per_source"] = {}
    for s in out["sources"]:
        ds = [d for d in refs.values() if s in d["sources"]]
        out["per_source"][s] = {"references": len(ds), "found": sum(d["found"] != "not_found" for d in ds),
                                "retained": sum(d["retained"] for d in ds), "core": sum(d["core"] for d in ds),
                                "relevant_found": sum(d["found"] != "not_found" and d["kept_ta"] for d in ds),
                                "relevant_missed": sum(d["found"] == "not_found" and "include" in d["judgements"]
                                                       for d in ds)}
    out["date"] = (store.db.execute("SELECT max(created_at) FROM recall_refs WHERE run_id = ?", (run_id,)).fetchone()[0]
                   or "")[:10]
    return out


# The agent task: the review's screening criteria, applied to references the search did not find

def write_task(store: Store, run_id: int, kind: str, agent: str, protocol: dict, sha: str, root: Path,
               options: dict, batch_size: int = 25) -> Path:
    from .agents import _new_folder, screen_instructions
    ensure(store)
    ids = to_judge(store, run_id, protocol)
    if not ids:
        raise ValueError("no references to judge: run `recall <run>` first")
    rows = store.db.execute(f"SELECT work_id, title, abstract, year, venue, type FROM works WHERE work_id IN "
                            f"({','.join('?' * len(ids))}) ORDER BY work_id", tuple(ids)).fetchall()
    records = [{k: r[k] for k in ("work_id", "year", "title", "abstract", "venue", "type")} for r in rows]
    corpus = protocol.get("corpus", "")
    out = _new_folder(root, corpus, run_id, kind, agent)
    text = screen_instructions(protocol).replace(
        "\n", "\n\nThese records come from the reference lists of consensus statements and earlier reviews on the "
              "topic; they test how much of the literature the review's search finds. Judge each exactly as in "
              "screening: the scores decide, not where the record was found.\n", 1)
    (out / "instructions.md").write_text(text, encoding="utf-8")
    batches = [records[i:i + batch_size] for i in range(0, len(records), batch_size)]
    for n, b in enumerate(batches, 1):
        (out / f"batch_{n:02d}.json").write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = {"kind": kind, "agent": agent, "run_id": run_id, "corpus": corpus, "protocol_sha256": sha,
            "batches": len(batches), "records": len(records)}
    (out / "task.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return out


def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions: str) -> dict:
    from .agents import _check_screen, _record_text, _strip_fences, _unpack, batch_files, decide, grounded
    ensure(store)
    metrics = protocol["screening"]["metrics"]
    problems, pending = [], []
    for batch in batch_files(folder):
        result = batch.with_name(batch.stem + "_result.json")
        if not result.exists():
            problems.append(f"{result.name} is missing")
            continue
        data = json.loads(batch.read_text(encoding="utf-8"))
        raw = result.read_text(encoding="utf-8")
        try:
            answers, _ = _unpack(json.loads(_strip_fences(raw)))
        except ValueError as err:
            problems.append(f"{result.name}: not valid JSON ({err})")
            continue
        problems += [f"{result.name}: {e}" for e in _check_screen(data, answers, protocol)]
        pending.append((batch, data, raw, answers))
    if problems:
        return {"stored": 0, "problems": problems}
    counts = {"stored": 0, "evidence": 0, "grounded": 0, "insights": 0, "problems": []}
    for batch, data, raw, answers in pending:
        call_id = store.log_call(meta["run_id"], "llm", meta["agent"], f"{instructions}\n\n[recall batch {batch.name}, "
                                 f"sha256 {hashlib.sha256(batch.read_bytes()).hexdigest()}]", 200, 0, raw.encode("utf-8"),
                                 {"agent": meta["agent"], "task": "recall", "records": len(data)})
        texts = {int(r["work_id"]): _record_text(r) for r in data}
        with store.db:
            for a in answers:
                w = int(a["work_id"])
                scores = {m["id"]: int(a["scores"][m["id"]]["score"]) for m in metrics}
                for m in metrics:
                    g = grounded(a["scores"][m["id"]].get("evidence"), texts[w])
                    counts["evidence"] += g is not None
                    counts["grounded"] += bool(g)
                decision, reason = decide(scores, a.get("kind", "study"), metrics)
                store.db.execute("INSERT OR REPLACE INTO recall_judgements (run_id, work_id, judge, decision, reason, "
                                 "scores, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (meta["run_id"], w, meta["agent"], decision, reason, json.dumps(scores), call_id, now()))
                counts["stored"] += 1
    return counts
