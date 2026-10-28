"""Europe PMC REST search: PubMed plus PMC-only records, preprints and other sources.

Every term is restricted to TITLE_ABS. Without it Europe PMC also searches full
text: 4,934 hits for the corpus A string against 85 in titles and abstracts
(2026-10-17). No key needed.
"""

from __future__ import annotations

import html
import re

from ..http import Http
from ..store import normalize_doi
from . import SearchResult

NAME = "europepmc"
URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PAGE = 1000


def search(http: Http, query: str, cap: int, contact_email: str | None = None) -> SearchResult:
    params = {"query": query, "format": "json", "resultType": "core", "pageSize": PAGE, "cursorMark": "*"}
    if contact_email:
        params["email"] = contact_email
    records, total = [], None
    while len(records) < cap:
        data = http.get_json(NAME, URL, params)
        total = data.get("hitCount")
        page = (data.get("resultList") or {}).get("result") or []
        records.extend(parse_result(r) for r in page)
        cursor = data.get("nextCursorMark")
        if not page or not cursor or cursor == params["cursorMark"]:
            break
        params["cursorMark"] = cursor
    sources = {}
    for r in records:
        sources[r["_epmc_source"]] = sources.get(r["_epmc_source"], 0) + 1
    notes = "Europe PMC sources: " + ", ".join(f"{k} {v}" for k, v in sorted(sources.items())) if sources else None
    return SearchResult(NAME, query, total, records[:cap], notes)


def parse_result(r: dict) -> dict:
    journal = ((r.get("journalInfo") or {}).get("journal") or {}).get("title")
    book = (r.get("bookOrReportDetails") or {}).get("publisher")
    pmcid = r.get("pmcid")
    oa = f"https://europepmc.org/article/PMC/{pmcid}" if pmcid else None
    if not oa:
        for u in ((r.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
            if u.get("availabilityCode") in ("OA", "F"):
                oa = u.get("url")
                break
    authors = [a.get("fullName") for a in ((r.get("authorList") or {}).get("author") or []) if a.get("fullName")]
    year = r.get("pubYear")
    return {
        "source_id": f"{r.get('source')}:{r.get('id')}",
        "_epmc_source": r.get("source") or "?",
        "pmid": r.get("pmid"),
        "doi": normalize_doi(r.get("doi")),
        "title": _clean(r.get("title")),
        "abstract": _clean(r.get("abstractText")),
        "year": int(year) if year and str(year).isdigit() else None,
        "venue": journal or book or (r.get("bookOrReportDetails") or {}).get("comment") or None,
        "type": ";".join((r.get("pubTypeList") or {}).get("pubType") or []) or None,
        "language": r.get("language"),
        "authors": authors,
        "oa_url": oa,
    }


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text)).split()) or None
