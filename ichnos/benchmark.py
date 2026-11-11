"""The validation benchmark, tests A1 and A2 (docs/design-validation-and-forecasting.md).

A1, screening: a published review's labelled records (included or excluded at title and abstract, e.g. from SYNERGY)
are imported into a benchmark run of their own. The run gets a protocol written from the review's own criteria, so the
existing screening agents (`agent-tasks screen <run>`) judge the records exactly as in any review. The labels are kept
in `bench_labels`, never in the records or the tasks, so no agent can see them. `score` then compares each screener,
and the combined decision Ichnos keeps, with the labels: recall with a 95% interval, precision, specificity, Cohen's
kappa, and every inclusion that was missed.

A2, retrieval: a review's list of included studies is matched to an existing Ichnos run, and recall is counted at each
stage: retrieved by the run's searches, on its screening sheet, kept, and with findings.

Thresholds come from the corpus protocol's [benchmark] table and are frozen with the protocol's hash at import, before
any agent has seen a record. A benchmark's file is hashed too: importing a changed file under the same name is refused.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from pathlib import Path

from .agreement import cohen_kappa
from .config import ROOT, load_protocol
from .store import Store, normalize_doi, now

TITLE_ABSTRACT = "title_abstract"
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
INCLUDE = {"1", "include", "included", "yes", "true", "i"}
EXCLUDE = {"0", "exclude", "excluded", "no", "false", "e"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS bench_sets (
    bench       TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,                   -- screening (A1), retrieval (A2)
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    corpus      TEXT NOT NULL,
    source      TEXT,                            -- where the labels come from, e.g. SYNERGY:vandeSchoot2018
    file_name   TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    protocol_sha256 TEXT,                        -- the corpus protocol as it was at import (A1)
    thresholds  TEXT NOT NULL,                   -- JSON, frozen at import
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bench_labels (
    bench       TEXT NOT NULL,
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    label       TEXT NOT NULL CHECK (label IN ('include', 'exclude')),
    ref         TEXT,                            -- the record's id in the source file
    PRIMARY KEY (bench, work_id)
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


# Reading the files

def read_rows(path: Path) -> tuple[list[dict], str]:
    """Rows of a CSV (comma, semicolon or tab), with lower-case column names, and the file's sha256."""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    header = text.split("\n", 1)[0]          # the header alone: abstracts are full of commas and semicolons
    delimiter = max(",;\t", key=header.count)
    rows = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text), delimiter=delimiter)):
        if None in r:
            raise ValueError(f"row {i + 2} has more fields than the header; check the delimiter and quoting")
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in r.items()})
    return rows, hashlib.sha256(raw).hexdigest()


def _record(r: dict) -> dict:
    doi = normalize_doi(r.get("doi") or None)
    year = r.get("year") or r.get("publication_year")
    authors = [a.strip() for a in re.split(r";|\|", r.get("authors", "")) if a.strip()]
    return {"doi": doi, "title": r.get("title") or None, "abstract": r.get("abstract") or None,
            "year": int(year) if year and year.isdigit() else None, "venue": r.get("venue") or r.get("journal") or None,
            "authors": authors or None, "openalex_id": (r.get("openalex_id") or "").rsplit("/", 1)[-1] or None}


def _label(r: dict, line: int) -> str:
    v = (r.get("label") or r.get("label_included") or r.get("included") or "").strip().lower()
    if v in INCLUDE:
        return "include"
    if v in EXCLUDE:
        return "exclude"
    raise ValueError(f"row {line}: label {v!r} is neither include nor exclude")


def _existing(store: Store, name: str, sha: str):
    row = store.db.execute("SELECT * FROM bench_sets WHERE bench = ?", (name,)).fetchone()
    if row and row["file_sha256"] != sha:
        raise ValueError(f"benchmark {name} was imported from a different file; give the new file a new name")
    return row


def thresholds(protocol: dict) -> dict:
    b = protocol.get("benchmark") or {}
    return {k: b[k] for k in ("recall_min", "precision_min", "kappa_min") if k in b} or {"recall_min": 0.95}


COLUMNS = ["id", "openalex_id", "doi", "pmid", "title", "abstract", "year", "venue", "authors", "label"]


def from_ids(path: Path, out: Path, http, api_key: str | None = None, contact_email: str | None = None) -> dict:
    """A label file of ids only (SYNERGY's <review>_ids.csv: doi, openalex_id, label_included) as a labelled set
    with titles and abstracts from OpenAlex, ready for import_labels. Rows with neither id cannot be shown to a
    screener and are left out, and counted; so are rows OpenAlex no longer returns."""
    from .sources import openalex
    rows, _ = read_rows(path)
    labels = [_label(r, i + 2) for i, r in enumerate(rows)]
    oa_ids = sorted({r["openalex_id"].rsplit("/", 1)[-1] for r in rows if r.get("openalex_id")})
    dois = sorted({d for r in rows if not r.get("openalex_id") and (d := normalize_doi(r.get("doi") or None))})
    by_id = openalex.works(http, "openalex", oa_ids, api_key, contact_email)
    by_doi = openalex.works(http, "doi", dois, api_key, contact_email) if dois else {}
    written, lost = [], {"include": 0, "exclude": 0}
    for r, label in zip(rows, labels):
        oa = (r.get("openalex_id") or "").rsplit("/", 1)[-1]
        rec = by_id.get(oa) if oa else by_doi.get(normalize_doi(r.get("doi") or None) or "")
        if not rec or not rec.get("title"):
            lost[label] += 1
            continue
        written.append({"id": rec["openalex_id"], "openalex_id": rec["openalex_id"], "doi": rec["doi"] or "",
                        "pmid": rec.get("pmid") or "", "title": rec["title"], "abstract": rec["abstract"] or "",
                        "year": rec["year"] or "", "venue": rec["venue"] or "",
                        "authors": "; ".join(rec["authors"] or []), "label": label})
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(written)
    return {"rows": len(rows), "written": len(written), "included": sum(x["label"] == "include" for x in written),
            "no_abstract": sum(not x["abstract"] for x in written), "lost": lost, "file": out}


# A1: screening

def import_labels(store: Store, corpus: str, path: Path, name: str, source: str | None = None,
                  root: Path = ROOT) -> dict:
    """A labelled set as a benchmark run of the corpus. Needs title (and ideally abstract) and a label per row."""
    ensure(store)
    if not NAME.match(name):
        raise ValueError("name: lower-case letters, digits and hyphens, e.g. synergy-vandeschoot2018")
    rows, sha = read_rows(path)
    if _existing(store, name, sha):
        raise ValueError(f"benchmark {name} is already imported")
    if not rows:
        raise ValueError("the file has no rows")
    if "label" not in rows[0] and "label_included" not in rows[0] and "included" not in rows[0]:
        raise ValueError("the file needs a `label` column (include/exclude or 1/0)")
    ppath, text, protocol = load_protocol(corpus, root)
    if not (protocol.get("screening") or {}).get("metrics"):
        raise ValueError(f"{ppath.name} has no [[screening.metrics]]: write the review's criteria there first")
    labels = [(_record(r), _label(r, i), r.get("id") or r.get("record_id") or str(i))
              for i, r in enumerate(rows, 2)]
    if any(not rec["title"] for rec, _, _ in labels):
        raise ValueError("every row needs a title")
    proto_sha = store.add_protocol(corpus, ppath.relative_to(root).as_posix(), text)
    run_id = store.start_run("benchmark", corpus, proto_sha, {"benchmark": name, "kind": "screening",
                                                             "file_sha256": sha})
    seen: dict[int, tuple[str, str]] = {}
    duplicates = 0
    for rank, (rec, label, ref) in enumerate(labels, 1):
        w = store.upsert_work(rec)
        if w in seen:
            duplicates += 1
            if label == "include":        # the same work labelled both ways: keep the inclusion, which is conservative
                seen[w] = ("include", seen[w][1])
            continue
        seen[w] = (label, ref)
        store.add_retrieval(run_id, w, "benchmark", ref, rank)
    with store.db:
        store.db.executemany("INSERT INTO bench_labels VALUES (?, ?, ?, ?)",
                             [(name, w, label, ref) for w, (label, ref) in seen.items()])
    store.add_sample(run_id, sorted(seen), "pilot_screening")
    with store.db:
        store.db.execute("INSERT INTO bench_sets VALUES (?, 'screening', ?, ?, ?, ?, ?, ?, ?, ?)",
                         (name, run_id, corpus, source, path.name, sha, proto_sha,
                          json.dumps(thresholds(protocol)), now()))
    store.finish_run(run_id, "ok", f"benchmark {name}: {len(seen)} records")
    inc = sum(1 for lb, _ in seen.values() if lb == "include")
    return {"bench": name, "run_id": run_id, "records": len(seen), "included": inc, "duplicates": duplicates}


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float] | None:
    if n == 0:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def confusion(truth: dict[int, str], decided: dict[int, bool]) -> dict:
    """truth: work -> include/exclude; decided: work -> kept? Only works with a decision count."""
    tp = sum(1 for w, k in decided.items() if k and truth[w] == "include")
    fn = sum(1 for w, k in decided.items() if not k and truth[w] == "include")
    fp = sum(1 for w, k in decided.items() if k and truth[w] == "exclude")
    tn = sum(1 for w, k in decided.items() if not k and truth[w] == "exclude")
    pairs = [("include" if decided[w] else "exclude", truth[w]) for w in decided]
    out = {"tp": tp, "fn": fn, "fp": fp, "tn": tn, "n": len(decided),
           "recall": tp / (tp + fn) if tp + fn else None, "recall_ci": wilson(tp, tp + fn),
           "precision": tp / (tp + fp) if tp + fp else None,
           "specificity": tn / (tn + fp) if tn + fp else None,
           "kappa": cohen_kappa(pairs) if pairs else None}
    return out


def score_screening(store: Store, name: str) -> dict:
    from .agents import kept_ids
    b = _bench(store, name, "screening")
    run_id = b["run_id"]
    truth = {r["work_id"]: r["label"] for r in store.db.execute("SELECT work_id, label FROM bench_labels WHERE bench "
                                                                "= ?", (name,))}
    per: dict[str, dict[int, bool]] = {}
    for r in store.db.execute("SELECT work_id, screener, decision FROM screening WHERE run_id = ? AND stage = ?",
                              (run_id, TITLE_ABSTRACT)):
        if r["work_id"] in truth:
            per.setdefault(r["screener"], {})[r["work_id"]] = r["decision"] != "exclude"
    screened = set().union(*[set(d) for d in per.values()]) if per else set()
    kept = set(kept_ids(store, run_id, stage=TITLE_ABSTRACT))
    combined = {w: w in kept for w in screened}
    result = {"bench": name, "run_id": run_id, "records": len(truth),
              "included": sum(1 for v in truth.values() if v == "include"), "screened": len(screened),
              "screeners": {s: confusion(truth, d) for s, d in sorted(per.items())},
              "combined": confusion(truth, combined) if combined else None,
              "thresholds": json.loads(b["thresholds"])}
    missed = [w for w, k in combined.items() if not k and truth[w] == "include"]
    result["missed"] = _cites(store, missed)
    result["verdict"] = _verdict(result["combined"], result["thresholds"], len(screened), len(truth))
    return result


def _verdict(c: dict | None, th: dict, screened: int, total: int) -> str:
    if not c or screened < total:
        return f"incomplete: {screened} of {total} records screened"
    fails = []
    if "recall_min" in th and (c["recall"] is None or c["recall"] < th["recall_min"]):
        fails.append(f"recall {c['recall']:.3f} < {th['recall_min']}" if c["recall"] is not None else "no inclusions")
    if "precision_min" in th and (c["precision"] or 0) < th["precision_min"]:
        fails.append(f"precision {c['precision'] or 0:.3f} < {th['precision_min']}")
    if "kappa_min" in th and (c["kappa"] if c["kappa"] is not None else -1) < th["kappa_min"]:
        fails.append(f"kappa < {th['kappa_min']}")
    return "meets every threshold" if not fails else "fails: " + "; ".join(fails)


# A2: retrieval

def import_reference(store: Store, run_id: int, path: Path, name: str, source: str | None = None) -> dict:
    """A review's included studies, matched to an existing run (by DOI, else title and year). Works not yet in the
    store are added as records, so they count as not retrieved."""
    ensure(store)
    if not NAME.match(name):
        raise ValueError("name: lower-case letters, digits and hyphens")
    run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"no run {run_id}")
    rows, sha = read_rows(path)
    if _existing(store, name, sha):
        raise ValueError(f"benchmark {name} is already imported")
    recs = [(_record(r), r.get("id") or str(i)) for i, r in enumerate(rows, 2)]
    if any(not (rec["doi"] or rec["title"]) for rec, _ in recs):
        raise ValueError("every row needs a doi or a title")
    added = 0
    with store.db:
        store.db.execute("INSERT INTO bench_sets VALUES (?, 'retrieval', ?, ?, ?, ?, ?, NULL, ?, ?)",
                         (name, run_id, run["corpus"], source, path.name, sha, json.dumps({}), now()))
    for rec, ref in recs:
        before = store.find_work(rec)
        w = store.upsert_work(rec)
        added += before is None
        with store.db:
            store.db.execute("INSERT OR IGNORE INTO bench_labels VALUES (?, ?, 'include', ?)", (name, w, ref))
    n = store.db.execute("SELECT count(*) FROM bench_labels WHERE bench = ?", (name,)).fetchone()[0]
    return {"bench": name, "run_id": run_id, "studies": n, "new_to_store": added}


def score_retrieval(store: Store, name: str) -> dict:
    from .agents import kept_ids
    b = _bench(store, name, "retrieval")
    run_id = b["run_id"]
    ref = [r[0] for r in store.db.execute("SELECT work_id FROM bench_labels WHERE bench = ? ORDER BY work_id", (name,))]
    retrieved = {r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ?", (run_id,))}
    sheet = {r[0] for r in store.db.execute("SELECT work_id FROM samples WHERE run_id = ? AND purpose = "
                                            "'pilot_screening'", (run_id,))}
    kept = set(kept_ids(store, run_id))
    with_findings = {r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM findings WHERE run_id = ? AND "
                                                    "status != 'superseded'", (run_id,))} \
        if _has_status(store) else {r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM findings WHERE "
                                                                    "run_id = ?", (run_id,))}
    stages = {}
    for stage, have in (("retrieved", retrieved), ("on the sheet", sheet), ("kept", kept),
                        ("with findings", with_findings)):
        k = sum(1 for w in ref if w in have)
        stages[stage] = {"found": k, "of": len(ref), "recall": k / len(ref) if ref else None,
                         "recall_ci": wilson(k, len(ref)), "missing": _cites(store, [w for w in ref if w not in have])}
    return {"bench": name, "run_id": run_id, "studies": len(ref), "stages": stages}


def _has_status(store: Store) -> bool:
    return "status" in {r["name"] for r in store.db.execute("PRAGMA table_info(findings)")}


# Shared

def _bench(store: Store, name: str, kind: str):
    ensure(store)
    b = store.db.execute("SELECT * FROM bench_sets WHERE bench = ?", (name,)).fetchone()
    if b is None:
        raise ValueError(f"no benchmark {name}")
    if b["kind"] != kind:
        raise ValueError(f"benchmark {name} is a {b['kind']} benchmark")
    return b


def _cites(store: Store, ids: list[int]) -> list[dict]:
    if not ids:
        return []
    rows = store.db.execute(f"SELECT work_id, title, year, doi FROM works WHERE work_id IN ({','.join('?' * len(ids))})"
                            " ORDER BY work_id", ids)
    return [dict(r) for r in rows]


def score(store: Store, name: str) -> dict:
    ensure(store)
    row = store.db.execute("SELECT kind FROM bench_sets WHERE bench = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(f"no benchmark {name}")
    return score_screening(store, name) if row["kind"] == "screening" else score_retrieval(store, name)


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.1%}"


def _ci(ci) -> str:
    return "" if not ci else f" [{ci[0]:.1%}, {ci[1]:.1%}]"


def report(result: dict) -> str:
    if "stages" in result:
        lines = [f"# Benchmark {result['bench']}: retrieval against a review's included studies", "",
                 f"Run {result['run_id']}; {result['studies']} included studies in the reference list.", "",
                 "| Stage | Found | Recall [95% CI] |", "| --- | ---: | --- |"]
        for stage, s in result["stages"].items():
            lines.append(f"| {stage} | {s['found']} of {s['of']} | {_pct(s['recall'])}{_ci(s['recall_ci'])} |")
        miss = result["stages"]["retrieved"]["missing"]
        lines += ["", f"## Not retrieved ({len(miss)})", ""]
        lines += [f"- #{m['work_id']} {m['title'] or ''} ({m['year'] or 'n.d.'}) {m['doi'] or ''}" for m in miss]
        return "\n".join(lines) + "\n"
    lines = [f"# Benchmark {result['bench']}: screening against a review's labels", "",
             f"Run {result['run_id']}; {result['records']} records, {result['included']} included by the review; "
             f"{result['screened']} screened so far.", "",
             f"**Verdict:** {result['verdict']} (thresholds frozen at import: "
             f"{', '.join(f'{k} {v}' for k, v in result['thresholds'].items())}).", "",
             "| Screener | Screened | Recall [95% CI] | Precision | Specificity | Kappa | Missed inclusions |",
             "| --- | ---: | --- | ---: | ---: | ---: | ---: |"]
    rows = list(result["screeners"].items()) + ([("combined (what Ichnos keeps)", result["combined"])]
                                                if result["combined"] else [])
    for s, c in rows:
        kappa = "–" if c["kappa"] is None else f"{c['kappa']:.2f}"
        lines.append(f"| {s} | {c['n']} | {_pct(c['recall'])}{_ci(c['recall_ci'])} | {_pct(c['precision'])} | "
                     f"{_pct(c['specificity'])} | {kappa} | {c['fn']} |")
    lines += ["", f"## Inclusions Ichnos missed ({len(result['missed'])})", ""]
    lines += [f"- #{m['work_id']} {m['title'] or ''} ({m['year'] or 'n.d.'}) {m['doi'] or ''}" for m in result["missed"]]
    return "\n".join(lines) + "\n"


def write_report(store: Store, name: str, root: Path = ROOT) -> Path:
    result = score(store, name)
    out = root / "output" / "benchmarks"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{name}.md"
    path.write_text(report(result), encoding="utf-8")
    path.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return path
