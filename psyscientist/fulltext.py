"""Open-access full texts for the kept studies: find a copy, fetch it, parse it into sections.

Only open-access copies are used; nothing goes through a university login.
In order of preference:

1. Europe PMC full-text XML (JATS) for articles in its open-access subset. It
   is already structured, so no PDF parsing is needed.
2. The PDF at Unpaywall's open-access locations. Unpaywall asks every caller
   for an email address, so this step runs only when CONTACT_EMAIL is set.
3. The PDF Semantic Scholar lists as the paper's open-access copy.
4. The PDF at OpenAlex's open-access locations (built on Unpaywall's data; no
   email needed).
5. The PDF links Crossref lists for an article under an open (Creative Commons)
   licence.
6. The open-access locations that name only a web page (a repository record, a
   journal's article page): the PDF that page names in its `citation_pdf_url`
   tag, the tag Google Scholar reads, else a link on the page that is plainly
   its PDF. A PDF link that returns such a page is followed the same way, once.

Only when none of these gives a text are two fallbacks asked:

7. CORE, the aggregator of repository copies (accepted manuscripts, working
   papers, theses), by DOI or title: its PDF, else the text it extracted.
8. The Internet Archive's copy of a free link that failed (gone, or refused):
   web.archive.org keeps snapshots of many open PDFs.

A copy found indirectly (a page's link, CORE, an archive) is kept only if its
first pages carry the record's DOI or nearly all of its title's words, so a
wrong file is never parsed as the study.

A site that answers with a bot check ("Just a moment...") is not worked
around: the copy is recorded as free but blocked, with its link, so the
reader can open it in a browser (library.py lists it as "Free copy").

PDFs are parsed with GROBID when it is running (GROBID_URL, default
http://localhost:8070), otherwise with pypdf and heading heuristics. Files stay
in fulltext/corpus_<X>/, outside version control: the licences of open-access
copies differ, so publish quotes, not files. The evidence store keeps source,
URL, licence, version, the file's SHA-256 and the parsed sections.
"""

from __future__ import annotations

import hashlib
import html
import io
import json
import re
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, Settings
from .http import ApiError, Http
from .sources import core
from .store import Store, normalize_doi, now, title_key

TEI = "{http://www.tei-c.org/ns/1.0}"
EPMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EPMC_XML = "https://www.ebi.ac.uk/europepmc/webservices/rest/{pmcid}/fullTextXML"
UNPAYWALL = "https://api.unpaywall.org/v2/{doi}"
OPENALEX = "https://api.openalex.org/works/{key}"
S2_PAPER = "https://api.semanticscholar.org/graph/v1/paper/{key}"
CROSSREF = "https://api.crossref.org/works/{doi}"
WAYBACK = "https://archive.org/wayback/available"
MAX_PDF = 40_000_000
MIN_TEXT = 3000          # characters: less is an abstract or a cover page, not a full text
ARCHIVE_TRIES = 3        # failed links looked up in the Internet Archive per record
TITLE_SHARE = 0.8        # of the title's words that must stand on the first pages of a copy found indirectly
DOI_TEXT = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.I)
# The pages sites show instead of the file when they take the caller for a bot (Cloudflare and others).
BOT_CHECK = re.compile(r"just a moment|attention required|are you a robot|captcha|verify you are human|"
                       r"cf-browser-verification|access denied|request unsuccessful", re.I)

METHOD_HEADS = re.compile(
    r"method|measure|instrument|material|participant|sample|procedure|design|data\b|dataset|analys|"
    r"statistic|variable|assessment|operationali|study population|setting|interview", re.I)
RESULT_HEADS = re.compile(r"result|finding|trajector|profile|class|subgroup|moderat", re.I)
DISCUSSION_HEADS = re.compile(r"discussion|conclusion|summary|implication|general discussion", re.I)
SKIP_HEADS = re.compile(r"reference|bibliograph|acknowledg|funding|conflict|declaration|author contribution|"
                        r"supplementary|appendix|data availability|ethic", re.I)


@dataclass
class Candidate:
    source: str          # europepmc, unpaywall, semantic_scholar, openalex, crossref, core, wayback
    kind: str            # jats, pdf, text (a source's own extracted text), or page (a web page that may name the PDF)
    url: str
    license: str | None
    version: str | None
    text: str | None = None      # kind "text": the text itself
    indirect: bool = False       # found by way of an aggregator or archive: kept only if it is the record's paper

KINDS = ("jats", "pdf", "text", "page")


# Finding a copy

def locate(http: Http, work, settings: Settings, errors: list[str] | None = None) -> list[Candidate]:
    """Every open-access copy the sources know of: JATS first, then PDFs, then pages that may name a PDF. A source
    that could not answer (a 429, a server error, no connection) is noted in `errors`, so the record is not taken
    for one without an open copy."""
    out: list[Candidate] = []
    doi, pmid = work["doi"], work["pmid"]
    if doi or pmid:
        q = " OR ".join(([f'DOI:"{doi}"'] if doi else []) + ([f"(EXT_ID:{pmid} AND SRC:MED)"] if pmid else []))
        try:
            data = http.get_json("europepmc", EPMC_SEARCH, {"query": q, "resultType": "core", "format": "json"})
            for r in (data.get("resultList") or {}).get("result") or []:
                if r.get("pmcid") and r.get("isOpenAccess") == "Y" and r.get("inEPMC") == "Y":
                    out.append(Candidate("europepmc", "jats", EPMC_XML.format(pmcid=r["pmcid"]),
                                         r.get("license"), "publishedVersion"))
                    break
        except ApiError as err:
            _note(errors, "europepmc", err)
    if doi and settings.contact_email:
        try:
            data = http.get_json("unpaywall", UNPAYWALL.format(doi=urllib.parse.quote(doi, safe="/")),
                                 {"email": settings.contact_email})
            locs = [data.get("best_oa_location")] + (data.get("oa_locations") or [])
            for loc in locs:
                if loc and (loc.get("url_for_pdf") or loc.get("url_for_landing_page")):
                    out.append(Candidate("unpaywall", "pdf" if loc.get("url_for_pdf") else "page",
                                         loc.get("url_for_pdf") or loc["url_for_landing_page"], loc.get("license"),
                                         loc.get("version")))
        except ApiError as err:
            _note(errors, "unpaywall", err)
    key = work["s2_id"] or (f"DOI:{doi}" if doi else f"PMID:{pmid}" if pmid else None)
    if key:
        try:
            data = http.get_json("semantic_scholar", S2_PAPER.format(key=key),
                                 {"fields": "openAccessPdf"},
                                 {"x-api-key": settings.s2_api_key} if settings.s2_api_key else None)
            pdf = data.get("openAccessPdf") or {}
            if pdf.get("url"):
                out.append(Candidate("semantic_scholar", "pdf", pdf["url"], pdf.get("license"), None))
        except ApiError as err:
            _note(errors, "semantic_scholar", err)
    key = f"doi:{doi}" if doi else work["openalex_id"]
    if key:
        params = {"select": "best_oa_location,locations"}
        if settings.openalex_api_key:
            params["api_key"] = settings.openalex_api_key
        try:
            data = http.get_json("openalex", OPENALEX.format(key=urllib.parse.quote(key, safe=":/")), params)
            locs = [data.get("best_oa_location")] + (data.get("locations") or [])
            for loc in locs:
                if loc and loc.get("is_oa") and (loc.get("pdf_url") or loc.get("landing_page_url")):
                    out.append(Candidate("openalex", "pdf" if loc.get("pdf_url") else "page",
                                         loc.get("pdf_url") or loc["landing_page_url"], loc.get("license"),
                                         loc.get("version")))
        except ApiError as err:
            _note(errors, "openalex", err)
    if doi:
        out += crossref_links(http, doi, errors)
    seen, unique = set(), []
    for c in sorted(out, key=lambda c: KINDS.index(c.kind)):
        if c.url not in seen:
            seen.add(c.url)
            unique.append(c)
    return unique


def _note(errors: list[str] | None, source: str, err: ApiError) -> None:
    """A lookup that could not be answered (not one that found nothing): the record is asked again next run."""
    if errors is not None and (err.status is None or err.status == 429 or err.status >= 500):
        errors.append(f"{source} lookup: {err}")


def crossref_links(http: Http, doi: str, errors: list[str] | None = None) -> list[Candidate]:
    """The PDF links Crossref lists for the article, if it carries a Creative Commons licence (only then is the
    publisher's copy open to anyone)."""
    try:
        msg = http.get_json("crossref", CROSSREF.format(doi=urllib.parse.quote(doi, safe="/")), {})["message"]
    except ApiError as err:
        _note(errors, "crossref", err)
        return []
    except KeyError:
        return []
    licences = [lic.get("URL") or "" for lic in msg.get("license") or []]
    open_licence = next((u for u in licences if "creativecommons.org" in u), None)
    if not open_licence:
        return []
    return [Candidate("crossref", "pdf", link["URL"], open_licence, "publishedVersion")
            for link in msg.get("link") or []
            if link.get("URL") and "pdf" in (link.get("content-type") or "").lower()
            and link.get("intended-application") != "similarity-checking"]


def fallbacks(http: Http, work, settings: Settings, failed: list[str], errors: list[str]) -> list[Candidate]:
    """Asked only when no located copy gave a text: CORE's repository copy, then archived copies of failed links."""
    out: list[Candidate] = []
    try:
        hit = core.find(http, work, settings.core_api_key)
    except (ApiError, ValueError) as err:        # ValueError: a web page instead of JSON
        hit = None
        errors.append(f"core: {err}")
    if hit:
        # CORE's own text first: its download links often stand behind a bot check.
        page = f"https://core.ac.uk/works/{hit.get('id')}"
        if len(hit.get("fullText") or "") >= MIN_TEXT:
            out.append(Candidate("core", "text", page, None, None, text=hit["fullText"], indirect=True))
        if hit.get("downloadUrl"):
            out.append(Candidate("core", "pdf", hit["downloadUrl"], None, None, indirect=True))
    for url in failed[:ARCHIVE_TRIES]:
        try:
            snap = (http.get_json("wayback", WAYBACK, {"url": url}).get("archived_snapshots") or {}).get("closest")
        except (ApiError, ValueError) as err:
            errors.append(f"wayback: {err}")
            continue
        if snap and snap.get("available") and str(snap.get("status")) == "200" and snap.get("timestamp"):
            out.append(Candidate("wayback", "pdf", f"https://web.archive.org/web/{snap['timestamp']}id_/{url}",
                                 None, None, indirect=True))
    return out


# Fetching and parsing

def retrieve(store: Store, work_ids: list[int], settings: Settings, corpus: str, run_id: int | None = None,
             root: Path = ROOT, refresh: bool = False, log=print) -> dict:
    http = Http(store, run_id, max_retries=2)
    core.configure(http, settings.core_api_key)
    http.pace("wayback", 1.0)
    if not settings.s2_api_key:
        http.pace("semantic_scholar", 1.1)
    http.breaker("semantic_scholar")
    grobid = grobid_url(settings)
    folder = root / "fulltext" / f"corpus_{corpus}"
    folder.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for w in store.db.execute(f"SELECT * FROM works WHERE work_id IN ({','.join('?' * len(work_ids))})",
                              tuple(work_ids)).fetchall() if work_ids else []:
        old = store.get_fulltext(w["work_id"])
        if old and old["status"] == "parsed" and not refresh:
            counts["already parsed"] = counts.get("already parsed", 0) + 1
            continue
        if old and not refresh and settled(old):
            counts[f"already settled ({old['status']})"] = counts.get(f"already settled ({old['status']})", 0) + 1
            continue
        result = _retrieve_one(http, w, settings, folder, root, grobid)
        store.set_fulltext(w["work_id"], **result)
        key = result["status"] if result["status"] != "parsed" else f"parsed ({result['source']}, {result['parser']})"
        counts[key] = counts.get(key, 0) + 1
        log(f"  #{w['work_id']}: {key}" + (f" - {result['error']}" if result.get("error") else ""))
        http.sleep(1.0)
    return counts


# An outcome that could change on the next run: a service that refused or failed, or a connection that broke.
TRANSIENT = re.compile(r"HTTP (?:None|202|429|5\d\d)\b|paused for this run|timed out|getaddrinfo", re.I)
CHAIN_SINCE = "2026-10-25"      # the day CORE and the archive joined the chain: older outcomes are asked again


def settled(row) -> bool:
    """A record not worth fetching again without --refresh: every source answered and none had a usable copy."""
    return (row["status"] in ("no_open_access", "failed") and (row["updated_at"] or "") >= CHAIN_SINCE
            and not TRANSIENT.search(row["error"] or ""))


def _retrieve_one(http: Http, w, settings: Settings, folder: Path, root: Path, grobid: str | None) -> dict:
    errors, blocked, failed = [], [], []
    candidates = locate(http, w, settings, errors)
    queue, seen, fell_back = list(candidates), set(), False
    while True:
        for c in queue:
            if c.url in seen and c.kind != "text":
                continue
            seen.add(c.url)
            result = _attempt(http, w, c, folder, root, grobid, errors, blocked)
            if result:
                return {**result, "error": "; ".join(errors) or None}
            if c.kind in ("pdf", "page") and c.source != "wayback":
                failed.append(c.url)
        if fell_back:
            break
        fell_back, queue = True, fallbacks(http, w, settings, failed, errors)
        candidates += queue
    if not candidates:
        return {"status": "no_open_access", "error": "; ".join(errors)[:1000] or None}
    # The link kept for the reader: a free copy only a bot check stood in front of, else the first PDF found.
    keep = blocked or [c.url for c in candidates if c.kind == "pdf"] or [candidates[0].url]
    return {"status": "failed", "url": keep[0], "error": "; ".join(errors)[:1000]}


def _attempt(http: Http, w, c: Candidate, folder: Path, root: Path, grobid: str | None, errors: list[str],
             blocked: list[str]) -> dict | None:
    """One candidate: its parsed text, or None with the reason added to `errors`."""
    if c.kind == "text":
        if not same_document(w, c.text[:6000]):
            errors.append(f"{c.source}: its text is another publication (title and DOI not found at its start)")
            return None
        body = c.text.encode("utf-8")
        path = folder / f"{w['work_id']}.{c.source}.txt"
        path.write_bytes(body)
        return {"status": "parsed", "source": c.source, "url": c.url, "license": c.license, "version": c.version,
                "file_sha256": hashlib.sha256(body).hexdigest(), "file_path": path.relative_to(root).as_posix(),
                "parser": f"{c.source} text", "sections": split_sections(c.text) or [{"head": "Body", "text": c.text}]}
    try:
        body, url = _fetch(http, c)
    except ApiError as err:
        if BOT_CHECK.search(str(err)):
            blocked.append(c.url)
            errors.append(f"{c.source}: blocked by the site's bot check ({c.url})")
        else:
            errors.append(f"{c.source}: {err}")
        return None
    base = {"source": c.source, "url": url, "license": c.license, "version": c.version,
            "file_sha256": hashlib.sha256(body).hexdigest()}
    if c.kind == "jats":
        path = folder / f"{w['work_id']}.jats.xml"
        path.write_bytes(body)
        try:
            sections = parse_jats(body)
        except ET.ParseError as err:
            errors.append(f"{c.source}: XML did not parse ({err})")
            return None
        parser = "jats"
    else:
        if not body.startswith(b"%PDF-") or len(body) > MAX_PDF:
            if not body.startswith(b"%PDF-") and BOT_CHECK.search(_page_title(body)):
                blocked.append(c.url)
                errors.append(f"{c.source}: blocked by the site's bot check ({c.url})")
            else:
                errors.append(f"{c.source}: not a PDF (a web page naming no PDF)" if not body.startswith(b"%PDF-")
                              else f"{c.source}: PDF over {MAX_PDF // 1_000_000} MB")
            return None
        # A PDF reached through a page's link, an aggregator or an archive must be the record's own paper.
        if (c.indirect or url != c.url) and not same_document(w, first_pages(body)):
            errors.append(f"{c.source}: the PDF is another publication (title and DOI not on its first pages)")
            return None
        path = folder / f"{w['work_id']}.pdf"
        path.write_bytes(body)
        sections, parser, err = parse_pdf(body, grobid)
        if not sections:
            errors.append(f"{c.source}: {err or 'no text in the PDF'}")
            return None
    return {**base, "status": "parsed", "file_path": path.relative_to(root).as_posix(), "parser": parser,
            "sections": sections}


def first_pages(pdf: bytes, n: int = 2) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf))
        return " ".join((p.extract_text() or "") for p in reader.pages[:n])
    except Exception:          # pypdf raises many types on damaged files
        return ""


def same_document(work, start: str) -> bool:
    """Is a text the record's paper? Its DOI on the first pages, or nearly all of its title's words there. A text
    that could not be read (no words at all) is let through: GROBID may still read the PDF, and nothing else
    would check it."""
    if not start.strip():
        return True
    doi = normalize_doi(work["doi"])
    if doi and doi in {d.lower().rstrip(".,;)") for d in DOI_TEXT.findall(start)}:
        return True
    key = title_key(work["title"]) or ""
    flat = title_key(start) or ""
    words = {x for x in key.split() if len(x) >= 3}
    if len(words) < 3:                    # too short to count words: the whole title must stand there
        return bool(key) and key in flat
    return len(words & set(flat.split())) / len(words) >= TITLE_SHARE


def _fetch(http: Http, c: Candidate) -> tuple[bytes, str]:
    """The candidate's file and the URL it came from. A web page instead of a PDF is searched for the PDF it names
    (`citation_pdf_url`), and that one is fetched; one hop only."""
    accept = {"jats": "application/xml", "pdf": "application/pdf",
              "page": "text/html,application/xhtml+xml,application/pdf"}[c.kind]
    body = http.get(c.source + ("_page" if c.kind == "page" else "_fulltext"), c.url, {}, {"Accept": accept},
                    keep_body=False)
    if c.kind == "jats" or body.startswith(b"%PDF-"):
        return body, c.url
    link = pdf_link(body, c.url)
    if not link or link == c.url:
        return body, c.url
    return http.get(c.source + "_fulltext", link, {}, {"Accept": "application/pdf"}, keep_body=False), link


META = re.compile(r"<meta\b[^>]*>", re.I)
LINK = re.compile(r"<link\b[^>]*>", re.I)
ATTR = re.compile(r"""([\w:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')""")
ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
# A link that is plainly the page's PDF: its address ends in .pdf or is a repository's file or download path, and
# its text says PDF, download or full text.
PDF_HREF = re.compile(r"\.pdf(?:$|[?#])|/bitstream/|/download/|/files/original|[?&]type=pdf|/pdf(?:/|$)", re.I)
PDF_WORDS = re.compile(r"\bpdf\b|download|full[\s-]*text|volltext", re.I)


def pdf_link(page: bytes, base: str) -> str | None:
    """The PDF a web page names for itself: `<meta name="citation_pdf_url">` (Highwire tags, used by journals and
    repositories for Google Scholar), else `<link rel="alternate" type="application/pdf">`, else the first link on
    the page that is plainly its PDF (on the same site; the PDF's first pages are checked against the record)."""
    text = page[:400_000].decode("utf-8", "replace")
    for pattern, test, key in ((META, lambda a: a.get("name", a.get("property", "")).lower() == "citation_pdf_url",
                                "content"),
                               (LINK, lambda a: a.get("type", "").lower() == "application/pdf"
                                and "alternate" in a.get("rel", "").lower(), "href")):
        for tag in pattern.findall(text):
            attrs = {k.lower(): html.unescape(v1 or v2) for k, v1, v2 in ATTR.findall(tag)}
            if test(attrs) and attrs.get(key, "").strip():
                return urllib.parse.urljoin(base, attrs[key].strip())
    host = urllib.parse.urlparse(base).netloc.lower()
    for attrs_text, inner in ANCHOR.findall(text):
        attrs = {k.lower(): html.unescape(v1 or v2) for k, v1, v2 in ATTR.findall(attrs_text)}
        href = attrs.get("href", "").strip()
        label = re.sub(r"<[^>]+>", " ", inner) + " " + attrs.get("title", "") + " " + attrs.get("aria-label", "")
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = urllib.parse.urljoin(base, href)
        if (PDF_HREF.search(urllib.parse.urlparse(url).path + ("?" + urllib.parse.urlparse(url).query))
                and PDF_WORDS.search(label) and urllib.parse.urlparse(url).netloc.lower() == host):
            return url
    return None


def _page_title(page: bytes) -> str:
    m = re.search(rb"<title[^>]*>(.*?)</title>", page[:20000], re.I | re.S)
    return m.group(1).decode("utf-8", "replace") if m else ""


def parse_jats(xml: bytes) -> list[dict]:
    root = ET.fromstring(xml)
    body = root.find(".//body")
    if body is None:
        return []
    out = []

    def walk(sec, path):
        head = _text(sec.find("title")) or ""
        here = path + [head] if head else path
        paras = [_text(p) for p in sec.findall("p")]
        if any(paras):
            out.append({"head": " > ".join(here) or "Body", "text": "\n".join(p for p in paras if p)})
        for child in sec.findall("sec"):
            walk(child, here)

    loose = [_text(p) for p in body.findall("p")]
    if any(loose):
        out.append({"head": "Body", "text": "\n".join(p for p in loose if p)})
    for sec in body.findall("sec"):
        walk(sec, [])
    return out


def grobid_url(settings: Settings) -> str | None:
    url = (settings.grobid_url or "http://localhost:8070").rstrip("/")
    try:
        with urllib.request.urlopen(url + "/api/isalive", timeout=5) as r:
            return url if r.read().strip().lower() == b"true" else None
    except OSError:
        return None


def parse_pdf(pdf: bytes, grobid: str | None) -> tuple[list[dict], str, str | None]:
    if grobid:
        try:
            tei = _grobid(pdf, grobid)
            sections = parse_tei(tei)
            if sections:
                return sections, f"grobid {_grobid_version(grobid)}", None
        except (OSError, ET.ParseError) as err:
            fallback_error = f"GROBID failed ({err}); used pypdf"
        else:
            fallback_error = "GROBID found no sections; used pypdf"
    else:
        fallback_error = None
    sections = parse_pdf_text(pdf)
    return sections, "pypdf", fallback_error if sections else (fallback_error or "pypdf found no text")


def _grobid(pdf: bytes, url: str) -> bytes:
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"input\"; filename=\"paper.pdf\"\r\n"
            f"Content-Type: application/pdf\r\n\r\n").encode() + pdf + \
           f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"consolidateHeader\"\r\n\r\n0" \
           f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(url + "/api/processFulltextDocument", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                          "Accept": "application/xml"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def _grobid_version(url: str) -> str:
    try:
        with urllib.request.urlopen(url + "/api/version", timeout=5) as r:
            raw = r.read().decode().strip()
        try:
            return str(json.loads(raw).get("version", raw))[:20]
        except (ValueError, AttributeError):
            return raw[:20]
    except OSError:
        return "?"


def parse_tei(tei: bytes) -> list[dict]:
    root = ET.fromstring(tei)
    body = root.find(f".//{TEI}text/{TEI}body")
    if body is None:
        return []
    out = []
    for div in body.iter(f"{TEI}div"):
        head = _text(div.find(f"{TEI}head")) or ""
        paras = [_text(p) for p in div.findall(f"{TEI}p")]
        if any(paras):
            n = div.find(f"{TEI}head")
            num = n.get("n") if n is not None and n.get("n") else ""
            out.append({"head": f"{num} {head}".strip() or "Body", "text": "\n".join(p for p in paras if p)})
    return out


HEADING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(abstract|introduction|background|literature review|theory|"
                     r"the present study|methods?|methodology|materials and methods|participants|sample|measures?|"
                     r"instruments?|procedure|design|data|data analysis|statistical analys[ie]s|analys[ie]s|results|"
                     r"findings|discussion|conclusions?|limitations|references|acknowledg(?:e)?ments)\s*$", re.I)


def parse_pdf_text(pdf: bytes) -> list[dict]:
    """Fallback without GROBID: pypdf text, split at lines that look like section headings."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return []
    try:
        reader = PdfReader(io.BytesIO(pdf))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:  # pypdf raises many types on damaged files
        return []
    return split_sections(text)


def split_sections(text: str) -> list[dict]:
    sections, head, buf = [], "Body", []
    for line in text.splitlines():
        if HEADING.match(line) and len(line.strip()) < 40:
            if buf:
                sections.append({"head": head, "text": " ".join(buf)})
            head, buf = line.strip(), []
        elif line.strip():
            buf.append(line.strip())
    if buf:
        sections.append({"head": head, "text": " ".join(buf)})
    return [s for s in sections if len(s["text"]) > 40]


# What the extraction agent reads

def methods_text(sections: list[dict], limit: int = 14000) -> str:
    """Methods-like sections first, then results, within `limit` characters; never references."""
    return select_text(sections, (METHOD_HEADS, RESULT_HEADS), limit)


def findings_text(sections: list[dict], limit: int = 14000) -> str:
    """Results first, then discussion, then methods: what a study found and for whom."""
    return select_text(sections, (RESULT_HEADS, DISCUSSION_HEADS, METHOD_HEADS), limit)


UNDIVIDED = 3       # a text without recognisable sections is read this many times further, so it reaches the results


def select_text(sections: list[dict], order, limit: int) -> str:
    usable = [s for s in sections if not SKIP_HEADS.search(s["head"])]
    picked = []
    for pattern in order:
        picked += [s for s in usable if pattern.search(s["head"]) and s not in picked]
    if sum(len(s["text"]) for s in picked) * 2 < sum(len(s["text"]) for s in usable):
        # the headings found do not divide the paper (a PDF read without GROBID, say: one heading matched and the rest
        # lies under "Introduction" or no heading at all): the sections cannot be chosen, so give the matched ones and
        # then the rest in its own order, further, or the reader sees only the conclusion or the introduction
        picked, limit = picked + [s for s in usable if s not in picked], limit * UNDIVIDED
    out, used = [], 0
    for s in picked:
        room = limit - used
        if room <= 200:
            break
        chunk = f"## {s['head']}\n{s['text']}"[:room]
        out.append(chunk)
        used += len(chunk)
    return "\n\n".join(out)


def _text(el) -> str | None:
    if el is None:
        return None
    return " ".join("".join(el.itertext()).split()) or None


def summary_rows(store: Store, work_ids: list[int]) -> list:
    if not work_ids:
        return []
    return store.db.execute(f"SELECT * FROM fulltexts WHERE work_id IN ({','.join('?' * len(work_ids))})",
                            tuple(work_ids)).fetchall()


def sections_of(row) -> list[dict]:
    return json.loads(row["sections"]) if row and row["sections"] else []
