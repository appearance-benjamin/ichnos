"""OpenAlex works search, restricted to titles and abstracts.

Uses the exact (unstemmed) title-and-abstract filter, because OpenAlex rejects
truncation such as retire* in its default stemmed search. The plain `search`
parameter also covers full text, which turned a 170-record corpus A string
into 22,648 hits on 2026-10-16.
"""

from __future__ import annotations

from ..http import Http
from ..store import normalize_doi
from . import SearchResult

NAME = "openalex"
URL = "https://api.openalex.org/works"
PER_PAGE = 200
SELECT = ",".join([
    "id", "doi", "ids", "title", "publication_year", "type", "language",
    "primary_location", "authorships", "abstract_inverted_index", "open_access",
])


def search(http: Http, query: str, cap: int, api_key: str | None = None,
           contact_email: str | None = None) -> SearchResult:
    if "," in query:
        raise ValueError("OpenAlex filters are comma-separated, so the query itself cannot contain a comma")
    params = {
        "filter": f"title_and_abstract.search.exact:{query}",
        "select": SELECT,
        "per_page": PER_PAGE,
        "cursor": "*",
    }
    if api_key:
        params["api_key"] = api_key
    if contact_email:
        params["mailto"] = contact_email

    records, total = [], None
    while len(records) < cap:
        data = http.get_json(NAME, URL, params)
        total = data["meta"]["count"]
        records.extend(parse_work(w) for w in data.get("results", []))
        cursor = data["meta"].get("next_cursor")
        if not cursor or not data.get("results"):
            break
        params["cursor"] = cursor
    return SearchResult(NAME, query, total, records[:cap])


def parse_work(w: dict) -> dict:
    ids = w.get("ids") or {}
    location = w.get("primary_location") or {}
    venue = (location.get("source") or {}).get("display_name")
    oa = w.get("open_access") or {}
    pmid = ids.get("pmid")
    return {
        "source_id": _short(w.get("id")),
        "openalex_id": _short(w.get("id")),
        "doi": normalize_doi(w.get("doi")),
        "pmid": pmid.rstrip("/").rsplit("/", 1)[-1] if pmid else None,
        "title": w.get("title"),
        "abstract": abstract_from_index(w.get("abstract_inverted_index")),
        "year": w.get("publication_year"),
        "venue": venue,
        "type": w.get("type"),
        "language": w.get("language"),
        "authors": [a["author"]["display_name"] for a in w.get("authorships") or []
                    if (a.get("author") or {}).get("display_name")],
        "oa_url": oa.get("oa_url"),
    }


def abstract_from_index(index: dict | None) -> str | None:
    """OpenAlex ships abstracts as {word: [positions]}; rebuild the text."""
    if not index:
        return None
    slots = {}
    for word, positions in index.items():
        for p in positions:
            slots[p] = word
    return " ".join(slots[p] for p in sorted(slots)) or None


def _short(openalex_url: str | None) -> str | None:
    return openalex_url.rsplit("/", 1)[-1] if openalex_url else None
