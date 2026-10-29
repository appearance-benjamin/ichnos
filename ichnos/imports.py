"""Library database exports: RIS, CSV / tab-delimited, and Web of Science plain text.

APA PsycINFO (EBSCOhost or Ovid), Scopus and Web of Science have no API we can
use, so their searches are run by hand through the university library and
exported. parse_file turns any of those exports into records shaped like the
API sources' (see sources/), so they merge into the same pool.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import shutil
import tomllib
from datetime import date
from pathlib import Path

from . import query
from .config import ROOT, load_protocol
from .sources import SearchResult
from .store import Store, normalize_doi

SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"<>]+", re.IGNORECASE)
RIS_LINE = re.compile(r"^([A-Z][A-Z0-9])  -(?: (.*))?$")
WOS_LINE = re.compile(r"^([A-Z][A-Z0-9])(?: (.*))?$")

# Header aliases for CSV and tab-delimited exports (Scopus, Web of Science, EBSCO), lowercased.
CSV_FIELDS = {
    "title": ["title", "document title", "article title", "ti"],
    "abstract": ["abstract", "ab"],
    "authors": ["authors", "author", "author full names", "author(s)", "au", "af"],
    "year": ["year", "publication year", "py", "publication date", "date"],
    "venue": ["source title", "journal", "publication title", "journal title", "source", "so"],
    "doi": ["doi", "di", "digital object identifier"],
    "pmid": ["pubmed id", "pmid", "pm"],
    "type": ["document type", "publication type", "dt", "pt", "type"],
    "language": ["language of original document", "language", "la"],
    "source_id": ["eid", "ut (unique wos id)", "ut", "accession number", "an"],
}


def parse_file(path: str | Path) -> tuple[str, list[dict], list[str]]:
    """Return (format, records, notes). Records without a title are skipped and counted in notes."""
    text = read_text(Path(path))
    fmt = detect_format(text, Path(path).suffix.lower())
    raw = {"ris": parse_ris, "wos": parse_wos_plain, "csv": parse_csv}[fmt](text)
    records = [r for r in raw if r.get("title")]
    notes = []
    if len(records) < len(raw):
        notes.append(f"{len(raw) - len(records)} records without a title skipped")
    return fmt, records, notes


def read_text(path: Path) -> str:
    data = path.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def detect_format(text: str, suffix: str = "") -> str:
    head = [line for line in text.splitlines()[:40] if line.strip()]
    if any(RIS_LINE.match(line) and line.startswith("TY  -") for line in head):
        return "ris"
    if head and (head[0].startswith("FN ") or head[0].startswith("PT ")) and "\t" not in head[0]:
        return "wos"
    if head:
        return "csv"
    raise ValueError("empty file")


def parse_ris(text: str) -> list[dict]:
    records, fields, last = [], {}, None
    for line in text.splitlines():
        m = RIS_LINE.match(line.rstrip())
        if m:
            tag, value = m.group(1), (m.group(2) or "").strip()
            if tag == "ER":
                if fields:
                    records.append(_from_ris(fields))
                fields, last = {}, None
                continue
            fields.setdefault(tag, []).append(value)
            last = tag
        elif last and line.strip():
            fields[last][-1] = (fields[last][-1] + " " + line.strip()).strip()
    if fields:
        records.append(_from_ris(fields))
    return records


def _from_ris(f: dict[str, list[str]]) -> dict:
    first = lambda *tags: next((v for t in tags for v in f.get(t, []) if v), None)  # noqa: E731
    doi = first("DO") or _find_doi(*(v for t in ("UR", "L3", "N1", "M3") for v in f.get(t, [])))
    return {
        "source_id": first("AN", "ID"),       # PsycINFO accession number, WOS:..., Scopus EID
        "doi": normalize_doi(_find_doi(doi) if doi else None),
        "pmid": None,                         # RIS has no PubMed ID tag; DOI and title still merge
        "title": _clean(first("TI", "T1", "BT", "CT")),
        "abstract": _clean(" ".join(f.get("AB", []) or f.get("N2", [])) or None),
        "year": _year(first("PY", "Y1", "DA", "Y2")),
        "venue": _clean(first("T2", "JO", "JF", "JA", "J2", "SE") if first("TY") != "BOOK" else first("PB")),
        "type": first("TY"),
        "language": first("LA"),
        "authors": [a for a in (f.get("AU", []) or f.get("A1", [])) if a],
        "oa_url": None,
    }


def parse_wos_plain(text: str) -> list[dict]:
    """Web of Science 'Plain text file' export: two-letter tags, continuation lines indented."""
    records, fields, last = [], {}, None
    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith("   ") and last:
            if last in ("AU", "AF"):
                fields[last].append(line.strip())
            else:
                fields[last][-1] += " " + line.strip()
            continue
        m = WOS_LINE.match(line.rstrip())
        if not m:
            continue
        tag, value = m.group(1), (m.group(2) or "").strip()
        if tag in ("FN", "VR", "EF"):
            continue
        if tag == "ER":
            if fields:
                records.append(_from_wos(fields))
            fields, last = {}, None
            continue
        fields.setdefault(tag, []).append(value)
        last = tag
    if fields:
        records.append(_from_wos(fields))
    return records


def _from_wos(f: dict[str, list[str]]) -> dict:
    first = lambda *tags: next((v for t in tags for v in f.get(t, []) if v), None)  # noqa: E731
    return {
        "source_id": first("UT"),
        "doi": normalize_doi(first("DI")),
        "pmid": _pmid(first("PM")),
        "title": _clean(first("TI")),
        "abstract": _clean(first("AB")),
        "year": _year(first("PY", "EA")),
        "venue": _clean(first("SO")),
        "type": first("DT", "PT"),
        "language": first("LA"),
        "authors": f.get("AF") or f.get("AU") or [],
        "oa_url": None,
    }


def parse_csv(text: str) -> list[dict]:
    header = text.splitlines()[0] if text else ""
    sep = max(",;\t", key=header.count)
    # Tab-delimited exports (Web of Science) do not quote fields, so a title starting with " must stay literal.
    quoting = csv.QUOTE_NONE if sep == "\t" else csv.QUOTE_MINIMAL
    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=sep, quoting=quoting)
    columns = {(name or "").strip().lower(): name for name in reader.fieldnames or []}
    pick = {field: next((columns[a] for a in aliases if a in columns), None) for field, aliases in CSV_FIELDS.items()}
    if not pick["title"]:
        raise ValueError(f"no title column found; columns are: {', '.join(reader.fieldnames or [])}")
    records = []
    for row in reader:
        get = lambda field: (row.get(pick[field]) or "").strip() or None if pick[field] else None  # noqa: E731
        authors = get("authors")
        records.append({
            "source_id": get("source_id"),
            "doi": normalize_doi(_find_doi(get("doi")) if get("doi") else None),
            "pmid": _pmid(get("pmid")),
            "title": _clean(get("title")),
            "abstract": _clean(_no_abstract(get("abstract"))),
            "year": _year(get("year")),
            "venue": _clean(get("venue")),
            "type": get("type"),
            "language": get("language"),
            "authors": [a.strip() for a in re.split(r";", authors) if a.strip()] if authors else [],
            "oa_url": None,
        })
    return records


def load_entry(entry: dict, tree, root: Path, store: Store, run_id: int) -> SearchResult:
    """Read one protocol [[imports]] entry for a run; every file is logged to `calls` with its full bytes."""
    source = entry["source"]
    sent = entry.get("query") or query.render(tree, entry["platform"])
    records, formats, notes = [], [], []
    for rel in entry["files"]:
        path = root / rel
        data = path.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        store.log_call(run_id, "import", source, f"{rel} (sha256 {sha}), searched {entry.get('searched', '?')}",
                       200, 0, data, {"file": rel, "platform": entry.get("platform"), "searched": entry.get("searched")})
        fmt, recs, file_notes = parse_file(path)
        records.extend(recs)
        formats.append(fmt)
        notes.extend(f"{Path(rel).name}: {n}" for n in file_notes)
    where = entry.get("platform") or "a database"
    notes.insert(0, f"Library export from {where}, searched {entry.get('searched', '?')}: "
                    f"{len(entry['files'])} file(s), {', '.join(sorted(set(formats)))}")
    return SearchResult(source, sent, entry.get("reported_total"), records, "\n".join(notes))


def add_export(corpus: str, files: list[Path], source: str, platform: str | None = None,
               query_file: Path | None = None, searched: str | None = None, hits: int | None = None,
               root: Path = ROOT, log=print) -> dict:
    """Check the export files, copy them under imports/, and register them in the protocol."""
    proto_path, text, protocol = load_protocol(corpus, root)
    if not SOURCE_NAME.match(source):
        raise ValueError("--source must be a short lowercase name such as psycinfo, scopus or wos")
    taken = set(protocol.get("sources", [])) | {e.get("source") for e in protocol.get("imports", [])}
    if source in taken:
        raise ValueError(f"source {source!r} is already in the protocol; pick another name, e.g. {source}_2")
    if platform is None and query_file is None:
        raise ValueError("give --platform (you pasted the string from `strings` unchanged) or --query-file")
    if platform is not None and platform not in query.LIBRARY_DIALECTS:
        raise ValueError(f"--platform must be one of {', '.join(query.LIBRARY_DIALECTS)}")
    searched = searched or date.today().isoformat()
    try:
        date.fromisoformat(searched)
    except ValueError:
        raise ValueError(f"--searched must be a date like 2026-10-17, not {searched!r}") from None
    if query_file is not None:
        sent = read_text(Path(query_file)).strip()
    else:
        sent = query.render(query.parse(protocol["query"]), platform)
    if not sent:
        raise ValueError("the search string is empty")
    if "'''" in sent:
        raise ValueError("the search string cannot contain three single quotes in a row")

    total = 0
    for f in files:
        fmt, recs, notes = parse_file(f)
        total += len(recs)
        log(f"  {Path(f).name}: {fmt}, {summarize(recs)}" + "".join(f"; {n}" for n in notes))
    if total == 0:
        raise ValueError("no records with a title found in the export")

    target_dir = root / "imports" / f"corpus_{corpus.lower()}"
    target_dir.mkdir(parents=True, exist_ok=True)
    copied, rel_paths = [], []
    for f in files:
        dest = target_dir / f"{source}_{searched}_{Path(f).name}"
        if dest.exists() and dest.read_bytes() != Path(f).read_bytes():
            raise ValueError(f"{dest} already exists with different content")
        if not dest.exists():
            shutil.copyfile(f, dest)
            copied.append(dest)
        rel_paths.append(dest.relative_to(root).as_posix())

    lines = ["", "[[imports]]", f"# Registered with add-export on {date.today().isoformat()}.",
             f"source = {json.dumps(source)}"]
    if platform:
        lines.append(f"platform = {json.dumps(platform)}")
    lines.append(f"searched = {json.dumps(searched)}")
    if hits is not None:
        lines.append(f"reported_total = {int(hits)}")
    lines.append("files = [" + ", ".join(json.dumps(p, ensure_ascii=False) for p in rel_paths) + "]")
    lines += ["query = '''", sent, "'''", ""]
    new_text = text.rstrip("\n") + "\n" + "\n".join(lines)
    try:
        entry = tomllib.loads(new_text)["imports"][-1]
    except (tomllib.TOMLDecodeError, KeyError, IndexError):
        entry = {}
    if entry.get("source") != source or entry.get("files") != rel_paths or entry.get("query", "").strip() != sent:
        for c in copied:
            c.unlink()
        raise ValueError("could not write a valid protocol entry; nothing was changed")
    proto_path.write_text(new_text, encoding="utf-8")
    log(f"Registered {total} records as {source!r} in {proto_path.relative_to(root).as_posix()}.")
    log(f"Now run: python -m ichnos pilot {corpus}")
    return entry


def write_strings(corpus: str, root: Path = ROOT) -> tuple[Path, str]:
    """The corpus string for every database, for pasting into the library interfaces."""
    _, _, protocol = load_protocol(corpus, root)
    tree = query.parse(protocol["query"])
    out = [f"# Corpus {corpus} search strings\n",
           f"Protocol {protocol.get('version', '?')}. Every term is limited to titles and abstracts.\n",
           "After running one of these in the library, note the number of results, export them "
           "(RIS works everywhere), and register the file:\n",
           f"```\npython -m ichnos add-export {corpus} <file> --source <name> --platform <platform> "
           f"--hits <number>\n```\n",
           "Compare the number of results with the API sources in the latest report. A count far off "
           "usually means the database parsed something differently (hyphens, truncation); fix the "
           "string there, save what you actually ran as a text file, and pass it with --query-file.\n"]
    out.append("## Library databases (searched by hand)\n")
    for key in query.LIBRARY_DIALECTS:
        out.append(f"**{query.DIALECTS[key].name}** (`--platform {key}`)\n")
        out.append(f"```\n{query.render(tree, key)}\n```\n")
    out.append("## API sources (searched by `pilot`, for comparison)\n")
    for key in (k for k in query.DIALECTS if k not in query.LIBRARY_DIALECTS):
        out.append(f"**{query.DIALECTS[key].name}**\n")
        out.append(f"```\n{query.render(tree, key)}\n```\n")
    text = "\n".join(out)
    path = root / "output" / f"corpus_{corpus.upper()}" / "search_strings.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path, text


def summarize(records: list[dict]) -> str:
    n = len(records)
    years = sorted(r["year"] for r in records if r.get("year"))
    span = f"{years[0]}-{years[-1]}" if years else "no years"
    return (f"{n} records, {sum(1 for r in records if r.get('doi'))} with DOI, "
            f"{sum(1 for r in records if r.get('abstract'))} with abstract, {span}")


def _find_doi(*values: str | None) -> str | None:
    for v in values:
        m = DOI.search(v or "")
        if m:
            return m.group(0).rstrip(".,;)")
    return None


def _year(value: str | None) -> int | None:
    m = re.search(r"\b(1[89]\d\d|20\d\d)\b", value or "")
    return int(m.group(1)) if m else None


def _pmid(value: str | None) -> str | None:
    m = re.search(r"\d{4,9}", value or "")
    return m.group(0) if m else None


def _no_abstract(value: str | None) -> str | None:
    # Scopus fills the column with this placeholder when there is none.
    return None if value and value.strip().lower() in ("[no abstract available]", "no abstract available") else value


def _clean(value: str | None) -> str | None:
    return " ".join(value.split()) if value and value.strip() else None
