"""Full texts the reader gets through their own library access.

Open-access copies are fetched automatically (fulltext.py). The rest you download yourself, through your
university's subscriptions, into `fulltext/inbox/`; the pipeline matches each PDF to its record, parses it
like any other full text and keeps it local (`fulltext/` is never shared).

- todo:  the retained records without a full text, the ones that matter most first (the ones you marked, core
         studies, then studies in the review's settings that contributed findings, then the rest), with links.
- add_from_inbox: match, store and parse the PDFs you dropped into the inbox, and those in your downloads folder
         that match a retained record still without a full text (copied there, never moved or deleted).
- add_all: the same for every run, with the agent tasks to run again for the studies that gained a full text.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import urllib.parse
from pathlib import Path

from . import document, tables
from .config import ROOT, Settings
from .fulltext import grobid_url, parse_pdf
from .store import Store, title_key

INBOX = Path("fulltext") / "inbox"
CHECKED = INBOX / ".downloads_checked.json"   # downloads already compared with a run's candidates
LICENSE = "the reader's institutional access; local copy only, not redistributed"
GROUPS = {0: "Studies you marked (your include or unsure)",
          1: "Core studies (meet every inclusion criterion)",
          2: "Studies behind the latest manuscript's headline claims (check these against the full text)",
          3: "Studies in the review's settings that contributed findings",
          4: "Other studies that contributed findings",
          5: "Retained records without findings so far"}


POTENTIAL = ("Potential core studies, needed first: they meet every inclusion criterion on their abstract and count "
             "as core once read in full")


def group_label(g: int, rows: list[dict]) -> str:
    shown = {r["group"] for r in rows}
    if g == 1 and any(r.get("potential") for r in rows if r["group"] == 1):
        return POTENTIAL
    return "Studies that contributed findings" if g == 4 and 3 not in shown else GROUPS[g]


def marked(store: Store, run_id: int) -> set[int]:
    """The records a person kept in this run (include or unsure, at any stage): the ones they find important."""
    return {r[0] for r in store.db.execute("SELECT work_id FROM screening WHERE run_id = ? AND screener LIKE "
                                           "'human:%' AND decision != 'exclude'", (run_id,))}


def todo(store: Store, run_id: int, protocol: dict, proxy: str | None = None) -> list[dict]:
    from .agents import kept_ids
    mine = marked(store, run_id)
    elig = {e["work_id"]: e for e in tables.eligibility(store, run_id, protocol)} if tables.criteria(protocol) else {}
    findings = {}
    for r in tables.finding_rows(store, run_id):
        findings.setdefault(r["work_id"], []).append(r["source"])
    setting = next((c["id"] for c in tables.criteria(protocol) if c.get("dimension") == "setting"), None)
    from . import checks, paper
    paper.ensure(store)
    latest = checks.latest_paper(store, run_id)
    headline = {i["work"] for i in checks.headline_findings(store, latest, protocol)} if latest else set()
    out = []
    for w, meta in tables.works(store, kept_ids(store, run_id)).items():
        ft = store.get_fulltext(w)
        short = bool(ft and ft["status"] == "parsed" and (ft["chars"] or 0) < tables.FULL_MIN)
        if ft and ft["status"] == "parsed" and not short:
            continue                    # a text too short to be the study (a first page, an abstract) is still needed
        e, n = elig.get(w), len(findings.get(w, []))
        if e and e.get("abstract_only"):
            continue                    # a conference abstract: there is no fuller text to get
        in_setting = bool(setting and e and e["cells"].get(setting) == "yes")
        # without a full text, a study that meets every criterion is core, or a potential core study where the
        # protocol counts only studies read in full ([paper] core_needs_fulltext): either way it comes first
        potential = bool(e and e.get("potential"))
        core = bool(e and e["core"]) or potential
        group = 0 if w in mine else 1 if core else 2 if w in headline else 3 if n and in_setting else 4 if n else 5
        free = ft["url"] if ft and ft["status"] == "failed" and "bot check" in (ft["error"] or "") else None
        out.append({"work_id": w, "group": group, "core": core, "potential": potential,
                    "cite": meta["cite"], "title": meta["title"] or "",
                    "year": meta["year"], "venue": meta["venue"] or "", "type": tables.kind_of(meta),
                    "doi": meta["doi"], "findings": n, "status": ft["status"] if ft else "not tried",
                    "had": (f"a short text ({ft['chars']} characters)" if short else
                            "abstract" if len((meta.get("abstract") or "").strip()) > 50 else "title only"),
                    "links": links(meta, proxy, free)})
    return sorted(out, key=lambda r: (r["group"], not r["core"], -r["findings"], r["cite"]))


def links(meta: dict, proxy: str | None = None, free: str | None = None) -> dict[str, str]:
    """DOI and Google Scholar links; with a library proxy (LIBRARY_PROXY, e.g. an EZproxy `login?url=` prefix, or a
    template with `{url}`), also the DOI through the library, which asks you to sign in once in your browser.
    `free`: an open-access copy the pipeline found but a bot check kept it from, listed first ("Free copy"): it opens
    in a browser without a library login."""
    out = {"Free copy": free} if free else {}
    if meta.get("doi"):
        out["DOI"] = f"https://doi.org/{meta['doi']}"
        if proxy:
            out["Library"] = (proxy.replace("{url}", urllib.parse.quote(out["DOI"], safe="")) if "{url}" in proxy
                              else proxy + out["DOI"])
    if meta.get("title"):
        out["Google Scholar"] = "https://scholar.google.com/scholar?q=" + urllib.parse.quote(f'"{meta["title"]}"')
    return out


def todo_markdown(rows: list[dict], inbox: Path, downloads: Path | None = None) -> str:
    where = (f"Leave each PDF where your browser saves it (`{downloads}`), or save it in `{inbox}`"
             if downloads else f"Save each PDF in `{inbox}`")
    lines = ["# Full texts to download", "",
             f"{len(rows)} retained records have no full text yet. Download them through your university library "
             f"(the DOI link opens through it with the library's browser extension, or use the Library link). "
             f"{where}. A file named with the record number shown (for example `20.pdf`) is matched at once; "
             "otherwise the pipeline matches it by its DOI or title. Then press **Add downloaded PDFs** in the app, "
             "or run `python -m psyscientist add-pdfs` (every run at once).", "",
             "The PDFs stay on this computer; they are never shared or uploaded.", ""]
    free = sum(1 for r in rows if "Free copy" in r["links"])
    if free:
        lines += [f"**{free} of them are open access**, but the publisher's site stopped the pipeline with a bot check. "
                  "Their *Free copy* link opens the PDF in your browser without a library login.", ""]
    shown = sorted({r["group"] for r in rows})
    for g in shown:
        head = group_label(g, rows)
        lines += [f"## {head}", "", "| No. | Study | Title | Type | Agents had | Findings so far | Links |",
                  "| ---: | --- | --- | --- | --- | ---: | --- |"]
        for r in (r for r in rows if r["group"] == g):
            title = r["title"].replace("|", "/")
            kind = r["type"] + ((", potential core" if r.get("potential") else ", core") if g == 0 and r.get("core") else "")
            lines.append(f"| {r['work_id']} | {r['cite']} | {title} | {kind} | {r.get('had', '')} | {r['findings']} | "
                         + " · ".join(f"[{k}]({v})" for k, v in r["links"].items()) + " |")
        lines.append("")
    return "\n".join(lines)


def write_todo(store: Store, run_id: int, protocol: dict, root: Path = ROOT,
               settings: Settings | None = None) -> tuple[Path, Path, int]:
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    rows = todo(store, run_id, protocol, settings.library_proxy if settings else None)
    base = root / "output" / f"corpus_{corpus}" / f"run_{run_id:03d}"
    base.mkdir(parents=True, exist_ok=True)
    (root / INBOX).mkdir(parents=True, exist_ok=True)
    text = todo_markdown(rows, (root / INBOX).resolve(), settings.downloads_dir if settings else None)
    md = base / "fulltext_todo.md"
    md.write_text(text, encoding="utf-8")
    page = md.with_suffix(".html")
    page.write_text(document.to_html(text, "Full texts to download"), encoding="utf-8")
    return md, page, len(rows)


# The inbox

DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.I)


def _first_pages(pdf: bytes, n: int = 2) -> str:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(pdf))
        return " ".join((p.extract_text() or "") for p in reader.pages[:n])
    except Exception:          # a PDF pypdf cannot read is matched by its file name only
        return ""


RECORD_NAME = re.compile(r"\s*(\d+)(?:$|[\s_.-]+(?=[^\d\s]))")   # "20", "20_smith", "20 - title"; not "1472-6920-14"


def match(name: str, pdf: bytes, candidates: dict[int, dict], by_name: bool = True) -> tuple[int | None, str]:
    """Which retained record a PDF is: its file name's number (`by_name`, for files saved to the inbox for the
    pipeline), a DOI on its first pages, or its title there."""
    m = RECORD_NAME.match(name) if by_name else None
    if m and int(m.group(1)) in candidates:
        return int(m.group(1)), "file name"
    text = _first_pages(pdf)
    dois = {d.lower().rstrip(".,;)") for d in DOI.findall(text)}
    for w, meta in candidates.items():
        if meta.get("doi") and meta["doi"].lower() in dois:
            return w, "DOI"
    # A title is matched where titles stand, near the top of the first page; a short generic title ("Competency-Based
    # Assessment") anywhere in the text would match many other papers.
    flat = title_key(text) or ""
    for w, meta in candidates.items():
        key = title_key(meta.get("title") or "") or ""
        if len(key) >= 30 and (key[:80] in flat[:1000] or (len(key) >= 60 and key[:80] in flat)):
            return w, "title"
    return None, ""


def _store_pdf(store: Store, w: int, pdf: bytes, folder: Path, grobid: str | None, root: Path) -> tuple[bool, str, str]:
    target = folder / f"{w}.pdf"
    target.write_bytes(pdf)
    sections, parser, err = parse_pdf(pdf, grobid)
    if sections:
        store.set_fulltext(w, "parsed", source="library", license=LICENSE,
                           file_path=target.relative_to(root).as_posix(),
                           file_sha256=hashlib.sha256(pdf).hexdigest(), parser=parser, sections=sections, error=err)
    return bool(sections), parser, err


REJECTED = INBOX / ".rejected.json"     # {work_id: [sha256 of PDFs found to be another publication]}


def _rejected(root: Path) -> dict[str, list[str]]:
    try:
        return json.loads((root / REJECTED).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def reject(store: Store, w: int, reason: str, root: Path = ROOT) -> None:
    """A stored PDF that is not the record's full text (another article on the same pages, a page of abstracts, a
    publication that only cites it): the record has no full text again, so it returns to the download list, and the
    same file is never matched to it again. The file stays where it is."""
    ft = store.get_fulltext(w)
    if not ft or not ft["file_sha256"]:
        raise ValueError(f"record {w} has no stored PDF")
    gone = _rejected(root)
    gone[str(w)] = sorted(set(gone.get(str(w), [])) | {ft["file_sha256"]})
    (root / REJECTED).parent.mkdir(parents=True, exist_ok=True)
    (root / REJECTED).write_text(json.dumps(gone, indent=1), encoding="utf-8")
    store.set_fulltext(w, "failed", source=ft["source"], license=ft["license"], file_path=ft["file_path"],
                       file_sha256=ft["file_sha256"], parser=ft["parser"], error=f"wrong PDF: {reason}")


def _parsed(store: Store, w: int) -> bool:
    """Has a full text: a parsed one long enough to be the study (a shorter one may be replaced by a downloaded PDF)."""
    ft = store.get_fulltext(w)
    return bool(ft and ft["status"] == "parsed" and (ft["chars"] or 0) >= tables.FULL_MIN)


def add_from_inbox(store: Store, run_id: int, settings: Settings, root: Path = ROOT, log=print,
                   quiet: bool = False) -> dict:
    """The PDFs in the inbox (matched by record number, DOI or title, then moved to inbox/done), and those in the
    downloads folder (`settings.downloads_dir`) that match a retained record still without a full text by DOI or title:
    those are copied, and the downloads folder is never changed. `quiet`: do not report inbox files no record of this
    run matches (another run may)."""
    from .agents import kept_ids
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    inbox, done = root / INBOX, root / INBOX / "done"
    inbox.mkdir(parents=True, exist_ok=True)
    folder = root / "fulltext" / f"corpus_{corpus}"
    folder.mkdir(parents=True, exist_ok=True)
    candidates = tables.works(store, kept_ids(store, run_id))
    grobid = grobid_url(settings)
    if not grobid and not quiet:
        log("GROBID is not running: parsing with pypdf, which finds fewer sections. Start GROBID for better text.")
    counts = {"added": [], "unmatched": [], "failed": [], "downloads_read": 0}
    gone = _rejected(root)
    for path in sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"):
        pdf = path.read_bytes()
        if not pdf.startswith(b"%PDF-"):
            counts["failed"].append(f"{path.name}: not a PDF")
            continue
        w, how = match(path.stem, pdf, candidates)
        if w is not None and hashlib.sha256(pdf).hexdigest() in gone.get(str(w), []):
            w = None                        # this file was found to be another publication, not the record
        if w is None:
            counts["unmatched"].append(path.name)
            if not quiet:
                log(f"  {path.name}: no retained record matches; rename it with the record number, e.g. 20.pdf")
            continue
        ok, parser, err = _store_pdf(store, w, pdf, folder, grobid, root)
        if not ok:
            counts["failed"].append(f"{path.name}: {err or 'no text found'}")
            log(f"  {path.name} -> #{w}: no text found ({err})")
            continue
        counts["added"].append(w)
        log(f"  {path.name} -> #{w} {candidates[w]['cite']} (matched by {how}, {parser})")
        done.mkdir(exist_ok=True)
        shutil.move(str(path), str(done / path.name))
    downloads = settings.downloads_dir
    if downloads and Path(downloads).is_dir() and Path(downloads).resolve() != inbox.resolve():
        missing = {w: m for w, m in candidates.items() if not _parsed(store, w)}
        if missing:
            _from_downloads(store, run_id, Path(downloads), missing, folder, grobid, root, counts, log)
    return counts


def _from_downloads(store, run_id, downloads: Path, missing: dict, folder: Path, grobid, root: Path, counts: dict,
                    log) -> None:
    """Compare the PDFs at the top of the downloads folder with the run's records still without a full text. A file
    is not read again while those records are among the ones it was already compared with and did not match
    (fulltext/inbox/.downloads_checked.json)."""
    path_checked = root / CHECKED
    try:
        checked = json.loads(path_checked.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        checked = {}
    wanted = set(missing)
    gone = _rejected(root)
    for path in sorted(p for p in downloads.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"):
        stat = path.stat()
        key = f"{path.name}|{stat.st_size}|{stat.st_mtime_ns}"
        before = set(checked.get(key, {}).get(str(run_id)) or [])
        if wanted <= before:
            continue
        try:
            pdf = path.read_bytes()
        except OSError:
            continue
        counts["downloads_read"] += 1
        w = None
        if pdf.startswith(b"%PDF-"):
            w, how = match(path.stem, pdf, missing, by_name=False)
        if w is not None and hashlib.sha256(pdf).hexdigest() in gone.get(str(w), []):
            w = None                        # found to be another publication, not the record
        if w is not None and not _parsed(store, w):
            ok, parser, err = _store_pdf(store, w, pdf, folder, grobid, root)
            if ok:
                counts["added"].append(w)
                log(f"  {downloads.name}/{path.name} -> #{w} {missing[w]['cite']} (matched by {how}, {parser}; "
                    "copied, the original stays)")
            else:
                counts["failed"].append(f"{path.name}: {err or 'no text found'}")
        checked.setdefault(key, {})[str(run_id)] = sorted(before | wanted)   # read: against these records
    path_checked.parent.mkdir(parents=True, exist_ok=True)
    path_checked.write_text(json.dumps(checked), encoding="utf-8")


def add_all(store: Store, settings: Settings, root: Path = ROOT, log=print, runs: list[int] | None = None) -> dict:
    """add_from_inbox for every run with a screening sheet (or the given runs). Returns {run: counts}, each with
    `gained`: the run's retained records that got a full text in this call (a record two runs share counts in both),
    and the inbox files no run matched."""
    from .agents import kept_ids
    if runs is None:
        runs = [r[0] for r in store.db.execute("SELECT DISTINCT run_id FROM samples WHERE purpose = 'pilot_screening' "
                                               "ORDER BY run_id")]
    out, gained = {}, set()
    if not grobid_url(settings):
        log("GROBID is not running: parsing with pypdf, which finds fewer sections. Start GROBID for better text.")
    for run_id in runs:
        counts = add_from_inbox(store, run_id, settings, root, log=log, quiet=True)
        gained |= set(counts["added"])
        out[run_id] = counts
    for run_id in runs:
        out[run_id]["gained"] = sorted(gained & set(kept_ids(store, run_id)))
    left = sorted(p.name for p in (root / INBOX).iterdir() if p.is_file() and p.suffix.lower() == ".pdf") \
        if (root / INBOX).is_dir() else []
    for name in left:
        log(f"  {name}: no retained record of any run matches; rename it with the record number, e.g. 20.pdf")
    out["unmatched_inbox"] = left
    return out


def next_steps(store: Store, run_id: int, works: list[int]) -> list[str]:
    """The agent tasks that read the new full texts: charting and findings again for these records, by the run's own
    latest extractors, so their earlier charting is replaced and their earlier findings are kept as superseded."""
    ids = ",".join(map(str, works))
    out = []
    for kind, table in (("extract", "study_features"), ("findings", "findings")):
        agent = tables._latest(store, table, run_id)
        if agent and works:
            out.append(f"python -m psyscientist agent-tasks {kind} {run_id} --works {ids} --agent {agent}")
    return out
