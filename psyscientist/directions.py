"""Directions: what is worth exploring next, proposed after a finished paper, chosen by the author.

The pipeline directs the analysis; the author's attention goes to what the results open up. After a paper, an agent
reads it, its tables and the open gaps, hypotheses, questions and contradictions the agents logged, and proposes
6-10 directions, ranked: a title, the question, why it matters for the paper's readers, what the evidence shows now
(citing findings and tables), what is unknown, the concrete next step (a new review strand, a reanalysis, an
empirical study, an intervention to design, a piece of theory) and its value, feasibility and novelty (1-5). The
author marks each `chosen` (explore it) or `set_aside`; a chosen direction is what the next run is drafted from.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import insights, tables
from .store import Store, now

KINDS = ("directions",)
DIRECTION_KINDS = ("new_review", "reanalysis", "empirical_study", "intervention_design", "theory")
STATUSES = ("proposed", "chosen", "set_aside")

SCHEMA = """
CREATE TABLE IF NOT EXISTS directions (
    direction_id INTEGER PRIMARY KEY,
    corpus       TEXT NOT NULL,
    run_id       INTEGER NOT NULL REFERENCES runs(run_id),
    paper_id     INTEGER,
    rank         INTEGER NOT NULL,
    label        TEXT NOT NULL,                  -- D1, D2, ... within one proposal
    title        TEXT NOT NULL,
    kind         TEXT NOT NULL,
    content      TEXT NOT NULL,                  -- JSON: question, why, evidence, unknown, next_step, scores, cites
    status       TEXT NOT NULL DEFAULT 'proposed',
    note         TEXT,
    decided_by   TEXT,
    agent        TEXT NOT NULL,
    call_id      INTEGER REFERENCES calls(call_id),
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""

OUTPUT = {"directions": [{
    "id": "D1", "title": "a short title (at most 12 words)",
    "kind": " | ".join(DIRECTION_KINDS),
    "question": "the question to answer, one sentence",
    "why": "why it matters for the paper's readers, 1-3 sentences",
    "evidence": "what the evidence shows now, 1-3 sentences; numbers only from what you cite",
    "unknown": "what is not known, 1-2 sentences",
    "next_step": "the concrete first step, 1-2 sentences (what the pipeline or the author would do)",
    "value": 4, "feasibility": 3, "novelty": 3,
    "cites": [123], "tables": ["T1"], "insights": [45]}]}


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def rows(store: Store, run_id: int) -> list[dict]:
    """The latest proposal's directions for a run, ranked."""
    ensure(store)
    last = store.db.execute("SELECT max(call_id) FROM directions WHERE run_id = ?", (run_id,)).fetchone()[0]
    if last is None:
        return []
    out = []
    for r in store.db.execute("SELECT * FROM directions WHERE run_id = ? AND call_id = ? ORDER BY rank",
                              (run_id, last)):
        out.append({**{k: r[k] for k in ("direction_id", "paper_id", "rank", "label", "title", "kind", "status",
                                         "note", "decided_by")}, **json.loads(r["content"])})
    return out


def set_status(store: Store, direction_id: int, status: str, who: str, note: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    ensure(store)
    with store.db:
        cur = store.db.execute("UPDATE directions SET status = ?, note = ?, decided_by = ?, updated_at = ? WHERE "
                               "direction_id = ?", (status, note, who, now(), direction_id))
    if not cur.rowcount:
        raise ValueError(f"no direction {direction_id}")


# The task

def _input(store: Store, run_id: int, protocol: dict, paper_id: int | list[int] | None) -> dict:
    """One paper, or several read together (a general paper and a proposal, say): their manuscripts, main tables and
    the findings their evidence is made of. Several papers' tables are keyed P16-T1, P19-T1, ..."""
    from . import paper
    from . import aim as aim_mod
    paper.ensure(store)
    ids = paper_id if isinstance(paper_id, list) else [paper_id] if paper_id else []
    if not ids:
        last = store.db.execute("SELECT paper_id FROM papers WHERE run_id = ? ORDER BY paper_id DESC LIMIT 1",
                                (run_id,)).fetchone()
        if last is None:
            raise ValueError("no paper yet: directions are proposed after a paper")
        ids = [last[0]]
    several = len(ids) > 1
    papers, main_tables, by_id, base = [], {}, {}, protocol
    for pid in ids:
        row = store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (pid,)).fetchone()
        if row is None:
            raise ValueError(f"no paper {pid}")
        method = json.loads(row["method"])
        protocol = aim_mod.with_aim(base, method.get("aim_id"))     # the aim the paper was written for
        main_tables.update({(f"P{pid}-{t['id']}" if several else t["id"]): t["text"]
                            for t in method.get("tables") or [] if t.get("place") == "main"})
        # a focused paper: the findings its evidence is made of
        for r in aim_mod.aim_rows(store, run_id, protocol) if protocol.get("aim") else tables.finding_rows(store, run_id):
            by_id.setdefault(r["finding_id"], r)
        papers.append({"paper_id": pid, "version": row["version"], "title": row["title"], "aim": protocol.get("aim") or {},
                       "manuscript": paper.render(store, pid, full=False)})
    open_kinds = ("gap", "hypothesis", "question", "contradiction", "pattern")
    logged = [{"id": i["insight_id"], "kind": i["kind"], "statement": i["statement"]}
              for i in insights.rows(store, row["corpus"]) if i["status"] != "dropped" and i["kind"] in open_kinds]
    finding_ids = {r["finding_id"] for r in tables.finding_rows(store, run_id)}
    labels = tables.cite_map(store, run_id)
    findings = [{"id": r["finding_id"], "study": labels.get(r["work_id"], ""), "finding": r["finding"]}
                for r in by_id.values()][:400]
    one = {"version": papers[0]["version"], "aim": papers[0]["aim"], "manuscript": papers[0]["manuscript"]}
    return {"paper_id": ids[-1], **({"papers": papers} if several else one), "tables": main_tables,
            "insights": logged[-150:], "finding_ids": sorted(finding_ids), "findings": findings,
            "strands": [{"corpus": c, "title": " ".join(str(p.get("title", "")).split())}
                        for c, p in _strands(protocol).items()]}


def _strands(protocol: dict) -> dict:
    from .config import load_protocol
    out = {}
    for c in {(protocol.get("aim") or {}).get("comparator"), (protocol.get("comparator") or {}).get("corpus")} - {None}:
        try:
            out[c] = load_protocol(c)[2]
        except OSError:
            pass
    return out


def instructions(protocol: dict, several: bool = False) -> str:
    aim = protocol.get("aim") or {}
    held = ("several finished papers on the same review, to be read together (`papers`: each with its `title`, the "
            "`aim` it answers and its `manuscript`, Markdown) and their main tables (`tables`, keyed by paper: P16-T1 "
            "is Table 1 of paper 16)" if several else
            "a finished paper (`manuscript`, Markdown) and its main tables (`tables`), the aim it answers (`aim`)")
    return "\n".join([
        "# What to explore next",
        "",
        f"batch_01.json holds {held}, the gaps, hypotheses, questions, contradictions and patterns the agents logged while reviewing "
        "(`insights`), the findings the paper rests on (`findings`: id, study, statement; cite them by id; any id in "
        "`finding_ids` may be cited), and the strands already explored "
        "(`strands`: do not propose them again).",
        "",
        "The author directs the research by choosing what to explore next. Propose 6 to 10 directions, ranked by how "
        "much they would add for the paper's readers" + (f" ({aim['audience']})" if aim.get("audience") else "")
        + ". A good direction:",
        "- follows from something the paper found, failed to find or could not judge (cite the findings, tables and "
        "insights it rests on);",
        "- is a question someone could answer: name the next step concretely (which literature to review, which data "
        "to reanalyse, which study to run with which design and measures, which intervention to design and test);",
        "- is honest about its value, feasibility and novelty (1 low to 5 high), and about what is unknown;",
        f"- has one `kind`: {', '.join(DIRECTION_KINDS)}.",
        "Prefer a mix: at least one direction the pipeline can run on its own (a new review strand or a reanalysis) "
        "and at least one empirical study the author could run.",
        "",
        "Rules: use only batch_01.json; no web, no outside literature. Numbers only from the manuscript or tables. "
        "Plain English, no hype.",
        "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps(OUTPUT, ensure_ascii=False, indent=1), "```"]) + "\n"


def write_task(store: Store, run_id: int, kind: str, agent: str, protocol: dict, sha: str, root: Path,
               options: dict) -> Path:
    from .agents import _new_folder
    ensure(store)
    data = _input(store, run_id, protocol, options.get("paper"))
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    out = _new_folder(root, corpus, run_id, kind, agent)
    (out / "instructions.md").write_text(instructions(protocol, "papers" in data), encoding="utf-8")
    (out / "batch_01.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "task.json").write_text(json.dumps({"kind": kind, "agent": agent, "run_id": run_id, "corpus": corpus,
                                               "protocol_sha256": sha, "batches": 1, "records": 1,
                                               "paper_id": data["paper_id"]}, indent=1), encoding="utf-8")
    return out


def check(answer, data: dict) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("directions"), list):
        return ['the answer must be {"directions": [...]}']
    ds = answer["directions"]
    errs = [] if 6 <= len(ds) <= 10 else [f"propose 6 to 10 directions, not {len(ds)}"]
    known_f, known_t = set(data["finding_ids"]), set(data["tables"])
    known_i = {i["id"] for i in data["insights"]}
    for n, d in enumerate(ds, 1):
        where = f"direction {d.get('id', n)}"
        for k in ("title", "kind", "question", "why", "evidence", "unknown", "next_step"):
            if not str(d.get(k) or "").strip():
                errs.append(f"{where}: needs `{k}`")
        if d.get("kind") not in DIRECTION_KINDS:
            errs.append(f"{where}: kind must be one of {DIRECTION_KINDS}")
        for k in ("value", "feasibility", "novelty"):
            if not isinstance(d.get(k), int) or not 1 <= d[k] <= 5:
                errs.append(f"{where}: {k} must be an integer from 1 to 5")
        bad = ([f for f in d.get("cites") or [] if f not in known_f] + [t for t in d.get("tables") or [] if t not in known_t]
               + [i for i in d.get("insights") or [] if i not in known_i])
        if bad:
            errs.append(f"{where}: cites what was not given: {bad[:6]}")
    return errs


def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions_text: str) -> dict:
    from .agents import _strip_fences
    ensure(store)
    data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
    result = folder / "batch_01_result.json"
    if not result.exists():
        return {"stored": 0, "problems": ["batch_01_result.json is missing"]}
    raw = result.read_text(encoding="utf-8")
    try:
        answer = json.loads(_strip_fences(raw))
    except ValueError as err:
        return {"stored": 0, "problems": [f"not valid JSON ({err})"]}
    errs = check(answer, data)
    if errs:
        return {"stored": 0, "problems": errs}
    batch_text = (folder / "batch_01.json").read_text(encoding="utf-8")
    call_id = store.log_call(meta["run_id"], "llm", meta["agent"],
                             f"{instructions_text}\n\n[directions batch_01.json, sha256 "
                             f"{hashlib.sha256(batch_text.encode('utf-8')).hexdigest()}]", 200, 0, raw.encode("utf-8"),
                             {"agent": meta["agent"], "task": "directions", "records": 1,
                              "protocol_sha256": meta["protocol_sha256"]})
    stamp = now()
    with store.db:
        for rank, d in enumerate(answer["directions"], 1):
            content = {k: d.get(k) for k in ("question", "why", "evidence", "unknown", "next_step", "value",
                                             "feasibility", "novelty", "cites", "tables", "insights")}
            if data.get("papers"):
                content["papers"] = [p["paper_id"] for p in data["papers"]]
            store.db.execute("INSERT INTO directions (corpus, run_id, paper_id, rank, label, title, kind, content, "
                             "status, agent, call_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                             "'proposed', ?, ?, ?, ?)",
                             (meta["corpus"], meta["run_id"], data["paper_id"], rank, str(d.get("id") or f"D{rank}"),
                              d["title"], d["kind"], json.dumps(content, ensure_ascii=False), meta["agent"], call_id,
                              stamp, stamp))
    return {"stored": len(answer["directions"]), "problems": []}
