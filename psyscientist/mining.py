"""Reference mining and citation searching, from Semantic Scholar or OpenAlex.

Backward: the reference lists of the records excluded as reviews and of the core studies. Forward: the works
that cite the core studies. A mined record joins the run when its title or abstract matches the protocol's
search string, so it meets the same test as a database record; it is stored as a retrieval from the source
"citation_search", put on the screening sheet if it is new, and then passes every stage like any other record.
Every request is logged; the counts go into the run's source row and the Method reports them. Each pass is kept
in the row's `passes`, so a later pass (from more core studies) does not hide what an earlier one added.

`via="openalex"` takes the same links from OpenAlex (a work's `referenced_works`, and the works whose filter
`cites:` names it): far faster than Semantic Scholar without a key. Lookups that already succeeded in this run are
read back from the log, so a pass stopped halfway resumes.
"""

from __future__ import annotations

import json
import urllib.parse

from . import query, tables
from .agents import kept_ids, sheet_ids
from .http import ApiError, Http
from .sources import openalex
from .sources.semantic_scholar import FIELDS, PAUSE_S, parse_paper
from .store import Store, now, title_key

SOURCE = "citation_search"
URL = "https://api.semanticscholar.org/graph/v1/paper/{id}/{edge}"
MATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search/match"
OA_SOURCE = "citation_search_openalex"
OA_WORK = "https://api.openalex.org/works/{key}"
OA_BATCH = 50              # works fetched per request by their ids
OA_MAX_CITING = 2000       # citing works taken per core study
SAMPLE_PURPOSE = "pilot_screening"


def seeds(store: Store, run_id: int, protocol: dict) -> dict[str, list[int]]:
    """Whose reference lists are mined (excluded reviews, core studies) and whose citations (core studies). A review is
    a record on the sheet that any screener excluded as a review and that was not retained."""
    kept = set(kept_ids(store, run_id))
    sheet = set(sheet_ids(store, run_id))
    reviews = sorted({r["work_id"] for r in store.db.execute(
        "SELECT work_id FROM screening WHERE run_id = ? AND decision = 'exclude' AND reason = 'review'", (run_id,))}
        & sheet - kept)
    core = sorted(e["work_id"] for e in tables.eligibility(store, run_id, protocol) if e["core"])
    return {"references": reviews + core, "citations": core, "reviews": reviews, "core": core}


def paper_id(work) -> str | None:
    """How Semantic Scholar knows the work: its own id, the DOI or the PubMed id."""
    ids = paper_ids(work)
    return ids[0] if ids else None


def paper_ids(work) -> list[str]:
    """Every identifier Semantic Scholar may know the work by, best first; a lookup that fails tries the next."""
    return ([work["s2_id"]] if work["s2_id"] else []) + ([f"DOI:{work['doi']}"] if work["doi"] else []) + \
        ([f"PMID:{work['pmid']}"] if work["pmid"] else [])


def match_title(http: Http, work, api_key: str | None = None) -> str | None:
    """A record with no identifier: Semantic Scholar's best title match, accepted only when the titles agree (and the
    years, where both are known)."""
    if not (work["title"] or "").strip():
        return None
    headers = {"x-api-key": api_key} if api_key else None
    try:
        data = http.get_json(SOURCE, MATCH_URL, {"query": work["title"], "fields": "paperId,title,year"}, headers,
                             reuse=True)
    except ApiError:
        return None
    for p in data.get("data") or []:
        if title_key(p.get("title")) == title_key(work["title"]) and \
                (not work["year"] or not p.get("year") or abs(int(p["year"]) - int(work["year"])) <= 1):
            return p.get("paperId")
    return None


def edges(http: Http, pid: str, edge: str, api_key: str | None = None, limit: int = 1000) -> list[dict]:
    """The papers a work cites (edge "references") or that cite it ("citations"), one page of up to `limit`."""
    key = "citedPaper" if edge == "references" else "citingPaper"
    headers = {"x-api-key": api_key} if api_key else None
    out, offset = [], 0
    while True:
        # reuse: a run stopped halfway (a crash, a closed window) picks up its finished lookups from the log
        data = http.get_json(SOURCE, URL.format(id=pid, edge=edge),
                             {"fields": FIELDS, "limit": limit, "offset": offset}, headers, reuse=True)
        rows = [r[key] for r in data.get("data") or [] if r.get(key) and r[key].get("title")]
        out += rows
        nxt = data.get("next")
        if nxt is None or not data.get("data") or len(out) >= 9000:
            return out
        offset = nxt
        http.sleep(PAUSE_S)


def openalex_key(work) -> str | None:
    """How OpenAlex knows the work: its id, else the DOI or the PubMed id."""
    if work["openalex_id"]:
        return work["openalex_id"]
    return f"doi:{work['doi']}" if work["doi"] else f"pmid:{work['pmid']}" if work["pmid"] else None


def openalex_edges(http: Http, key: str, edge: str, auth: dict) -> list[dict]:
    """The works the work cites (`references`) or that cite it (`citations`), as OpenAlex records."""
    work = http.get_json(OA_SOURCE, OA_WORK.format(key=urllib.parse.quote(key, safe=":/")),
                         {"select": "id,referenced_works", **auth}, reuse=True)
    out: list[dict] = []
    if edge == "references":
        ids = [u.rsplit("/", 1)[-1] for u in work.get("referenced_works") or []]
        for i in range(0, len(ids), OA_BATCH):
            data = http.get_json(OA_SOURCE, openalex.URL, {"filter": "openalex:" + "|".join(ids[i:i + OA_BATCH]),
                                                           "per_page": OA_BATCH, "select": openalex.SELECT, **auth},
                                 reuse=True)
            out += data.get("results") or []
        return out
    wid, cursor = work["id"].rsplit("/", 1)[-1], "*"
    while cursor and len(out) < OA_MAX_CITING:
        data = http.get_json(OA_SOURCE, openalex.URL, {"filter": f"cites:{wid}", "per_page": 200, "cursor": cursor,
                                                       "select": openalex.SELECT, **auth}, reuse=True)
        out += data.get("results") or []
        cursor = (data.get("meta") or {}).get("next_cursor") if data.get("results") else None
    return out


def mine(store: Store, run_id: int, protocol: dict, api_key: str | None = None, log=print, via: str = "semantic_scholar",
         openalex_key_: str | None = None, contact_email: str | None = None) -> dict:
    """`[mining] query` (a broader string, e.g. with synonyms) replaces the review's search string for matching."""
    string = " ".join(((protocol.get("mining") or {}).get("query") or protocol["query"]).split())
    tree = query.parse(string)
    who = seeds(store, run_id, protocol)
    http = Http(store, run_id)
    meta = tables.works(store, set(who["references"]) | set(who["citations"]))
    found: dict[str, dict] = {}
    counts = {"seeds_reviews": len(who["reviews"]), "seeds_core": len(who["core"]), "no_identifier": 0,
              "by_title": 0, "failed": 0, "lookups": 0, "references": 0, "citations": 0}
    if via == "openalex":
        auth = {**({"api_key": openalex_key_} if openalex_key_ else {}),
                **({"mailto": contact_email} if contact_email else {})}
        failed: set[int] = set()
        for edge in ("references", "citations"):
            for w in who[edge]:
                key = openalex_key(meta[w])
                if not key:
                    counts["no_identifier"] += 1
                    continue
                counts["lookups"] += 1
                try:
                    got = openalex_edges(http, key, edge, auth)
                except (ApiError, KeyError) as err:
                    counts["failed"] += 1
                    failed.add(w)
                    log(f"  {edge} of {w}: {err}")
                    continue
                counts[edge] += len(got)
                for p in got:
                    rec = openalex.parse_work(p)
                    if rec["source_id"]:
                        found.setdefault(rec["source_id"], rec)
        counts["failed_works"] = len(failed)
        return _join(store, run_id, protocol, tree, string, found, counts, "OpenAlex")
    titled: dict[int, str | None] = {}
    failed: set[int] = set()
    for edge in ("references", "citations"):
        for w in who[edge]:
            pids = paper_ids(meta[w])
            if not pids:
                if w not in titled:
                    titled[w] = match_title(http, meta[w], api_key)
                    http.sleep(PAUSE_S)
                pids = [titled[w]] if titled[w] else []
            if not pids:
                continue
            counts["lookups"] += 1
            got, err = None, None
            for pid in pids:
                try:
                    got = edges(http, pid, edge, api_key)
                    break
                except ApiError as e:
                    err = e
                    http.sleep(PAUSE_S)
            if got is None:
                counts["failed"] += 1
                failed.add(w)
                log(f"  {edge} of {w}: {err}")
                continue
            counts[edge] += len(got)
            for p in got:
                rec = parse_paper(p)
                if rec["source_id"]:
                    found.setdefault(rec["source_id"], rec)
            http.sleep(PAUSE_S)
    counts["by_title"] = sum(1 for v in titled.values() if v)
    counts["no_identifier"] = sum(1 for v in titled.values() if not v)
    counts["failed_works"] = len(failed)
    return _join(store, run_id, protocol, tree, string, found, counts, "Semantic Scholar")


def _join(store: Store, run_id: int, protocol: dict, tree, string: str, found: dict[str, dict], counts: dict,
          via: str) -> dict:
    """The mined records that match the string join the run: retrievals, the sheet for new ones, the source row."""
    matched = [r for r in found.values()
               if query.matches(tree, f"{r.get('title') or ''} {r.get('abstract') or ''}")]
    before = set(sheet_ids(store, run_id))
    pooled = {r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ?", (run_id,))}
    ids = []
    for rank, rec in enumerate(matched, 1):
        w = store.upsert_work(rec)
        store.add_retrieval(run_id, w, SOURCE, rec["source_id"], rank)
        ids.append(w)
    new = sorted(set(ids) - pooled)
    store.add_sample(run_id, [w for w in new if w not in before], SAMPLE_PURPOSE)
    # matched source records -> distinct records in the store (two can merge into one) -> already there, or new
    counts.update(unique=len(found), matched=len(matched), matched_works=len(set(ids)),
                  already=len(set(ids) & pooled), new=len(new), new_ids=new,
                  date=now()[:10], broadened=bool((protocol.get("mining") or {}).get("query")), query=string)
    row = store.db.execute("SELECT notes FROM run_sources WHERE run_id = ? AND source = ?", (run_id, SOURCE)).fetchone()
    old = json.loads(row["notes"] or "{}") if row else {}
    passes = old.get("passes") or ([{k: v for k, v in old.items() if k != "query"}] if old else [])
    counts["via"] = via
    passes.append({k: v for k, v in counts.items() if k != "query"})
    store.set_run_source(run_id, SOURCE, "references of excluded reviews and core studies, and citations of core "
                         f"studies ({via}), matched against the search string",
                         counts["references"] + counts["citations"], len(matched),
                         "partial" if counts["failed"] else "ok", notes=json.dumps({**counts, "passes": passes}))
    return counts
