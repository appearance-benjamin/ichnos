"""CORE (core.ac.uk), the aggregator of open repository copies: a record's abstract and full text.

CORE harvests university and subject repositories (accepted manuscripts, working papers, theses) and keeps the
text it extracted from each PDF. It is asked by DOI, else by title; a hit counts only if it is the same work
(the same DOI, or the same title with a year within one). Without a key CORE allows about 10 requests a minute,
so the requests are spaced; CORE_API_KEY (free) lifts that.
"""

from __future__ import annotations

import re

from ..http import Http
from ..store import normalize_doi, title_key

URL = "https://api.core.ac.uk/v3/search/works"
PACE_NO_KEY = 6.5        # seconds between requests: 10 a minute, CORE's limit without a key
PACE_KEY = 2.1           # with a key: "five single requests per 10 seconds"
MIN_TITLE_WORDS = 5      # a shorter title (a journal's name, "Fertility") matches too many works


def configure(http: Http, api_key: str | None) -> None:
    http.pace("core", PACE_KEY if api_key else PACE_NO_KEY)
    http.breaker("core")


def same_work(work, hit: dict) -> bool:
    doi = normalize_doi(work["doi"])
    if doi and normalize_doi(hit.get("doi")) == doi:
        return True
    key = title_key(work["title"])
    if not key or len(key.split()) < MIN_TITLE_WORDS or title_key(hit.get("title")) != key:
        return False
    year, other = work["year"], hit.get("yearPublished")
    return not (year and other) or abs(int(year) - int(other)) <= 1


def find(http: Http, work, api_key: str | None = None) -> dict | None:
    """The CORE record of the work, with its `abstract`, `fullText` and `downloadUrl` where CORE has them."""
    queries = []
    if work["doi"]:
        queries.append(f'doi:"{work["doi"]}"')
    words = re.sub(r"[^\w\s]+", " ", work["title"] or "").split()
    if len(words) >= MIN_TITLE_WORDS:
        queries.append(f'title:"{" ".join(words)}"')
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else None
    for q in queries:
        data = http.get_json("core", URL, {"q": q, "limit": 5}, headers)
        for hit in data.get("results") or []:
            if same_work(work, hit):
                return hit
    return None
