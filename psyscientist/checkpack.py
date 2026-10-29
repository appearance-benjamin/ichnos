"""Check packs: what a person must verify in an AI-assisted review, as one HTML file anyone can open.

An editor's condition for such a review is that a person checks it. A pack holds three parts:

1. findings: every finding behind the headline claims of the latest manuscript, with its study, a link and the
   verbatim passage; the checker marks each confirmed or corrected, against the passage or the full text;
2. core: every core study with what its charting says on each inclusion criterion; the checker says whether it
   meets them (a study a person says fails leaves the core);
3. screening: a seeded random sample of screened records, stratified by the agents' decisions but shown without
   them; the checker screens them against the inclusion rule, and agreement with each agent is computed on import;
4. coding (on request): chosen studies, in a seeded order, with exactly the text the coders saw; the checker codes
   the given charting dimensions without seeing any coder's codes, and agreement with the primary coding and with
   the second coder is computed on import.

The page needs no installation and no server: it opens from disk in any browser, keeps progress in that browser,
and "Download my checks" saves a JSON file. The review's owner imports it with
`python -m psyscientist import-checks <file>`; finding checks go to the same store as the app's Check findings tab,
so the Declaration of AI use and the next revision follow them.
"""

from __future__ import annotations

import hashlib
import html
import json
import random
import re
from collections import Counter
from pathlib import Path

from . import agreement, checks, tables
from .config import ROOT
from .store import Store, now

FORMAT = "psyscientist-checks/1"
VERDICTS = {"meets": "meets every inclusion criterion", "fails": "fails at least one criterion",
            "cannot_tell": "cannot tell from what I can access"}
DECISIONS = {"include": "include", "exclude": "exclude", "unsure": "unsure"}
SAMPLE_PURPOSE = "calibration_screening"

SCHEMA = """
CREATE TABLE IF NOT EXISTS check_packs (
    pack_id     TEXT PRIMARY KEY,
    run_id      INTEGER NOT NULL,
    paper_id    INTEGER,
    created_at  TEXT NOT NULL,
    content     TEXT NOT NULL                    -- what the pack shows, plus the strata it does not show
);
CREATE TABLE IF NOT EXISTS core_checks (
    check_id    INTEGER PRIMARY KEY,
    pack_id     TEXT,
    run_id      INTEGER NOT NULL,
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    checker     TEXT NOT NULL,                   -- human:<name>
    verdict     TEXT NOT NULL CHECK (verdict IN ('meets', 'fails', 'cannot_tell')),
    criterion   TEXT,                            -- the criterion it fails, when it fails
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS core_checks_work ON core_checks(run_id, work_id);
CREATE TABLE IF NOT EXISTS coding_checks (
    check_id    INTEGER PRIMARY KEY,
    pack_id     TEXT,
    run_id      INTEGER NOT NULL,
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    checker     TEXT NOT NULL,                   -- human:<name>
    dimension   TEXT NOT NULL,
    canonical   TEXT NOT NULL,                   -- the categories chosen, separated by '; '
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS coding_checks_work ON coding_checks(run_id, work_id);
CREATE TABLE IF NOT EXISTS pack_imports (
    import_id   INTEGER PRIMARY KEY,
    pack_id     TEXT NOT NULL,
    checker     TEXT NOT NULL,
    file_sha256 TEXT NOT NULL,
    counts      TEXT,
    created_at  TEXT NOT NULL
);
"""


def ensure(store: Store) -> None:
    if getattr(store, "_check_packs_ready", False):
        return
    store.db.executescript(SCHEMA)
    checks.ensure(store)
    store._check_packs_ready = True


def _clip(text, n: int = 6000) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _link(w: dict) -> str:
    return f"https://doi.org/{w['doi']}" if w.get("doi") else (w.get("oa_url") or "")


# What goes in a pack

CODE_FIELDS = ("candidate_rule", "direction", "context", "evidence_type", "outcome", "modern_input",
               "population_stage", "focal_sex", "readiness_input", "status_measure", "kin_type")
ONE_RULE = {"readiness_input", "status_measure", "kin_type"}      # not shown where the finding is on another rule


def _codes(fields: dict) -> str:
    return " · ".join(f"{k.replace('_', ' ')}: {str(fields[k]).replace('_', ' ')}" for k in CODE_FIELDS
                      if fields.get(k) and not (k in ONE_RULE and fields[k] == "not_applicable"))


def _findings(store: Store, row, protocol: dict, sample: int = 0, seed: int = 0) -> list[dict]:
    """The findings behind the headline claims and, with `sample`, a stratified random sample of the other findings
    the paper counts, each with the codes the counts use."""
    items = checks.headline_findings(store, row, protocol)
    if sample:
        items += checks.sample_findings(store, row, protocol, sample, seed, skip={i["id"] for i in items})
    out = []
    for i in items:
        codes = _codes(i.get("fields") or {})
        out.append({"id": i["id"], "work_id": i["work"], "cite": i["cite"], "title": i["title"], "link": i["link"],
                    "fulltext": bool(i["fulltext"]), "finding": i["finding"], "effect": i["effect"] or "",
                    "for_whom": i["for_whom"] or "", "evidence": i["evidence"], "location": i["location"] or "",
                    "source": i["source"], "codes": codes,
                    "uses": [{"part": u["part"], "text": u["text"]} for u in i["uses"]]})
    return out


def _core(store: Store, run_id: int, protocol: dict) -> list[dict]:
    crit = tables.criteria(protocol)
    if not crit:
        return []
    core = [e for e in tables.eligibility(store, run_id, protocol) if e["core"]]
    meta = tables.works(store, [e["work_id"] for e in core], run_id)
    chart = tables.charting(store, run_id)
    out = []
    for e in sorted(core, key=lambda e: e["cite"]):
        w, c = meta[e["work_id"]], chart.get(e["work_id"], {})
        rows = []
        for k in crit:
            d = c.get(k.get("dimension") or "", {})
            rows.append({"id": k["id"], "label": k.get("label", k["id"]),
                         "charted": ", ".join(v.replace("_", " ") for v in d.get("values", [])) or "not charted",
                         "quote": _clip(d.get("text"), 600)})
        ft = store.get_fulltext(e["work_id"])
        out.append({"work_id": e["work_id"], "cite": e["cite"], "title": w["title"], "year": w["year"],
                    "venue": w["venue"] or "", "link": _link(w), "abstract": _clip(w["abstract"]),
                    "fulltext": bool(ft and ft["status"] == "parsed"), "criteria": rows})
    return out


def _screening(store: Store, run_id: int, n: int, seed: int) -> tuple[list[dict], dict[int, str]]:
    """A random sample of records screened at title and abstract, stratified by the agents' decisions (both
    excluded, both kept, disagreed), equal numbers per stratum as far as each has records; records a person has
    already decided are left out."""
    from .agents import sheet_ids
    calls: dict[int, dict[str, bool]] = {}
    human = set()
    for r in store.db.execute("SELECT work_id, screener, decision FROM screening WHERE run_id = ? AND "
                              "stage = 'title_abstract'", (run_id,)):
        if r["screener"].startswith("human:"):
            human.add(r["work_id"])
        else:
            calls.setdefault(r["work_id"], {})[r["screener"]] = r["decision"] != "exclude"
    strata: dict[str, list[int]] = {"excluded": [], "kept": [], "disagreed": []}
    for w in sheet_ids(store, run_id):
        if w in human or w not in calls:
            continue
        v = set(calls[w].values())
        strata["disagreed" if len(v) > 1 else "kept" if v == {True} else "excluded"].append(w)
    rng = random.Random(seed)
    for ids in strata.values():
        rng.shuffle(ids)
    picked: dict[int, str] = {}
    want = {k: n // 3 + (1 if i < n % 3 else 0) for i, k in enumerate(strata)}
    for k, ids in strata.items():
        for w in ids[:want[k]]:
            picked[w] = k
    spare = [(k, w) for k, ids in strata.items() for w in ids[want[k]:]]
    rng.shuffle(spare)
    for k, w in spare[:max(0, n - len(picked))]:
        picked[w] = k
    meta = tables.works(store, picked)
    order = list(picked)
    rng.shuffle(order)                                   # the page shows no strata and no order by stratum
    items = [{"work_id": w, "title": meta[w]["title"], "year": meta[w]["year"], "venue": meta[w]["venue"] or "",
              "abstract": _clip(meta[w]["abstract"])} for w in order]
    return items, picked


def _coded_records(root: Path, corpus: str, run_id: int, dims: list[str]) -> dict[int, dict]:
    """The records the primary coders of these dimensions saw (title, abstract, full-text sections, byline), from
    the latest extraction task that charted exactly them; {} when there is none."""
    import json as _json
    folders = []
    for t in (root / "output" / f"corpus_{corpus}" / f"run_{run_id:03d}" / "agents").glob("extract__*/task.json"):
        meta = _json.loads(t.read_text(encoding="utf-8"))
        if sorted(meta.get("dims") or []) == sorted(dims) and not meta.get("second_coder"):
            folders.append(t.parent)
    out: dict[int, dict] = {}
    for folder in sorted(folders, key=lambda f: f.stat().st_mtime):
        for b in sorted(folder.glob("batch_[0-9][0-9].json")):
            for r in _json.loads(b.read_text(encoding="utf-8")):
                out[int(r["work_id"])] = r
    return out


def _coding(store: Store, run_id: int, protocol: dict, works: list[int], dims: list[str], seed: int,
            root: Path = ROOT) -> tuple[list[dict], list[dict]]:
    """The studies to code blind, in a seeded order, each with the text its coders saw, and the dimensions with their
    questions and categories."""
    spec = {d["name"]: d for d in (protocol.get("map") or {}).get("dimensions") or []}
    unknown = [d for d in dims if d not in spec or not spec[d].get("categories")]
    if unknown:
        raise ValueError(f"no map dimension with categories named {unknown}")
    run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    seen = _coded_records(root, run["corpus"], run_id, dims)
    meta = tables.works(store, works, run_id)
    missing = [w for w in works if w not in meta]
    if missing:
        raise ValueError(f"no such works: {missing}")
    order = list(works)
    random.Random(seed).shuffle(order)                   # the page shows no reason for picking a study
    items = []
    for w in order:
        m, r = meta[w], seen.get(w, {})
        items.append({"work_id": w, "cite": m["cite"], "title": m["title"], "year": m["year"], "venue": m["venue"] or "",
                      "link": _link(m), "abstract": _clip(r.get("abstract") if r else m["abstract"]),
                      "fulltext": _clip(r.get("fulltext"), 20000), "byline": _clip(r.get("byline"), 3000),
                      "seen": "the text the coders saw" if r else "title and abstract from the store"})
    questions = [{"name": d, "label": spec[d].get("label") or d.replace("_", " "),
                  "question": " ".join(str(spec[d].get("question") or "").split()),
                  "categories": list(spec[d]["categories"])} for d in dims]
    return items, questions


def build(store: Store, run_id: int, protocol: dict, paper_id: int | None = None, sample: int = 60,
          seed: int | None = None, parts=("findings", "core", "screening"), coding_works: list[int] | None = None,
          coding_dims: list[str] | None = None, coding_strata: dict[int, str] | None = None,
          coding_note: str = "", root: Path = ROOT, findings_sample: int = 0) -> dict:
    """The pack's content; `stored` adds the screening strata, which the page must not show. `findings_sample` adds a
    stratified random sample of the paper's other findings to the headline ones."""
    ensure(store)
    run = store.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"no run {run_id}")
    from . import paper as paper_mod
    paper_mod.ensure(store)
    row = (store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone() if paper_id
           else checks.latest_paper(store, run_id))
    seed = seed if seed is not None else int(hashlib.sha256(f"{run_id}:{now()}".encode()).hexdigest()[:8], 16)
    data = {"format": FORMAT, "corpus": run["corpus"], "run_id": run_id, "title": protocol.get("title", ""),
            "paper_id": row["paper_id"] if row else None, "version": row["version"] if row else None,
            "paper_title": row["title"] if row else None, "created": now()[:10], "seed": seed,
            "headline": checks.headline_names(protocol, row),
            "statuses": {k: f"{k.capitalize()}: {v}" for k, v in checks.STATUSES.items()},
            "against": checks.AGAINST,
            "verdicts": {k: v[:1].upper() + v[1:] for k, v in VERDICTS.items()},
            "decisions": {k: v.capitalize() for k, v in DECISIONS.items()},
            "rule": " ".join(str((protocol.get("screening") or {}).get("rule") or "").split()),
            "criteria": [{"id": c["id"], "label": c.get("label", c["id"])} for c in tables.criteria(protocol)],
            "findings": [], "core": [], "screening": [], "coding": [], "coding_dims": [], "coding_note": ""}
    strata: dict[int, str] = {}
    if "findings" in parts and row is not None:
        data["findings"] = _findings(store, row, protocol, findings_sample, seed)
    if "core" in parts:
        data["core"] = _core(store, run_id, protocol)
    if "screening" in parts and sample > 0:
        data["screening"], strata = _screening(store, run_id, sample, seed)
    if "coding" in parts:
        if not coding_works:
            raise ValueError("the coding part needs the studies to code (--coding-works)")
        if not coding_dims:
            from . import patterns
            patterns.ensure(store)
            coding_dims = [r[0] for r in store.db.execute(
                "SELECT DISTINCT dimension FROM second_codings WHERE run_id = ? ORDER BY dimension", (run_id,))]
            order = [d["name"] for d in (protocol.get("map") or {}).get("dimensions") or []]
            coding_dims = sorted(coding_dims, key=order.index)
        if not coding_dims:
            raise ValueError("say which dimensions to code (--coding-dims)")
        data["coding"], data["coding_dims"] = _coding(store, run_id, protocol, coding_works, coding_dims, seed, root)
        data["coding_note"] = coding_note
    data["pack_id"] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
    stored = dict(data, strata={str(w): k for w, k in strata.items()},
                  coding_strata={str(w): k for w, k in (coding_strata or {}).items()})
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO check_packs (pack_id, run_id, paper_id, created_at, content) "
                         "VALUES (?, ?, ?, ?, ?)", (data["pack_id"], run_id, data["paper_id"], now(),
                                                    json.dumps(stored, ensure_ascii=False)))
    return data


def write(store: Store, run_id: int, protocol: dict, out_dir: Path, **kw) -> Path:
    data = build(store, run_id, protocol, **kw)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"check_pack_{data['corpus']}_run{run_id:03d}_{data['pack_id']}.html"
    path.write_text(page(data), encoding="utf-8")
    return path


# Reading a filled pack back

def find_checks_file(path: Path | None = None, downloads: Path | None = None) -> Path:
    """The checks file to import: the one given, or the newest checks_*.json in the Downloads folder; a missing file
    says where such files come from and which ones there are."""
    downloads = downloads or Path.home() / "Downloads"
    found = sorted(downloads.glob("checks_*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if path is None and found:
        return found[0]
    if path is not None and Path(path).exists():
        return Path(path)
    there = ("Checks files in " + str(downloads) + ": " + ", ".join(f.name for f in found[:5])) if found else         f"There is no checks_*.json file in {downloads} yet."
    raise FileNotFoundError(
        (f"No such file: {path}. " if path is not None else "") + there + " A checks file is made in the check pack "
        "itself: open the pack, go to 'Finish and send', enter your name and click 'Download my checks'. Then run "
        "this command again without a file name, and the newest one in Downloads is imported.")


def import_file(store: Store, path: Path, checker: str | None = None, dry_run: bool = False) -> dict:
    """Store a checker's answers: finding checks, core verdicts and blind screening decisions."""
    return import_bytes(store, Path(path).read_bytes(), checker, dry_run)


def import_bytes(store: Store, raw: bytes, checker: str | None = None, dry_run: bool = False) -> dict:
    ensure(store)
    try:
        answer = json.loads(raw.decode("utf-8-sig"))
    except ValueError as err:
        raise ValueError(f"not a checks file ({err})") from err
    if answer.get("format") != FORMAT:
        raise ValueError("not a Psych-Scientist checks file (made with 'Download my checks' in a check pack)")
    row = store.db.execute("SELECT * FROM check_packs WHERE pack_id = ?", (answer.get("pack_id"),)).fetchone()
    if row is None:
        raise ValueError(f"pack {answer.get('pack_id')} is not in this evidence store; import it into the store the "
                         "pack was made from")
    pack = json.loads(row["content"])
    name = " ".join(str(checker or answer.get("checker") or "").split())
    if not name:
        raise ValueError("the file names no checker; give one with --as")
    who = f"human:{name}"
    sha = hashlib.sha256(raw).hexdigest()
    if store.db.execute("SELECT 1 FROM pack_imports WHERE file_sha256 = ?", (sha,)).fetchone():
        raise ValueError("this file was imported already")
    findings = {f["id"]: f for f in pack["findings"]}
    core = {c["work_id"]: c for c in pack["core"]}
    screening = {s["work_id"] for s in pack["screening"]}
    coding = {c["work_id"] for c in pack.get("coding") or []}
    coding_cats = {d["name"]: set(d["categories"]) for d in pack.get("coding_dims") or []}
    crit = {c["id"] for c in pack["criteria"]}
    problems, todo = [], {"findings": [], "core": [], "screening": [], "coding": []}
    for a in answer.get("findings") or []:
        fid = int(a.get("id", -1))
        if fid not in findings:
            problems.append(f"finding {fid} is not in the pack")
        elif a.get("status"):
            if a["status"] not in checks.STATUSES:
                problems.append(f"finding {fid}: status {a['status']!r}")
            elif a.get("against") not in checks.AGAINST:
                problems.append(f"finding {fid}: say what it was checked against")
            elif a["status"] == "corrected" and not (a.get("correction") or "").strip():
                problems.append(f"finding {fid}: a correction needs what the source says")
            else:
                todo["findings"].append(a)
    for a in answer.get("core") or []:
        w = int(a.get("work_id", -1))
        if w not in core:
            problems.append(f"core study {w} is not in the pack")
        elif a.get("verdict"):
            if a["verdict"] not in VERDICTS:
                problems.append(f"core study {w}: verdict {a['verdict']!r}")
            elif a["verdict"] == "fails" and a.get("criterion") not in crit:
                problems.append(f"core study {w}: say which criterion it fails")
            else:
                todo["core"].append(a)
    for a in answer.get("screening") or []:
        w = int(a.get("work_id", -1))
        if w not in screening:
            problems.append(f"screening record {w} is not in the pack")
        elif a.get("decision"):
            if a["decision"] not in DECISIONS:
                problems.append(f"screening record {w}: decision {a['decision']!r}")
            else:
                todo["screening"].append(a)
    for a in answer.get("coding") or []:
        w = int(a.get("work_id", -1))
        codes = {d: [v for v in vs if v] for d, vs in (a.get("codes") or {}).items() if vs}
        if w not in coding:
            problems.append(f"coding study {w} is not in the pack")
        elif codes:
            bad = [f"{d}: {v}" for d, vs in codes.items() for v in vs if v not in coding_cats.get(d, set())]
            if bad:
                problems.append(f"coding study {w}: unknown codes {bad}")
            else:
                todo["coding"].append({**a, "codes": codes})
    if problems:
        raise ValueError("; ".join(problems[:12]) + (f" (and {len(problems) - 12} more)" if len(problems) > 12 else ""))
    counts = {k: len(v) for k, v in todo.items()}
    coding_agree = coding_agreement(store, row["run_id"], pack.get("coding_dims") or [],
                                    {int(a["work_id"]): {d: set(vs) for d, vs in a["codes"].items()}
                                     for a in todo["coding"]})
    agree = screening_agreement(store, row["run_id"], {int(a["work_id"]): a["decision"] for a in todo["screening"]})
    if dry_run:
        return {"checker": name, "counts": counts, "agreement": agree, "coding_agreement": coding_agree,
                "dry_run": True}
    corpus = pack["corpus"]
    for a in todo["findings"]:
        checks.record(store, int(a["id"]), who, a["status"], a.get("against"), a.get("note"), a.get("correction"),
                      pack.get("paper_id"))
    with store.db:
        for a in todo["core"]:
            store.db.execute("INSERT INTO core_checks (pack_id, run_id, work_id, checker, verdict, criterion, note, "
                             "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                             (pack["pack_id"], row["run_id"], int(a["work_id"]), who, a["verdict"],
                              a.get("criterion") if a["verdict"] == "fails" else None,
                              checks.clean_text(a.get("note")), now()))
    with store.db:
        for a in todo["coding"]:
            for d, vs in a["codes"].items():
                store.db.execute("INSERT INTO coding_checks (pack_id, run_id, work_id, checker, dimension, canonical, "
                                 "note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (pack["pack_id"], row["run_id"], int(a["work_id"]), who, d, "; ".join(sorted(vs)),
                                  checks.clean_text(a.get("note")), now()))
    for a in todo["screening"]:
        store.add_screening(row["run_id"], int(a["work_id"]), corpus, "title_abstract", who, a["decision"],
                            None, checks.clean_text(a.get("note")))
    store.add_sample(row["run_id"], [int(a["work_id"]) for a in todo["screening"]], SAMPLE_PURPOSE)
    with store.db:
        store.db.execute("INSERT INTO pack_imports (pack_id, checker, file_sha256, counts, created_at) VALUES "
                         "(?, ?, ?, ?, ?)", (pack["pack_id"], who, sha, json.dumps(counts), now()))
    return {"checker": name, "counts": counts, "agreement": agree, "coding_agreement": coding_agree}


def coding_agreement(store: Store, run_id: int, dims: list[dict], human: dict[int, dict[str, set]]) -> dict:
    """Per dimension: the person's codes against the primary coding and against the second coder, each category a
    yes/no decision per study the person coded on that dimension (Cohen's kappa over all of them, and how many
    studies matched exactly), and the categories on which they differ most."""
    from . import patterns
    patterns.ensure(store)
    first = {w: {d: set(c["values"]) for d, c in v.items()} for w, v in tables.charting(store, run_id).items()}
    second: dict[int, dict[str, set]] = {}
    for r in store.db.execute("SELECT work_id, dimension, canonical FROM second_codings WHERE run_id = ?", (run_id,)):
        second.setdefault(r["work_id"], {})[r["dimension"]] = patterns._values(r["canonical"])
    out = {}
    for d in dims:
        name, cats = d["name"], [c for c in d["categories"] if c not in patterns.UNCODED]
        row = {}
        for coder, codes in (("primary", first), ("second", second)):
            works = [w for w, h in human.items() if name in h and name in codes.get(w, {})]
            pairs = [(str(c in human[w][name]), str(c in codes[w][name])) for w in works for c in cats]
            exact = sum((human[w][name] - patterns.UNCODED) == (codes[w][name] - patterns.UNCODED) for w in works)
            k = agreement.cohen_kappa(pairs) if pairs else None
            row[coder] = {"n": len(works), "exact": exact, "kappa": None if k is None else round(k, 2)}
        out[name] = row
    return out


def screening_agreement(store: Store, run_id: int, human: dict[int, str]) -> dict[str, dict]:
    """How often each agent's keep-or-exclude call matched the person's, with Cohen's kappa."""
    out = {}
    agents: dict[str, dict[int, bool]] = {}
    for r in store.db.execute("SELECT work_id, screener, decision FROM screening WHERE run_id = ? AND stage = "
                              "'title_abstract' AND screener NOT LIKE 'human:%'", (run_id,)):
        agents.setdefault(r["screener"], {})[r["work_id"]] = r["decision"] != "exclude"
    for a, calls in sorted(agents.items()):
        pairs = [("keep" if d != "exclude" else "exclude", "keep" if calls[w] else "exclude")
                 for w, d in human.items() if w in calls]
        if pairs:
            agree = sum(x == y for x, y in pairs)
            k = agreement.cohen_kappa(pairs)
            out[a.split(":", 1)[-1]] = {"n": len(pairs), "agree": agree, "kappa": None if k is None else round(k, 2)}
    return out


# What the review reports about the checks

def core_verdicts(store: Store, run_id: int) -> dict[int, dict]:
    """The latest core check per study."""
    ensure(store)
    out = {}
    for r in store.db.execute("SELECT * FROM core_checks WHERE run_id = ? ORDER BY created_at, check_id", (run_id,)):
        out[r["work_id"]] = dict(r)
    return out


def calibration(store: Store, run_id: int) -> dict | None:
    """The blind screening sample a person decided: size, strata, people and agreement with each agent."""
    ensure(store)
    sample = {r[0] for r in store.db.execute("SELECT work_id FROM samples WHERE run_id = ? AND purpose = ?",
                                             (run_id, SAMPLE_PURPOSE))}
    if not sample:
        return None
    human, people = {}, set()
    for r in store.db.execute("SELECT work_id, screener, decision FROM screening WHERE run_id = ? AND stage = "
                              "'title_abstract' AND screener LIKE 'human:%' ORDER BY created_at", (run_id,)):
        if r["work_id"] in sample:
            human[r["work_id"]] = r["decision"]
            people.add(r["screener"].split(":", 1)[1])
    strata = Counter()
    for r in store.db.execute("SELECT content FROM check_packs WHERE run_id = ?", (run_id,)):
        for w, k in (json.loads(r["content"]).get("strata") or {}).items():
            if int(w) in human:
                strata[k] += 1
    return {"n": len(human), "people": sorted(people), "strata": dict(strata),
            "agreement": screening_agreement(store, run_id, human),
            "kept": sum(d != "exclude" for d in human.values())}


# The page

def page(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    title = f"Check pack: {data.get('title') or 'corpus ' + data['corpus']}"
    return (PAGE.replace("__TITLE__", html.escape(title))
                .replace("__DATA__", payload))


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { --bg:#f7f7f4; --card:#fff; --ink:#1d1d1f; --muted:#626268; --line:#dcdcd5; --accent:#2f4fa8; --soft:#eef1fa;
  --ok:#1f7a45; --warn:#a4570b; --bad:#b0302a; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#18181a; --card:#222225; --ink:#ececec;
  --muted:#a2a2a8; --line:#3a3a3f; --accent:#9db2ff; --soft:#262b3b; --ok:#6fcf97; --warn:#f2b263; --bad:#ff8a80; } }
:root[data-theme="dark"] { --bg:#18181a; --card:#222225; --ink:#ececec; --muted:#a2a2a8; --line:#3a3a3f;
  --accent:#9db2ff; --soft:#262b3b; --ok:#6fcf97; --warn:#f2b263; --bad:#ff8a80; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }
header { padding:18px 16px 10px; max-width:980px; margin:0 auto; }
h1 { font-size:1.3rem; margin:0 0 4px; } .sub { color:var(--muted); margin:0; }
nav { position:sticky; top:0; z-index:5; background:var(--bg); border-bottom:1px solid var(--line); }
nav .in { max-width:980px; margin:0 auto; padding:0 12px; display:flex; gap:4px; overflow-x:auto; }
nav button { border:0; background:none; color:var(--muted); font:inherit; padding:12px 10px; cursor:pointer;
  border-bottom:3px solid transparent; white-space:nowrap; }
nav button.on { color:var(--ink); border-bottom-color:var(--accent); font-weight:600; }
main { max-width:980px; margin:0 auto; padding:14px 16px 80px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:0 0 14px; }
.card.done { border-left:4px solid var(--ok); } .card.todo { border-left:4px solid var(--line); }
.meta { color:var(--muted); font-size:.88rem; } .tag { display:inline-block; font-size:.78rem; padding:1px 7px;
  border-radius:20px; background:var(--soft); color:var(--accent); margin-left:6px; }
blockquote { margin:8px 0; padding:8px 12px; border-left:3px solid var(--accent); background:var(--soft);
  border-radius:0 6px 6px 0; }
.lab { font-weight:600; font-size:.85rem; color:var(--muted); margin:10px 0 2px; text-transform:uppercase;
  letter-spacing:.03em; }
.choices { display:flex; flex-wrap:wrap; gap:6px 14px; margin:6px 0; } .choices label { cursor:pointer; }
textarea, input[type=text], select { width:100%; font:inherit; color:var(--ink); background:var(--bg);
  border:1px solid var(--line); border-radius:6px; padding:7px 9px; } textarea { min-height:54px; resize:vertical; }
table.crit { width:100%; border-collapse:collapse; font-size:.9rem; } .crit td, .crit th { border-bottom:1px solid var(--line);
  padding:5px 6px; text-align:left; vertical-align:top; } .crit th { color:var(--muted); font-weight:600; }
.wide { overflow-x:auto; } a { color:var(--accent); overflow-wrap:anywhere; }
.bar { height:6px; background:var(--line); border-radius:4px; overflow:hidden; margin:4px 0 12px; }
.bar i { display:block; height:100%; background:var(--ok); }
.btn { font:inherit; border:1px solid var(--accent); background:var(--accent); color:#fff; border-radius:7px;
  padding:9px 14px; cursor:pointer; margin:4px 6px 4px 0; } .btn.ghost { background:none; color:var(--accent); }
.note { color:var(--muted); font-size:.9rem; } .err { color:var(--bad); } .okt { color:var(--ok); }
details summary { cursor:pointer; color:var(--accent); } .rule { white-space:pre-wrap; }
@media (max-width:640px) { body { font-size:14.5px; } .card { padding:12px; } }
</style></head>
<body>
<header><h1 id="h1"></h1><p class="sub" id="sub"></p></header>
<nav><div class="in" id="tabs"></div></nav>
<main id="main"></main>
<script type="application/json" id="pack">__DATA__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("pack").textContent);
const KEY = "psyscientist-pack-" + D.pack_id;
let S = {checker: "", findings: {}, core: {}, screening: {}, coding: {}};
try { const s = localStorage.getItem(KEY); if (s) S = Object.assign(S, JSON.parse(s)); } catch (e) {}
function save() { try { localStorage.setItem(KEY, JSON.stringify(S)); } catch (e) {} progress(); }
function el(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "text") e.textContent = v; else if (k === "on") for (const [ev, f] of Object.entries(v)) e.addEventListener(ev, f);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids) if (c !== null && c !== undefined) e.append(c);
  return e;
}
function radios(name, options, value, onchange) {
  const box = el("div", {class: "choices"});
  for (const [k, label] of Object.entries(options)) {
    const r = el("input", {type: "radio", name, value: k, checked: value === k, on: {change: () => onchange(k)}});
    box.append(el("label", {}, r, " " + label));
  }
  return box;
}
function area(placeholder, value, oninput) {
  return el("textarea", {placeholder, on: {input: e => oninput(e.target.value)}}, value || "");
}
const PARTS = [
  ["findings", "Headline findings", D.findings.length],
  ["core", "Core studies", D.core.length],
  ["screening", "Blind screening", D.screening.length],
  ["coding", "Blind coding", (D.coding || []).length],
  ["finish", "Finish and send", null],
].filter(p => p[2] === null || p[2] > 0);              // a pack shows only the parts it has
let tab = PARTS.find(p => p[2])?.[0] || "finish";
try { tab = localStorage.getItem(KEY + "-tab") || tab; } catch (e) {}
if (!PARTS.some(p => p[0] === tab)) tab = PARTS[0][0];
function done(part) {
  if (part === "findings") return D.findings.filter(f => (S.findings[f.id] || {}).status).length;
  if (part === "core") return D.core.filter(c => (S.core[c.work_id] || {}).verdict).length;
  if (part === "screening") return D.screening.filter(s => (S.screening[s.work_id] || {}).decision).length;
  if (part === "coding") return (D.coding || []).filter(c => codedAll(c.work_id)).length;
  return 0;
}
function progress() {
  for (const b of document.querySelectorAll("nav button")) {
    const p = PARTS.find(x => x[0] === b.dataset.part);
    b.textContent = p[1] + (p[2] !== null ? ` (${done(p[0])}/${p[2]})` : "");
  }
}
function header() {
  document.getElementById("h1").textContent = "Check pack: " + (D.title || "corpus " + D.corpus);
  document.getElementById("sub").textContent = (D.paper_title && D.findings.length ? `Manuscript version ${D.version}: “${D.paper_title}”. ` : "")
    + `Pack ${D.pack_id}, made ${D.created}. Your answers stay in this browser until you download them.`;
  const t = document.getElementById("tabs");
  for (const [k] of PARTS) t.append(el("button", {"data-part": k, class: k === tab ? "on" : null, on: {click: () => go(k)}}));
  progress();
}
function go(k) {
  tab = k; try { localStorage.setItem(KEY + "-tab", k); } catch (e) {}
  for (const b of document.querySelectorAll("nav button")) b.classList.toggle("on", b.dataset.part === k);
  render(); window.scrollTo(0, 0);
}
function bar(part, total) {
  const n = done(part);
  return el("div", {}, el("div", {class: "note", text: `${n} of ${total} done`}),
            el("div", {class: "bar"}, el("i", {style: `width:${total ? 100 * n / total : 0}%`})));
}
function findingCard(f) {
  const a = S.findings[f.id] || (S.findings[f.id] = {});
  const card = el("div", {class: "card " + (a.status ? "done" : "todo")});
  const set = (k, v) => { a[k] = v; save(); card.className = "card " + (a.status ? "done" : "todo"); corr.hidden = a.status !== "corrected"; };
  card.append(el("div", {class: "meta"}, `Finding ${f.id} · `,
      f.link ? el("a", {href: f.link, target: "_blank", rel: "noopener", text: f.cite}) : f.cite,
      el("span", {class: "tag", text: f.fulltext ? "open-access full text was read" : "charted from the abstract"})));
  card.append(el("div", {class: "meta", text: f.title}));
  card.append(el("div", {class: "lab", text: "Used in"}));
  for (const u of f.uses) card.append(el("div", {class: "note", text: `${u.part}: ${u.text}`}));
  card.append(el("div", {class: "lab", text: "The finding as extracted"}), el("div", {text: f.finding}));
  if (f.effect) card.append(el("div", {class: "note", text: "Effect as reported: " + f.effect}));
  if (f.for_whom) card.append(el("div", {class: "note", text: "For whom: " + f.for_whom}));
  if (f.codes) card.append(el("div", {class: "note", text: "Coded as (these codes are counted; a wrong code is a correction): " + f.codes}));
  card.append(el("div", {class: "lab", text: `The passage it rests on (${f.location || "location not given"}; found in the ${f.source})`}),
              el("blockquote", {text: f.evidence}));
  card.append(el("div", {class: "lab", text: "Your check"}),
              radios("st" + f.id, D.statuses, a.status, v => set("status", v)),
              el("div", {class: "note", text: "Checked against:"}),
              radios("ag" + f.id, D.against, a.against, v => set("against", v)));
  const corr = el("div", {}, el("div", {class: "note", text: "What the source actually says (required for a correction):"}),
                  area("The correction", a.correction, v => set("correction", v)));
  corr.hidden = a.status !== "corrected";
  card.append(corr, area("Note (optional)", a.note, v => set("note", v)),
              el("button", {class: "btn ghost", on: {click: () => { delete S.findings[f.id]; save(); render(); }}, text: "Clear"}));
  return card;
}
function coreCard(c) {
  const a = S.core[c.work_id] || (S.core[c.work_id] = {});
  const card = el("div", {class: "card " + (a.verdict ? "done" : "todo")});
  const set = (k, v) => { a[k] = v; save(); card.className = "card " + (a.verdict ? "done" : "todo"); pick.hidden = a.verdict !== "fails"; };
  card.append(el("div", {class: "meta"}, c.link ? el("a", {href: c.link, target: "_blank", rel: "noopener", text: c.cite}) : c.cite,
      el("span", {class: "tag", text: c.fulltext ? "open-access full text was read" : "charted from the abstract"})));
  card.append(el("div", {}, el("strong", {text: c.title}), el("span", {class: "meta", text: ` (${c.year || "n.d."}${c.venue ? ", " + c.venue : ""})`})));
  if (c.abstract) card.append(el("details", {}, el("summary", {text: "Abstract"}), el("p", {text: c.abstract})));
  const t = el("table", {class: "crit"}, el("tr", {}, el("th", {text: "Criterion"}), el("th", {text: "As charted"}), el("th", {text: "Quote"})));
  for (const r of c.criteria) t.append(el("tr", {}, el("td", {text: r.label}), el("td", {text: r.charted}), el("td", {text: r.quote})));
  card.append(el("div", {class: "lab", text: "What the charting says on each inclusion criterion"}), el("div", {class: "wide"}, t));
  card.append(el("div", {class: "lab", text: "Your check"}), radios("v" + c.work_id, D.verdicts, a.verdict, v => set("verdict", v)));
  const sel = el("select", {on: {change: e => set("criterion", e.target.value)}}, el("option", {value: "", text: "Which criterion does it fail?"}));
  for (const k of D.criteria) sel.append(el("option", {value: k.id, selected: a.criterion === k.id, text: k.label}));
  const pick = el("div", {}, sel); pick.hidden = a.verdict !== "fails";
  card.append(pick, area("Note (optional): what you checked and how", a.note, v => set("note", v)));
  return card;
}
function screenCard(s, i) {
  const a = S.screening[s.work_id] || (S.screening[s.work_id] = {});
  const card = el("div", {class: "card " + (a.decision ? "done" : "todo")});
  const set = (k, v) => { a[k] = v; save(); card.className = "card " + (a.decision ? "done" : "todo"); };
  card.append(el("div", {class: "meta", text: `Record ${i + 1} of ${D.screening.length} · ${s.year || "n.d."}${s.venue ? " · " + s.venue : ""}`}));
  card.append(el("div", {}, el("strong", {text: s.title || "(no title)"})));
  card.append(el("p", {class: s.abstract ? null : "note", text: s.abstract || "No abstract: decide from the title, and choose unsure if it cannot tell."}));
  card.append(radios("d" + s.work_id, D.decisions, a.decision, v => set("decision", v)),
              area("Reason (optional)", a.note, v => set("note", v)));
  return card;
}
function codedAll(w) {
  const a = (S.coding || {})[w]; if (!a || !a.codes) return false;
  return D.coding_dims.every(d => (a.codes[d.name] || []).length);
}
const NONE = ["not_reported", "none_described", "not_determinable"];
function codingCard(c, i) {
  S.coding = S.coding || {};
  const a = S.coding[c.work_id] || (S.coding[c.work_id] = {codes: {}});
  a.codes = a.codes || {};
  const card = el("div", {class: "card " + (codedAll(c.work_id) ? "done" : "todo")});
  const mark = () => { card.className = "card " + (codedAll(c.work_id) ? "done" : "todo"); };
  card.append(el("div", {class: "meta"}, `Study ${i + 1} of ${D.coding.length} · `,
      c.link ? el("a", {href: c.link, target: "_blank", rel: "noopener", text: c.cite}) : c.cite,
      el("span", {class: "tag", text: c.seen})));
  card.append(el("div", {}, el("strong", {text: c.title}), el("span", {class: "meta", text: ` (${c.year || "n.d."}${c.venue ? ", " + c.venue : ""})`})));
  card.append(el("div", {class: "lab", text: "Abstract"}), el("p", {class: c.abstract ? null : "note", text: c.abstract || "No abstract."}));
  if (c.fulltext) card.append(el("details", {}, el("summary", {text: "Full text the coders saw (methods and results)"}), el("p", {class: "rule", text: c.fulltext})));
  if (c.byline) card.append(el("details", {}, el("summary", {text: "Authors and affiliations"}), el("p", {text: c.byline})));
  for (const d of D.coding_dims) {
    const chosen = new Set(a.codes[d.name] || []);
    const box = el("div", {class: "choices"});
    const boxes = [];
    for (const k of d.categories) {
      const cb = el("input", {type: "checkbox", value: k, checked: chosen.has(k), on: {change: e => {
        let now = new Set(a.codes[d.name] || []);
        if (e.target.checked) {
          if (NONE.includes(k)) now = new Set([k]); else { NONE.forEach(n => now.delete(n)); now.add(k); }
        } else now.delete(k);
        a.codes[d.name] = [...now];
        boxes.forEach(b => { b.checked = now.has(b.value); });
        save(); mark();
      }}});
      boxes.push(cb);
      box.append(el("label", {}, cb, " " + k.replace(/_/g, " ")));
    }
    card.append(el("div", {class: "lab", text: d.label}),
                el("details", {}, el("summary", {text: "The coding question"}), el("p", {class: "note", text: d.question})), box);
  }
  card.append(area("Note (optional): anything hard to decide, with where you found it", a.note, v => { a.note = v; save(); }));
  return card;
}
function intro(text) { return el("p", {class: "note", text}); }
function render() {
  const m = document.getElementById("main"); m.textContent = "";
  if (tab === "findings") {
    m.append(intro(`These ${D.findings.length} findings are what the headline parts of the manuscript (${D.headline.join(", ")}) rest on. ` +
      "Open the study, find the passage, and say whether the source says what the finding says. If not, mark it corrected and write what the source says. " +
      "Say whether you checked it against the passage shown or the full text (through your library if it is paywalled)."), bar("findings", D.findings.length));
    D.findings.forEach(f => m.append(findingCard(f)));
  } else if (tab === "core") {
    m.append(intro(`These ${D.core.length} studies were judged to meet every inclusion criterion from what an agent charted. ` +
      "Check each against the study itself. A study you say fails leaves the core."), bar("core", D.core.length));
    D.core.forEach(c => m.append(coreCard(c)));
  } else if (tab === "screening") {
    m.append(intro(`A random sample of ${D.screening.length} records. Screen each on its title and abstract, as if you were the first screener; ` +
      "the agents' decisions are not shown, so your calls can measure how accurate they were. Your decision counts as a person's call in the review."),
      el("details", {open: true}, el("summary", {text: "The inclusion rule"}), el("p", {class: "rule", text: D.rule || "(no rule in the protocol)"})),
      bar("screening", D.screening.length));
    D.screening.forEach((s, i) => m.append(screenCard(s, i)));
  } else if (tab === "coding") {
    m.append(intro(`Code these ${D.coding.length} studies on the ${D.coding_dims.length} dimensions below, from the text shown (it is what the coders saw) and, ` +
      "if you like, the study itself. The coders' codes are not shown, so your codes measure how accurate they were. Tick every category the record reports; " +
      "tick 'not reported' (or 'none described', 'not determinable') when it reports none. Code what learners and assessors reported, not what the programme intended."),
      D.coding_note ? el("p", {class: "note", text: D.coding_note}) : null, bar("coding", D.coding.length));
    D.coding.forEach((c, i) => m.append(codingCard(c, i)));
  } else {
    const name = el("input", {type: "text", value: S.checker || "", placeholder: "Your full name, as the Declaration should give it",
      on: {input: e => { S.checker = e.target.value; save(); }}});
    const msg = el("p", {class: "note"});
    m.append(el("div", {class: "card"},
      el("div", {class: "lab", text: "Your name"}), name,
      el("div", {class: "lab", text: "Progress"}),
      el("p", {text: `Headline findings ${done("findings")}/${D.findings.length} · core studies ${done("core")}/${D.core.length} · blind screening ${done("screening")}/${D.screening.length}` +
        ((D.coding || []).length ? ` · blind coding ${done("coding")}/${D.coding.length}` : "")}),
      el("p", {class: "note", text: "You can stop and come back: answers are kept in this browser. Download a file whenever you like; a later file replaces nothing, the newest check per item counts."}),
      el("button", {class: "btn", on: {click: () => exportFile(msg)}, text: "Download my checks"}),
      el("button", {class: "btn ghost", on: {click: () => copyFile(msg)}, text: "Copy to clipboard"}),
      el("label", {class: "btn ghost"}, "Continue from a downloaded file",
        el("input", {type: "file", accept: ".json,application/json", style: "display:none", on: {change: e => loadFile(e, msg)}})),
      msg,
      el("div", {class: "lab", text: "Then"}),
      el("p", {class: "note", text: "Send the downloaded file to the review's owner, who imports it with: python -m psyscientist import-checks <file>. " +
        "Your checks then appear in the manuscript's Declaration of AI use and its appendix, with your name and the date."})));
  }
}
function answers() {
  const out = {format: D.format, pack_id: D.pack_id, corpus: D.corpus, run_id: D.run_id, paper_id: D.paper_id,
               checker: (S.checker || "").trim(), exported_at: new Date().toISOString(), findings: [], core: [], screening: [], coding: []};
  for (const f of D.findings) { const a = S.findings[f.id]; if (a && a.status) out.findings.push({id: f.id, status: a.status, against: a.against || "", correction: a.correction || "", note: a.note || ""}); }
  for (const c of D.core) { const a = S.core[c.work_id]; if (a && a.verdict) out.core.push({work_id: c.work_id, verdict: a.verdict, criterion: a.criterion || "", note: a.note || ""}); }
  for (const s of D.screening) { const a = S.screening[s.work_id]; if (a && a.decision) out.screening.push({work_id: s.work_id, decision: a.decision, note: a.note || ""}); }
  for (const c of D.coding || []) { const a = (S.coding || {})[c.work_id]; if (a && a.codes && Object.values(a.codes).some(v => v.length)) out.coding.push({work_id: c.work_id, codes: a.codes, note: a.note || ""}); }
  return out;
}
function problems(o) {
  const p = [];
  if (!o.checker) p.push("Add your name.");
  for (const f of o.findings) { if (!f.against) p.push(`Finding ${f.id}: say what you checked it against.`);
    if (f.status === "corrected" && !f.correction.trim()) p.push(`Finding ${f.id}: write what the source says.`); }
  for (const c of o.core) if (c.verdict === "fails" && !c.criterion) p.push(`Core study ${c.work_id}: choose the criterion it fails.`);
  return p;
}
function exportFile(msg) {
  const o = answers(), p = problems(o);
  if (p.length) { msg.className = "err"; msg.textContent = p.slice(0, 6).join(" "); return; }
  const blob = new Blob([JSON.stringify(o, null, 1)], {type: "application/json"});
  const a = el("a", {href: URL.createObjectURL(blob), download: `checks_${D.corpus}_${D.pack_id}_${o.checker.replace(/\W+/g, "_")}.json`});
  document.body.append(a); a.click(); a.remove();
  msg.className = "okt"; msg.textContent = `Saved: ${o.findings.length} findings, ${o.core.length} core studies, ${o.screening.length} screening decisions, ${o.coding.length} coded studies.`;
}
function copyFile(msg) {
  const o = answers(), p = problems(o);
  if (p.length) { msg.className = "err"; msg.textContent = p.slice(0, 6).join(" "); return; }
  navigator.clipboard.writeText(JSON.stringify(o, null, 1)).then(() => { msg.className = "okt"; msg.textContent = "Copied. Paste it into a file ending in .json."; },
    () => { msg.className = "err"; msg.textContent = "Copying was not allowed here; use Download instead."; });
}
function loadFile(e, msg) {
  const f = e.target.files[0]; if (!f) return;
  f.text().then(t => {
    const o = JSON.parse(t);
    if (o.pack_id !== D.pack_id) throw new Error("That file belongs to another pack.");
    S.checker = o.checker || S.checker;
    for (const a of o.findings || []) S.findings[a.id] = {status: a.status, against: a.against, correction: a.correction, note: a.note};
    for (const a of o.core || []) S.core[a.work_id] = {verdict: a.verdict, criterion: a.criterion, note: a.note};
    for (const a of o.screening || []) S.screening[a.work_id] = {decision: a.decision, note: a.note};
    S.coding = S.coding || {};
    for (const a of o.coding || []) S.coding[a.work_id] = {codes: a.codes || {}, note: a.note};
    save(); render(); msg.className = "okt"; msg.textContent = "Loaded.";
  }).catch(err => { msg.className = "err"; msg.textContent = "Could not read that file: " + err.message; });
}
header(); render();
</script>
</body></html>
"""
