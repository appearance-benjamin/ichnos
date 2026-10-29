"""PubMed through NCBI E-utilities: esearch for the IDs, efetch for the records.

Every term is restricted to [tiab] (title and abstract). Without an API key
NCBI allows 3 requests per second; a free key (NCBI account settings) raises
that to 10.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from ..http import Http
from ..store import normalize_doi
from . import SearchResult

NAME = "pubmed"
BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
FETCH_BATCH = 200


def search(http: Http, query: str, cap: int, api_key: str | None = None,
           contact_email: str | None = None) -> SearchResult:
    common = {"db": "pubmed", "tool": "ichnos"}
    if api_key:
        common["api_key"] = api_key
    if contact_email:
        common["email"] = contact_email
    pause = 0.11 if api_key else 0.34

    found = http.get_json(NAME, BASE + "esearch.fcgi",
                          {**common, "term": query, "retmode": "json", "usehistory": "y", "retmax": 0})
    result = found["esearchresult"]
    if result.get("errorlist") and any(result["errorlist"].values()):
        raise ValueError(f"PubMed rejected the query: {result['errorlist']}")
    total = int(result["count"])
    notes = []
    if result.get("warninglist") and any(result["warninglist"].values()):
        notes.append(f"warnings: {result['warninglist']}")
    if result.get("querytranslation"):
        notes.append(f"PubMed's translation: {result['querytranslation']}")

    records = []
    for start in range(0, min(total, cap), FETCH_BATCH):
        http.sleep(pause)
        xml = http.get(NAME, BASE + "efetch.fcgi", {
            **common, "WebEnv": result["webenv"], "query_key": result["querykey"],
            "retstart": start, "retmax": min(FETCH_BATCH, cap - start), "retmode": "xml",
        })
        records.extend(parse_articles(xml))
    return SearchResult(NAME, query, total, records[:cap], "\n".join(notes) or None)


def parse_articles(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    out = []
    for art in root:
        if art.tag == "PubmedArticle":
            out.append(_article(art))
        elif art.tag == "PubmedBookArticle":
            out.append(_book(art))
    return out


def _article(art: ET.Element) -> dict:
    cit = art.find("MedlineCitation")
    a = cit.find("Article")
    journal = a.find("Journal")
    ids = {i.get("IdType"): _text(i) for i in art.findall("PubmedData/ArticleIdList/ArticleId")}
    doi = ids.get("doi") or next((_text(e) for e in a.findall("ELocationID") if e.get("EIdType") == "doi"), None)
    pmid = _text(cit.find("PMID"))
    pmc = ids.get("pmc")
    return {
        "source_id": pmid,
        "pmid": pmid,
        "doi": normalize_doi(doi),
        "title": _text(a.find("ArticleTitle")),
        "abstract": _abstract(a.find("Abstract")),
        "year": _year(journal.find("JournalIssue/PubDate") if journal is not None else None, a.find("ArticleDate")),
        "venue": _text(journal.find("Title")) if journal is not None else None,
        "type": ";".join(_text(t) for t in a.findall("PublicationTypeList/PublicationType")) or None,
        "language": _text(a.find("Language")),
        "authors": _authors(a.find("AuthorList")),
        "oa_url": f"https://pmc.ncbi.nlm.nih.gov/articles/{pmc}/" if pmc else None,
    }


def _book(art: ET.Element) -> dict:
    doc = art.find("BookDocument")
    pmid = _text(doc.find("PMID"))
    title = _text(doc.find("ArticleTitle")) or _text(doc.find("Book/BookTitle"))
    return {
        "source_id": pmid, "pmid": pmid, "doi": None, "title": title,
        "abstract": _abstract(doc.find("Abstract")),
        "year": _year(doc.find("Book/PubDate"), None),
        "venue": _text(doc.find("Book/Publisher/PublisherName")), "type": "Book", "language": _text(doc.find("Language")),
        "authors": _authors(doc.find("AuthorList")), "oa_url": None,
    }


def _abstract(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    parts = []
    for t in el.findall("AbstractText"):
        text = _text(t)
        if text:
            parts.append(f"{t.get('Label')}: {text}" if t.get("Label") else text)
    return " ".join(parts) or None


def _authors(el: ET.Element | None) -> list[str]:
    if el is None:
        return []
    names = []
    for a in el.findall("Author"):
        if a.find("CollectiveName") is not None:
            names.append(_text(a.find("CollectiveName")))
        else:
            names.append(" ".join(filter(None, [_text(a.find("ForeName")) or _text(a.find("Initials")),
                                                _text(a.find("LastName"))])))
    return [n for n in names if n]


def _year(pubdate: ET.Element | None, article_date: ET.Element | None) -> int | None:
    for el in (pubdate, article_date):
        if el is None:
            continue
        year = _text(el.find("Year"))
        if not year:
            m = re.search(r"\d{4}", _text(el.find("MedlineDate")) or "")
            year = m.group(0) if m else None
        if year and year.isdigit():
            return int(year)
    return None


def _text(el: ET.Element | None) -> str | None:
    if el is None:
        return None
    text = " ".join("".join(el.itertext()).split())
    return text or None
