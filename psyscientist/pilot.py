"""Pilot run for one corpus: search every source, merge, draw the screening sheet,
write the report. Screening decisions come back in through import_screening."""

from __future__ import annotations

import csv
import io
import random
import re
import tomllib
from pathlib import Path

from . import imports, query
from .config import ROOT, Settings, load_protocol
from .http import ApiError, Http
from .report import write_report
from .sources import eric, europepmc, openalex, pubmed, semantic_scholar
from .store import Store, now, work_link

DIALECT = {openalex.NAME: "openalex", semantic_scholar.NAME: "s2", pubmed.NAME: "pubmed",
           europepmc.NAME: "europepmc", eric.NAME: "eric"}
SHEET_COLUMNS = ["work_id", "found_by", "local_match", "year", "title", "abstract", "venue", "link",
                 "decision", "exclude_reason", "note"]
SAMPLE_PURPOSE = "pilot_screening"


def search_source(source: str, http: Http, sent: str, cap: int, settings: Settings):
    if source == openalex.NAME:
        return openalex.search(http, sent, cap, settings.openalex_api_key, settings.contact_email)
    if source == semantic_scholar.NAME:
        return semantic_scholar.search(http, sent, cap, settings.s2_api_key)
    if source == pubmed.NAME:
        return pubmed.search(http, sent, cap, settings.ncbi_api_key, settings.contact_email)
    if source == europepmc.NAME:
        return europepmc.search(http, sent, cap, settings.contact_email)
    if source == eric.NAME:
        return eric.search(http, sent, cap)
    raise ValueError(f"unknown source {source!r}; known: {', '.join(DIALECT)}")


def run_pilot(corpus: str, settings: Settings, root: Path = ROOT, csv_sep: str = ";", log=print,
              full_sheet: bool = False) -> Path:
    path, text, protocol = load_protocol(corpus, root)
    tree = query.parse(protocol["query"])
    pilot = protocol["pilot"]

    store = Store(settings.db_path)
    sha = store.add_protocol(corpus, path.relative_to(root).as_posix(), text)
    entries = protocol.get("imports", [])
    run_id = store.start_run("pilot", corpus, sha, {"sources": protocol["sources"],
                                                    "imports": [e.get("source") for e in entries],
                                                    "full_sheet": full_sheet, **pilot})
    http = Http(store, run_id)
    log(f"Run {run_id}: corpus {corpus}, protocol {protocol.get('version')} ({sha[:12]})")

    succeeded = 0
    for source in protocol["sources"]:
        sent = ""
        log(f"  {source}: searching ...")
        try:
            sent = query.render(tree, DIALECT.get(source, "openalex"))
            result = search_source(source, http, sent, pilot["max_records_per_source"], settings)
        except (ApiError, ValueError) as err:
            store.set_run_source(run_id, source, sent, None, 0, "failed", str(err))
            log(f"  {source}: FAILED ({err})")
            continue
        _store_result(store, run_id, result)
        succeeded += 1
        log(f"  {source}: {result.reported_total} reported, {len(result.records)} fetched")

    taken = set(protocol["sources"])
    for entry in entries:
        source = entry.get("source", "?")
        log(f"  {source}: reading library export ...")
        try:
            if source in taken:
                raise ValueError(f"source name {source!r} is used twice in the protocol")
            taken.add(source)
            result = imports.load_entry(entry, tree, root, store, run_id)
        except (OSError, ValueError, KeyError) as err:
            store.set_run_source(run_id, source, str(entry.get("query", "")), None, 0, "failed", str(err))
            log(f"  {source}: FAILED ({err})")
            continue
        _store_result(store, run_id, result)
        succeeded += 1
        log(f"  {source}: {len(result.records)} records from {len(entry['files'])} file(s)")

    pool = [r["work_id"] for r in store.db.execute(
        "SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? ORDER BY work_id", (run_id,))]
    # Records already on an earlier sheet of this corpus keep the decisions made there.
    seen = set() if full_sheet else earlier_sheet_ids(store, corpus, run_id)
    candidates = [w for w in pool if w not in seen]
    size = pilot["sample_size"]
    sample = candidates if len(candidates) <= size else sorted(random.Random(pilot["seed"]).sample(candidates, size))
    store.add_sample(run_id, sample, SAMPLE_PURPOSE)

    out_dir = root / "output" / f"corpus_{corpus.upper()}" / f"run_{run_id:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Shuffle the sheet so screening order (and fatigue) is unrelated to source or ID.
    order = random.Random(pilot["seed"]).sample(sample, len(sample))
    write_sheet(store, run_id, order, tree, out_dir / "screening_sheet.csv", csv_sep)
    store.finish_run(run_id, "ok" if succeeded == len(protocol["sources"]) + len(entries) else
                     "partial" if succeeded else "failed")
    write_report(store, run_id, out_dir / "report.md")
    already = len(pool) - len(candidates)
    log(f"Pool: {len(pool)} unique records; {already} already on an earlier sheet; "
        f"sheet: {len(sample)}. Output: {out_dir}")
    store.close()
    return out_dir


def add_sources(store: Store, run_id: int, settings: Settings, root: Path = ROOT, log=print,
                stats: dict | None = None) -> list[int]:
    """Search the protocol's sources that this run has not searched yet (a source added to the protocol after
    the run began) and put the records that are new to the run's pool on its sheet, to be screened like the
    others. Records the run already has are only marked as found by this source too (a record left off a sampled
    sheet stays off it). Library exports registered with add-export ([[imports]]) are loaded the same way; a record
    the run already has gains the export's abstract where it had none (stats["abstracts"] counts them)."""
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    _, _, protocol = load_protocol(corpus, root)
    tree = query.parse(protocol["query"])
    done = {r["source"] for r in store.db.execute(
        "SELECT source FROM run_sources WHERE run_id = ? AND status IN ('ok', 'truncated')", (run_id,))}
    todo = [s for s in protocol["sources"] if s not in done]
    entries = [e for e in protocol.get("imports") or [] if e.get("source") and e["source"] not in done]
    if not todo and not entries:
        log("Every source in the protocol was already searched for this run.")
        return []
    pool = {r[0] for r in store.db.execute("SELECT work_id FROM retrievals WHERE run_id = ?", (run_id,))}
    seen = pool | earlier_sheet_ids(store, corpus, run_id)
    http = Http(store, run_id)
    new: list[int] = []
    for source in todo:
        sent = query.render(tree, DIALECT.get(source, "openalex"))
        try:
            result = search_source(source, http, sent, protocol["pilot"]["max_records_per_source"], settings)
        except (ApiError, ValueError) as err:
            store.set_run_source(run_id, source, sent, None, 0, "failed", str(err))
            log(f"  {source}: FAILED ({err})")
            continue
        result.notes = "; ".join(x for x in (result.notes, f"searched {now()[:10]}, after the run's other sources")
                                 if x)
        _store_result(store, run_id, result)
        ids = [r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? AND source = ?",
                                              (run_id, source))]
        fresh = [w for w in ids if w not in seen and w not in new]
        new += fresh
        log(f"  {source}: {result.reported_total} reported, {len(result.records)} fetched, {len(fresh)} new to the run")
    for entry in entries:
        source = entry["source"]
        lacking = _without_abstract(store, run_id)
        try:
            result = imports.load_entry(entry, tree, root, store, run_id)
        except (OSError, ValueError, KeyError) as err:
            store.set_run_source(run_id, source, str(entry.get("query", "")), None, 0, "failed", str(err))
            log(f"  {source}: FAILED ({err})")
            continue
        _store_result(store, run_id, result)
        filled = len(lacking - _without_abstract(store, run_id))
        ids = [r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? AND source = ?",
                                              (run_id, source))]
        fresh = [w for w in ids if w not in seen and w not in new]
        new += fresh
        if stats is not None:
            stats["abstracts"] = stats.get("abstracts", 0) + filled
        log(f"  {source}: {len(result.records)} records from the export, {len(fresh)} new to the run, "
            f"{filled} records of the run gained an abstract")
    store.add_sample(run_id, sorted(new), SAMPLE_PURPOSE)
    out_dir = root / "output" / f"corpus_{corpus.upper()}" / f"run_{run_id:03d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    write_report(store, run_id, out_dir / "report.md")
    return sorted(new)


def _without_abstract(store: Store, run_id: int) -> set[int]:
    return {r[0] for r in store.db.execute(
        "SELECT DISTINCT w.work_id FROM retrievals r JOIN works w ON w.work_id = r.work_id WHERE r.run_id = ? "
        "AND length(trim(coalesce(w.abstract, ''))) <= 50", (run_id,))}


def _store_result(store: Store, run_id: int, result) -> None:
    for rank, rec in enumerate(result.records, 1):
        work_id = store.upsert_work(rec)
        store.add_retrieval(run_id, work_id, result.source, rec.get("source_id"), rank)
    truncated = result.reported_total is not None and result.reported_total > len(result.records)
    store.set_run_source(run_id, result.source, result.query, result.reported_total, len(result.records),
                         "truncated" if truncated else "ok", notes=result.notes)


def earlier_sheet_ids(store: Store, corpus: str, run_id: int) -> set[int]:
    return {r["work_id"] for r in store.db.execute(
        """SELECT s.work_id FROM samples s JOIN runs r ON r.run_id = s.run_id
           WHERE r.corpus = ? AND s.purpose = ? AND s.run_id < ?""", (corpus, SAMPLE_PURPOSE, run_id))}


def write_sheet(store: Store, run_id: int, work_ids: list[int], tree, path: Path, sep: str) -> None:
    found = {}
    for r in store.db.execute("SELECT work_id, source FROM retrievals WHERE run_id = ? ORDER BY source", (run_id,)):
        found.setdefault(r["work_id"], []).append(r["source"])
    # utf-8-sig so Excel reads accents correctly; the separator follows the Windows list separator.
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh, delimiter=sep)
        writer.writerow(SHEET_COLUMNS)
        for work_id in work_ids:
            w = store.db.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
            text = f"{w['title'] or ''} {w['abstract'] or ''}"
            writer.writerow([
                w["work_id"], "+".join(found.get(work_id, [])),
                "yes" if query.matches(tree, text) else "no",
                w["year"] or "", _clean(w["title"]), _clean(w["abstract"]), w["venue"] or "", work_link(w),
                "", "", "",
            ])


def import_screening(csv_path: Path, settings: Settings, run_id: int | None = None,
                     screener: str = "human:reviewer", log=print) -> dict:
    run_id = run_id or _run_from_path(csv_path)
    store = Store(settings.db_path)
    run = store.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise SystemExit(f"No run {run_id} in {settings.db_path}")
    # Validate against the protocol this run used, not the file as it is today.
    content = store.db.execute("SELECT content FROM protocols WHERE sha256 = ?",
                               (run["protocol_sha256"],)).fetchone()["content"]
    rules = tomllib.loads(content)["screening"]
    sampled = {r["work_id"] for r in store.db.execute(
        "SELECT work_id FROM samples WHERE run_id = ? AND purpose = ?", (run_id, SAMPLE_PURPOSE))}

    raw = _read_sheet(csv_path)
    header = raw.splitlines()[0] if raw else ""
    sep = max(";,\t", key=header.count)
    counts = {"imported": 0, "blank": 0, "invalid": 0}
    problems = []
    for line_no, row in enumerate(csv.DictReader(io.StringIO(raw, newline=""), delimiter=sep), start=2):
        decision = (row.get("decision") or "").strip().lower()
        reason = (row.get("exclude_reason") or "").strip().lower() or None
        if not decision:
            counts["blank"] += 1
            continue
        try:
            work_id = int(row["work_id"])
        except (KeyError, ValueError):
            work_id = None
        error = None
        if work_id not in sampled:
            error = f"work_id {row.get('work_id')!r} is not on run {run_id}'s sheet"
        elif decision not in rules["decisions"]:
            error = f"decision {decision!r} not in {rules['decisions']}"
        elif decision == "exclude" and reason not in rules["exclude_reasons"]:
            error = f"exclude needs a reason from {rules['exclude_reasons']}, got {reason!r}"
        if error:
            counts["invalid"] += 1
            problems.append(f"line {line_no}: {error}")
            continue
        store.add_screening(run_id, work_id, run["corpus"], rules["stage"], screener, decision,
                            reason if decision == "exclude" else None, (row.get("note") or "").strip() or None)
        counts["imported"] += 1

    for p in problems[:20]:
        log("  " + p)
    out = csv_path.parent / "report.md"
    write_report(store, run_id, out)
    store.close()
    log(f"Imported {counts['imported']}, blank {counts['blank']}, invalid {counts['invalid']}. Report: {out}")
    return counts


def _read_sheet(path: Path) -> str:
    # Excel's plain "CSV" save uses the Windows code page, "CSV UTF-8" does not.
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="cp1252")


def _clean(text: str | None) -> str:
    return " ".join(text.split()) if text else ""


def _run_from_path(path: Path) -> int:
    m = re.search(r"run_(\d+)", str(path))
    if not m:
        raise SystemExit("Pass --run: the run number is not in the sheet's folder name")
    return int(m.group(1))
