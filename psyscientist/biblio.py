"""Volume, issue and pages for the references, from Crossref by DOI.

The search sources give titles, venues and years but rarely volume and pages. Crossref has them
for most DOIs; every lookup is logged like any other call. No contact email is sent.
"""

from __future__ import annotations

from collections import Counter

from .http import ApiError, Http
from .store import Store, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS biblio (
    work_id     INTEGER PRIMARY KEY REFERENCES works(work_id),
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,                  -- found, not_found, error
    container   TEXT,
    volume      TEXT,
    issue       TEXT,
    pages       TEXT,
    publisher   TEXT,
    kind        TEXT,                           -- Crossref type
    fetched_at  TEXT NOT NULL
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def get(store: Store, work_id: int):
    ensure(store)
    return store.db.execute("SELECT * FROM biblio WHERE work_id = ? AND status = 'found'", (work_id,)).fetchone()


def fetch(store: Store, run_id: int, work_ids, http: Http | None = None, refresh: bool = False) -> Counter:
    ensure(store)
    http = http or Http(store, run_id, max_retries=3)
    counts = Counter()
    ids = list(work_ids)
    rows = store.db.execute(f"SELECT work_id, doi FROM works WHERE doi IS NOT NULL AND work_id IN "
                            f"({','.join('?' * len(ids))})", tuple(ids)).fetchall() if ids else []
    for r in rows:
        if not refresh and store.db.execute("SELECT 1 FROM biblio WHERE work_id = ? AND status != 'error'",
                                            (r["work_id"],)).fetchone():
            counts["cached"] += 1
            continue
        try:
            m = http.get_json("crossref", f"https://api.crossref.org/works/{r['doi']}", {})["message"]
            pages = (m.get("page") or "").replace("-", "–") or (f"Article {m['article-number']}"
                                                                if m.get("article-number") else None)
            values = ("found", (m.get("container-title") or [None])[0], m.get("volume"), m.get("issue"), pages,
                      m.get("publisher"), m.get("type"))
        except ApiError as err:
            values = ("not_found" if err.status == 404 else "error", None, None, None, None, None, None)
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO biblio (work_id, source, status, container, volume, issue, "
                             "pages, publisher, kind, fetched_at) VALUES (?, 'crossref', ?, ?, ?, ?, ?, ?, ?, ?)",
                             (r["work_id"], *values, now()))
        counts[values[0]] += 1
    counts["no_doi"] = len(ids) - len(rows)
    return counts
