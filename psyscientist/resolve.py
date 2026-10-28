"""Identifiers for retained records found without one: the OpenAlex record of a work known only by its title.

Records from Semantic Scholar, repositories and dissertation indexes often come without a DOI or an OpenAlex id,
and then no source can be asked for their abstract or an open copy. For each such retained record, OpenAlex is
searched by title. A hit counts only if its title is the same after normalisation (title_key), the title has at
least five words (a journal's name or "Fertility" matches too much) and the years are within one of each other.

The OpenAlex id, and the DOI if OpenAlex has one, are added to the record. An id that another record already
holds marks the two records as duplicates instead: that is recorded (`duplicate_of`) and nothing is merged.
Each record's outcome is kept in `id_resolutions`; a lookup that failed on an error is tried again next time.
"""

from __future__ import annotations

import re
from collections import Counter

from .http import ApiError, Http
from .store import Store, normalize_doi, now, title_key

OPENALEX = "https://api.openalex.org/works"
MIN_TITLE_WORDS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS id_resolutions (
    work_id      INTEGER NOT NULL REFERENCES works(work_id),
    run_id       INTEGER REFERENCES runs(run_id),
    status       TEXT NOT NULL,                  -- found, not_found, duplicate, error
    openalex_id  TEXT,
    doi          TEXT,
    duplicate_of INTEGER REFERENCES works(work_id),
    error        TEXT,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (work_id, run_id)
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def needs(store: Store, run_id: int, work_ids=None) -> list[int]:
    """Retained records (or the given ones) with neither a DOI nor an OpenAlex id."""
    if work_ids is None:
        from .agents import kept_ids
        work_ids = kept_ids(store, run_id)
    ids = list(work_ids)
    if not ids:
        return []
    rows = store.db.execute(f"SELECT work_id FROM works WHERE work_id IN ({','.join('?' * len(ids))}) "
                            "AND doi IS NULL AND openalex_id IS NULL", ids).fetchall()
    return sorted(r["work_id"] for r in rows)


def matchable(work) -> bool:
    key = title_key(work["title"])
    return bool(key) and len(key.split()) >= MIN_TITLE_WORDS


def match(work, hits: list[dict]) -> dict | None:
    """The hit that is the same work: same title, years within one (the closest year first)."""
    if not matchable(work):
        return None
    key = title_key(work["title"])
    same = [h for h in hits if title_key(h.get("title")) == key]
    if work["year"]:
        same = [h for h in same if not h.get("publication_year") or abs(h["publication_year"] - work["year"]) <= 1]
        same.sort(key=lambda h: abs((h.get("publication_year") or work["year"]) - work["year"]))
    return same[0] if same else None


def search(http: Http, title: str, openalex_key: str | None = None, contact_email: str | None = None) -> list[dict]:
    # Commas separate filters, a colon starts a value and ? or * would be wildcards: all become spaces.
    text = " ".join(re.sub(r"[,:|?*]", " ", title).split())[:250]
    params = {"filter": f"title.search:{text}", "per_page": 10, "select": "id,doi,title,publication_year",
              **({"api_key": openalex_key} if openalex_key else {}),
              **({"mailto": contact_email} if contact_email else {})}
    return http.get_json("openalex_resolve", OPENALEX, params).get("results") or []


def resolve(store: Store, run_id: int, work_ids=None, http: Http | None = None, openalex_key: str | None = None,
            contact_email: str | None = None, refresh: bool = False, log=print) -> Counter:
    """Find and add the OpenAlex id (and DOI) of each retained record that has neither; returns the outcomes."""
    ensure(store)
    http = http or Http(store, run_id, max_retries=4)
    counts = Counter()
    for w in needs(store, run_id, work_ids):
        done = store.db.execute("SELECT status FROM id_resolutions WHERE work_id = ? AND run_id = ?",
                                (w, run_id)).fetchone()
        if done and done["status"] != "error" and not refresh:
            counts["already_tried"] += 1
            continue
        work = store.db.execute("SELECT * FROM works WHERE work_id = ?", (w,)).fetchone()
        status, oid, doi, dup, error = "not_found", None, None, None, None
        if matchable(work):                                      # a short title is not matched at all
            try:
                hit = match(work, search(http, work["title"], openalex_key, contact_email))
            except (ApiError, ValueError) as err:
                hit, status, error = None, "error", str(err)[:300]
                log(f"  #{w}: {err}")
            if hit:
                oid = hit["id"].rsplit("/", 1)[-1]
                doi = normalize_doi(hit.get("doi"))
                dup = _holder(store, w, oid, doi)
                status = "duplicate" if dup else "found"
        with store.db:
            if status == "found":
                # The abstract is left to abstracts.fill, which asks OpenAlex by this id and records where it came from.
                store.db.execute("UPDATE works SET openalex_id = ?, doi = COALESCE(doi, ?), updated_at = ? "
                                 "WHERE work_id = ?", (oid, doi, now(), w))
            store.db.execute("INSERT OR REPLACE INTO id_resolutions (work_id, run_id, status, openalex_id, doi, "
                             "duplicate_of, error, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (w, run_id, status, oid, doi, dup, error, now()))
        counts[status] += 1
    return counts


def _holder(store: Store, work_id: int, oid: str, doi: str | None) -> int | None:
    """Another record that already holds this OpenAlex id or DOI: the same work, recorded twice."""
    row = store.db.execute("SELECT work_id FROM works WHERE work_id != ? AND (openalex_id = ? OR (doi IS NOT NULL "
                           "AND doi = ?))", (work_id, oid, doi)).fetchone()
    return row["work_id"] if row else None


def duplicates(store: Store, run_id: int) -> list[tuple[int, int]]:
    """(record, the record it duplicates) for every duplicate the title lookup found in the run."""
    ensure(store)
    return [(r["work_id"], r["duplicate_of"]) for r in store.db.execute(
        "SELECT work_id, duplicate_of FROM id_resolutions WHERE run_id = ? AND status = 'duplicate' "
        "ORDER BY work_id", (run_id,))]
