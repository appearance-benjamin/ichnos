"""ERIC, the Education Resources Information Center of the US Institute of Education Sciences.

The main database of education research: journal articles, reports, theses and other grey literature.
Free, no key: https://api.ies.ed.gov/eric/. Every term is limited to the title and the abstract (ERIC's
`description`). ERIC gives no DOI field; the DOI is read from the record's link when that is a doi.org
link, and records whose full text ERIC holds get its PDF as an open-access copy.
"""

from __future__ import annotations

import re

from ..http import Http
from ..store import normalize_doi
from . import SearchResult

NAME = "eric"
URL = "https://api.ies.ed.gov/eric/"
PAGE = 2000
FIELDS = ("id,title,author,description,source,sourceid,publicationdateyear,publicationtype,peerreviewed,url,"
          "e_fulltextauth,language")
TYPES = {"Journal Articles": "journal-article", "Dissertations/Theses": "dissertation", "Books": "book",
         "Collected Works": "book", "Speeches/Meeting Papers": "proceedings-article"}


def search(http: Http, query: str, cap: int) -> SearchResult:
    records, total, start = [], None, 0
    while len(records) < cap:
        data = http.get_json(NAME, URL, {"search": query, "format": "json", "rows": min(PAGE, cap - len(records)),
                                         "start": start, "fields": FIELDS})
        resp = data.get("response") or {}
        total = resp.get("numFound")
        page = resp.get("docs") or []
        records.extend(parse_doc(d) for d in page)
        start += len(page)
        if not page or start >= (total or 0):
            break
    return SearchResult(NAME, query, total, records[:cap])


def parse_doc(d: dict) -> dict:
    m = re.search(r"10\.\d{4,9}/\S+", d.get("url") or "")
    kinds = d.get("publicationtype") or []
    kind = next((TYPES[k] for t in kinds for k in TYPES if t.startswith(k)), kinds[0] if kinds else None)
    year = d.get("publicationdateyear")
    return {
        "source_id": d.get("id"),
        "doi": normalize_doi(m.group(0)) if m else None,
        "title": _clean(d.get("title")),
        "abstract": _clean(d.get("description")),
        "year": int(year) if str(year or "").isdigit() else None,
        "venue": d.get("source") or None,
        "type": kind,
        "language": "; ".join(d.get("language") or []) or None,
        "authors": [_first_last(a) for a in d.get("author") or [] if a],
        "oa_url": f"https://files.eric.ed.gov/fulltext/{d['id']}.pdf" if d.get("e_fulltextauth") == 1 and d.get("id")
        else None,
    }


def _first_last(name: str) -> str:
    """ERIC writes "Last, First"; the other sources give "First Last"."""
    if "," in name:
        last, first = name.split(",", 1)
        return f"{first.strip()} {last.strip()}".strip()
    return name.strip()


def _clean(text: str | None) -> str | None:
    return " ".join(text.split()) or None if text else None
