"""Semantic Scholar bulk paper search (title and abstract, boolean syntax + | - " * ).

Without a key, requests share one public rate limit and often get HTTP 429;
the Http client backs off and retries. A free key: semanticscholar.org/product/api
"""

from __future__ import annotations

from ..http import Http
from ..store import normalize_doi
from . import SearchResult

NAME = "semantic_scholar"
URL = "https://api.semanticscholar.org/graph/v1/paper/search/bulk"
FIELDS = "paperId,externalIds,title,abstract,year,venue,publicationTypes,authors,openAccessPdf"
PAUSE_S = 1.1   # keyed accounts are limited to one request per second


def search(http: Http, query: str, cap: int, api_key: str | None = None) -> SearchResult:
    params = {"query": query, "fields": FIELDS}
    headers = {"x-api-key": api_key} if api_key else None

    records, total = [], None
    while len(records) < cap:
        data = http.get_json(NAME, URL, params, headers)
        total = data.get("total")
        records.extend(parse_paper(p) for p in data.get("data") or [])
        token = data.get("token")
        if not token or not data.get("data"):
            break
        params["token"] = token
        http.sleep(PAUSE_S)
    return SearchResult(NAME, query, total, records[:cap])


def parse_paper(p: dict) -> dict:
    ext = p.get("externalIds") or {}
    pdf = p.get("openAccessPdf") or {}
    types = p.get("publicationTypes") or []
    return {
        "source_id": p.get("paperId"),
        "s2_id": p.get("paperId"),
        "doi": normalize_doi(ext.get("DOI")),
        "pmid": ext.get("PubMed"),
        "title": p.get("title"),
        "abstract": p.get("abstract"),
        "year": p.get("year"),
        "venue": p.get("venue") or None,
        "type": ";".join(types) or None,
        "language": None,
        "authors": [a["name"] for a in p.get("authors") or [] if a.get("name")],
        "oa_url": pdf.get("url") or None,
    }
