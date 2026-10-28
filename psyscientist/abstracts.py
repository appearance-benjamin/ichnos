"""Abstracts for retained records that have only a title, looked up by identifier.

Some sources index a record without its abstract (an ERIC or repository record, a conference abstract) while
another source has it. For each retained record whose abstract is missing, the sources are asked in turn by
DOI or PubMed ID (Semantic Scholar and OpenAlex also by their own ids): PubMed, Europe PMC, Crossref,
Semantic Scholar, OpenAlex, then CORE (repository copies, also by title). The first abstract found is kept.
Every request is logged like any other call, and each record's outcome, with the source and the call that
supplied the abstract, is kept in `abstract_fills`, so the Method can say where each abstract came from.

A record whose lookup hit an error at some source (a 429, a timeout) and found nothing is not settled: the next
run asks again, without --refresh. Run resolve.resolve first, so records found without an identifier get theirs.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter

from .http import ApiError, Http
from .sources import core, europepmc, pubmed
from .sources.openalex import abstract_from_index
from .store import Store, normalize_doi, now, title_key

MIN_LENGTH = 50          # what the tables count as having an abstract
SOURCES = ("pubmed", "europepmc", "crossref", "semantic_scholar", "openalex", "core")
NAMES = {"pubmed": "PubMed", "europepmc": "Europe PMC", "crossref": "Crossref", "semantic_scholar": "Semantic Scholar",
         "openalex": "OpenAlex", "core": "CORE"}
S2_PACE_NO_KEY = 1.1     # seconds between Semantic Scholar requests without a key (its public limit)
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

SCHEMA = """
CREATE TABLE IF NOT EXISTS abstract_fills (
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    run_id      INTEGER REFERENCES runs(run_id),
    status      TEXT NOT NULL,                  -- found, not_found
    source      TEXT,                           -- the source that supplied it
    tried       TEXT,                           -- JSON: the sources asked, in order
    call_id     INTEGER REFERENCES calls(call_id),
    length      INTEGER,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (work_id, run_id)
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def has_abstract(text: str | None) -> bool:
    return len((text or "").strip()) > MIN_LENGTH


def missing(store: Store, run_id: int, work_ids=None) -> list[int]:
    """Retained records (or the given ones) without an abstract."""
    if work_ids is None:
        from .agents import kept_ids
        work_ids = kept_ids(store, run_id)
    ids = list(work_ids)
    if not ids:
        return []
    rows = store.db.execute(f"SELECT work_id, abstract FROM works WHERE work_id IN ({','.join('?' * len(ids))})",
                            tuple(ids)).fetchall()
    return sorted(r["work_id"] for r in rows if not has_abstract(r["abstract"]))


def clean(text: str | None, title: str | None = None) -> str | None:
    """Plain text from JATS or HTML markup; None for a stub, or a title given as abstract."""
    if not text:
        return None
    text = re.sub(r"<jats:title>[^<]{0,40}</jats:title>", " ", text)       # Crossref's "Abstract" heading
    text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    text = " ".join(text.split())
    text = re.sub(r"^(abstract|summary)[\s.:]+", "", text, flags=re.I)
    if not has_abstract(text) or re.match(r"(no abstract|abstract not available)", text, re.I):
        return None
    if title and title_key(text) == title_key(title):
        return None
    # a citation given as abstract: the title in quotes with the journal, volume and pages
    if title and (title_key(title) or "") in (title_key(text) or "") and len(text) < len(title) + 150:
        return None
    if len(text) < 200 and re.search(r"\b\d+\s*\(\d+\)\s*,\s*(pp?\.\s*)?\d+", text):
        return None
    return text


def same_record(work, doi: str | None, title: str | None) -> bool:
    """Is a source's record the work itself? The same DOI, or the same title (one may extend the other). A search by
    DOI can fall back to loose matching and return an unrelated paper."""
    if doi and work["doi"]:
        return normalize_doi(doi) == normalize_doi(work["doi"])
    a, b = title_key(title) or "", title_key(work["title"]) or ""
    return bool(a and b) and (a == b or (min(len(a), len(b)) >= 30 and (a.startswith(b) or b.startswith(a))))


def _last_call(store: Store) -> int | None:
    row = store.db.execute("SELECT max(call_id) FROM calls").fetchone()
    return row[0] if row else None


def lookup(http: Http, work, source: str, contact_email: str | None = None,
           openalex_key: str | None = None, core_key: str | None = None) -> str | None:
    """One source's abstract for the work, or None (no identifier it knows, not found, or none indexed)."""
    doi, pmid = work["doi"], work["pmid"]
    if source == "core":
        hit = core.find(http, work, core_key)
        return (hit or {}).get("abstract")
    if source == "pubmed":
        common = {"db": "pubmed", "tool": "psyscientist", **({"email": contact_email} if contact_email else {})}
        known = bool(pmid)
        if not pmid and doi:
            found = http.get_json("pubmed", EUTILS + "esearch.fcgi", {**common, "term": f"{doi}[doi]",
                                                                      "retmode": "json"})
            ids = (found.get("esearchresult") or {}).get("idlist") or []
            pmid = ids[0] if len(ids) == 1 else None
            http.sleep(0.34)
        if not pmid:
            return None
        arts = pubmed.parse_articles(http.get("pubmed", EUTILS + "efetch.fcgi",
                                              {**common, "id": pmid, "retmode": "xml"}))
        return next((a.get("abstract") for a in arts if a.get("abstract")
                     and (known or same_record(work, a.get("doi"), a.get("title")))), None)
    if source == "europepmc":
        if not (doi or pmid):
            return None
        q = f'DOI:"{doi}"' if doi else f"EXT_ID:{pmid} AND SRC:MED"
        data = http.get_json("europepmc", europepmc.URL, {"query": q, "format": "json", "resultType": "core",
                                                          **({"email": contact_email} if contact_email else {})})
        for r in (data.get("resultList") or {}).get("result") or []:
            if r.get("abstractText") and (str(r.get("pmid")) == str(pmid) if pmid and not doi else
                                          same_record(work, r.get("doi"), r.get("title"))):
                return r["abstractText"]
        return None
    if source == "crossref":
        if not doi:
            return None
        try:
            return (http.get_json("crossref", f"https://api.crossref.org/works/{doi}", {})["message"]
                    .get("abstract"))
        except ApiError as err:
            if err.status == 404:
                return None
            raise
    if source == "semantic_scholar":
        pid = work["s2_id"] or (f"DOI:{doi}" if doi else f"PMID:{pmid}" if pmid else None)
        if not pid:
            return None
        try:
            return http.get_json("semantic_scholar", f"https://api.semanticscholar.org/graph/v1/paper/{pid}",
                                 {"fields": "title,abstract"}).get("abstract")
        except ApiError as err:
            if err.status == 404:
                return None
            raise
    if source == "openalex":
        oid = work["openalex_id"] or (f"doi:{doi}" if doi else f"pmid:{pmid}" if pmid else None)
        if not oid:
            return None
        params = {"select": "id,abstract_inverted_index", **({"api_key": openalex_key} if openalex_key else {}),
                  **({"mailto": contact_email} if contact_email else {})}
        try:
            data = http.get_json("openalex", f"https://api.openalex.org/works/{oid}", params)
        except ApiError as err:
            if err.status == 404:
                return None
            raise
        return abstract_from_index(data.get("abstract_inverted_index"))
    raise ValueError(f"unknown source {source}")


def settled(row) -> bool:
    """A lookup that need not be repeated: one that found an abstract, or that every source answered."""
    return row is not None and (row["status"] == "found" or "(error)" not in (row["tried"] or ""))


def fill(store: Store, run_id: int, work_ids=None, http: Http | None = None, contact_email: str | None = None,
         openalex_key: str | None = None, refresh: bool = False, log=print, s2_key: str | None = None,
         core_key: str | None = None) -> Counter:
    """Look up an abstract for each retained record that has none; store the first one found."""
    ensure(store)
    if http is None:
        http = Http(store, run_id, max_retries=4)
        if not s2_key:
            http.pace("semantic_scholar", S2_PACE_NO_KEY)
        http.breaker("semantic_scholar")
        core.configure(http, core_key)
    counts = Counter()
    for w in missing(store, run_id, work_ids):
        old = store.db.execute("SELECT status, tried FROM abstract_fills WHERE work_id = ? AND run_id = ?",
                               (w, run_id)).fetchone()
        if not refresh and settled(old):
            counts["already_tried"] += 1
            continue
        work = store.db.execute("SELECT * FROM works WHERE work_id = ?", (w,)).fetchone()
        text, source, tried, call = None, None, [], None
        for s in SOURCES:
            try:
                raw = lookup(http, work, s, contact_email, openalex_key, core_key)
            except (ApiError, ValueError) as err:
                log(f"  {w} {s}: {err}")
                tried.append(f"{s} (error)")
                continue
            tried.append(s)
            text = clean(raw, work["title"])
            if text:
                source, call = s, _last_call(store)
                break
        with store.db:
            if text:
                store.db.execute("UPDATE works SET abstract = ?, updated_at = ? WHERE work_id = ?", (text, now(), w))
            store.db.execute("INSERT OR REPLACE INTO abstract_fills (work_id, run_id, status, source, tried, call_id, "
                             "length, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (w, run_id, "found" if text else "not_found", source, json.dumps(tried), call,
                              len(text) if text else None, now()))
        retry = not text and any(t.endswith("(error)") for t in tried)
        counts[source or ("not_found (an error at a source: tried again next time)" if retry else "not_found")] += 1
    return counts


def summary(store: Store, run_id: int) -> dict:
    """What `fill` did for a run: records tried, found by source, the date, and the ids that gained an abstract."""
    ensure(store)
    rows = store.db.execute("SELECT * FROM abstract_fills WHERE run_id = ?", (run_id,)).fetchall()
    found = [r for r in rows if r["status"] == "found"]
    return {"tried": len(rows), "found": len(found), "by_source": dict(Counter(r["source"] for r in found)),
            "date": max((r["fetched_at"][:10] for r in rows), default=None),
            "ids": sorted(r["work_id"] for r in found)}
