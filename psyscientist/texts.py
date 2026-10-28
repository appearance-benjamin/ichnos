"""Every text a retained record can have, in one pass that can be run again: identifiers, abstracts, full texts.

1. resolve: records found without a DOI or OpenAlex id get theirs from OpenAlex, matched by title.
2. abstracts: records without an abstract are looked up at six sources (CORE last, also by title).
3. fulltext: open copies are located, fetched and parsed; CORE and the Internet Archive are the fallbacks.

Each step skips what is settled and asks again where a service stopped it (a 429, a timeout), so a second run
picks up where the first was stopped. What is still missing afterwards is behind a paywall: the reader's library
list (`python -m psyscientist library <run>`) names it, with links.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from . import abstracts, resolve
from .agents import kept_ids
from .config import ROOT, Settings
from .fulltext import retrieve
from .store import Store


def run(store: Store, run_id: int, settings: Settings, corpus: str, work_ids: list[int] | None = None,
        root: Path | None = None, refresh: bool = False, log=print) -> dict:
    ids = work_ids if work_ids is not None else kept_ids(store, run_id)
    log(f"1/3 identifiers for records without a DOI or OpenAlex id ({len(resolve.needs(store, run_id, ids))})")
    found = resolve.resolve(store, run_id, ids, openalex_key=settings.openalex_api_key,
                            contact_email=settings.contact_email, refresh=refresh, log=log)
    log(f"2/3 abstracts for records without one ({len(abstracts.missing(store, run_id, ids))})")
    filled = abstracts.fill(store, run_id, ids, contact_email=settings.contact_email,
                            openalex_key=settings.openalex_api_key, refresh=refresh, log=log,
                            s2_key=settings.s2_api_key, core_key=settings.core_api_key)
    log(f"3/3 full texts ({len(ids)} records)")
    full = retrieve(store, ids, settings, corpus, run_id, root=root or ROOT, refresh=refresh, log=log)
    return {"identifiers": dict(found), "abstracts": dict(filled), "fulltexts": full,
            "coverage": coverage(store, ids), "duplicates": resolve.duplicates(store, run_id)}


def coverage(store: Store, ids: list[int]) -> dict:
    """How many of the records have an abstract and a parsed full text now."""
    if not ids:
        return {"records": 0, "abstract": 0, "fulltext": 0, "neither": 0}
    marks = ",".join("?" * len(ids))
    rows = store.db.execute(f"SELECT w.work_id, w.abstract, f.status FROM works w LEFT JOIN fulltexts f "
                            f"ON f.work_id = w.work_id WHERE w.work_id IN ({marks})", ids).fetchall()
    has_abs = Counter(abstracts.has_abstract(r["abstract"]) for r in rows)
    parsed = Counter(r["status"] == "parsed" for r in rows)
    neither = sum(1 for r in rows if not abstracts.has_abstract(r["abstract"]) and r["status"] != "parsed")
    return {"records": len(rows), "abstract": has_abs[True], "fulltext": parsed[True], "neither": neither}
