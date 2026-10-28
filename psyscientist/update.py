"""Search updates: a standing review searched again, and the records each update adds taken through the pipeline.

`update-search RUN` searches every source of the protocol again with the run's string. Each update is stored as its own
source, `<source>@<YYYY-MM-DD>`, so the original search stays as it was reported (the method facts and Figure 1 list
updates apart), and the records new to the run go on its sheet, to be screened like the others. `update-search RUN
--supplement ID` runs instead a supplementary search with its own string, from the protocol's [[supplements]] (a gap in
the original string, recorded as a deviation); it is stored as `<source>@<YYYY-MM-DD>+<ID>` and its records go through
the same pipeline.

`update RUN` writes, for the records the updates added (and, once the protocol's [mining] says `screen_all`, the records
citation searching put on the sheet), the next tasks the state allows, in the pipeline's order:
- screening: every screener, at title and abstract;
- eligibility: every judge, for the records kept at title and abstract;
- map extraction, for the records still in;
- findings, for those of them within the protocol's findings scope (which the charting decides).
A stage starts once the one before it is done for every added record. A task is done once it is imported
(agent-import); each is answered by a subagent, as every other task. While a task of a kind and agent is open (written,
not imported) no other is written for it, since writing would reuse its folder. Once no added record waits, the
forward check takes over: `referee RUN --dates`, then `referee RUN` (the CLI does both).

`update RUN --report` writes what the updates found, stage by stage, and what the later studies say of the papers'
frozen claims, to notes/<date>-update-corpus-X.md.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from . import query
from .agents import FULL_TEXT, IMPORTED, kept_ids, sheet_ids, task_dir, write_tasks
from .config import ROOT, Settings, load_protocol
from .http import ApiError, Http
from .pilot import DIALECT, SAMPLE_PURPOSE, _store_result, earlier_sheet_ids, search_source
from .report import write_report
from .store import Store, now

SEP = "@"
PLUS = "+"
AGENTS = {"screen": ("model:claude-sonnet-5-5", "model:claude-opus-5-5"),
          "eligibility": ("model:claude-sonnet-5-5", "model:claude-opus-5-5"),
          "extract": ("model:claude-sonnet-5-5",),
          "findings": ("model:claude-opus-5-5",)}
STAGES = ("screen", "eligibility", "extract", "findings")


def is_update(source: str) -> bool:
    return SEP in source


def supplement_of(source: str) -> str | None:
    """The supplementary search a source label belongs to (`openalex@2026-10-27+kin` -> `kin`), or None."""
    return source.split(SEP, 1)[1].split(PLUS, 1)[1] if is_update(source) and PLUS in source.split(SEP, 1)[1] else None


def supplements(protocol: dict) -> dict[str, dict]:
    return {s["id"]: s for s in protocol.get("supplements") or []}


def _corpus(store: Store, run_id: int) -> str:
    row = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    return row["corpus"]


# The search

def search(store: Store, run_id: int, settings: Settings, root: Path = ROOT, log=print, day: str | None = None,
           supplement: str | None = None, only: list[str] | None = None) -> list[int]:
    """Every source of the protocol searched again with the run's string, stored as `<source>@<day>`; the records new
    to the run (not in its pool, not on an earlier sheet of the corpus) go on its sheet. Returns them. With
    `supplement`, the protocol's supplementary search of that id instead, with its own string (and sources, if it names
    them), stored as `<source>@<day>+<id>`. With `only`, just these sources (to retry one that failed)."""
    corpus = _corpus(store, run_id)
    _, _, protocol = load_protocol(corpus, root)
    supp = supplements(protocol).get(supplement) if supplement else None
    if supplement and supp is None:
        raise ValueError(f"no [[supplements]] entry {supplement!r} in the protocol")
    tree = query.parse(supp["query"] if supp else protocol["query"])
    day = day or now()[:10]
    pool = {r[0] for r in store.db.execute("SELECT work_id FROM retrievals WHERE run_id = ?", (run_id,))}
    seen = pool | earlier_sheet_ids(store, corpus, run_id)
    http = Http(store, run_id)
    new: list[int] = []
    for source in [x for x in (supp or {}).get("sources") or protocol["sources"] if not only or x in only]:
        label = f"{source}{SEP}{day}" + (f"{PLUS}{supplement}" if supp else "")
        sent = query.render(tree, DIALECT.get(source, "openalex"))
        try:
            result = search_source(source, http, sent, protocol["pilot"]["max_records_per_source"], settings)
        except (ApiError, ValueError) as err:
            store.set_run_source(run_id, label, sent, None, 0, "failed", str(err))
            log(f"  {source}: FAILED ({err})")
            continue
        result.source = label
        result.notes = "; ".join(x for x in (result.notes, f"supplementary search {supplement} {day}, its own string"
                                             if supp else f"update search {day}, the run's string") if x)
        _store_result(store, run_id, result)
        ids = [r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? AND source = ?",
                                              (run_id, label))]
        fresh = [w for w in ids if w not in seen and w not in new]
        new += fresh
        log(f"  {source}: {result.reported_total} reported, {len(result.records)} fetched, {len(fresh)} new to the run")
    store.add_sample(run_id, sorted(new), SAMPLE_PURPOSE)
    out = root / "output" / f"corpus_{corpus.upper()}" / f"run_{run_id:03d}"
    out.mkdir(parents=True, exist_ok=True)
    write_report(store, run_id, out / "report.md")
    return sorted(new)


def added(store: Store, run_id: int) -> dict[int, str]:
    """The records on the run's sheet that only update searches retrieved, with the day of the first that did (for a
    supplementary search, `<day>+<id>`)."""
    first, other = {}, set()
    for r in store.db.execute("SELECT work_id, source FROM retrievals WHERE run_id = ? ORDER BY source", (run_id,)):
        if is_update(r["source"]):
            day = r["source"].split(SEP, 1)[1]
            first[r["work_id"]] = min(first.get(r["work_id"], day), day)
        else:
            other.add(r["work_id"])
    sheet = set(sheet_ids(store, run_id))
    return {w: d for w, d in sorted(first.items()) if w not in other and w in sheet}


# The pipeline for the added records

def _folders(root: Path, corpus: str, run_id: int, kind: str, agent: str) -> list[Path]:
    base = task_dir(root, corpus, run_id, kind, agent)
    out = []
    for f in sorted(base.parent.glob(base.name + "*")):
        if f.name != base.name and not f.name.startswith(base.name + "__"):
            continue
        meta = f / "task.json"
        if meta.exists():
            m = json.loads(meta.read_text(encoding="utf-8"))
            # a task for some dimensions only, or a blind second coder, does not stand for the stage
            if m.get("kind") == kind and m.get("agent") == agent and not m.get("dims") and not m.get("second_coder"):
                out.append(f)
    return out


def _records(folder: Path) -> set[int]:
    ids = set()
    for b in folder.glob("batch_[0-9][0-9].json"):
        data = json.loads(b.read_text(encoding="utf-8"))
        for rec in data if isinstance(data, list) else data.get("records") or []:
            if isinstance(rec, dict) and isinstance(rec.get("work_id"), int):
                ids.add(rec["work_id"])
    return ids


def _stored(store: Store, run_id: int, kind: str, agent: str) -> set[int]:
    if kind in ("screen", "eligibility"):
        stage = FULL_TEXT if kind == "eligibility" else "title_abstract"
        sql = "SELECT DISTINCT work_id FROM screening WHERE run_id = ? AND screener = ? AND stage = ?"
        return {r[0] for r in store.db.execute(sql, (run_id, agent, stage))}
    table = "study_features" if kind == "extract" else "findings"
    return {r[0] for r in store.db.execute(f"SELECT DISTINCT work_id FROM {table} WHERE run_id = ? AND extractor = ?",
                                           (run_id, agent))}


def coverage(store: Store, root: Path, corpus: str, run_id: int, kind: str, agent: str) -> tuple[set, list[Path]]:
    """The records a kind and agent has done (an imported task covered them, or their answers are stored; a study can
    yield no finding), and its open task folders."""
    done, open_ = _stored(store, run_id, kind, agent), []
    for f in _folders(root, corpus, run_id, kind, agent):
        if (f / IMPORTED).exists():
            done |= _records(f)
        elif any(f.glob("batch_[0-9][0-9].json")):
            open_.append(f)
    return done, open_


def step(store: Store, run_id: int, root: Path = ROOT, agents: dict | None = None) -> dict:
    """Write the next tasks for the added records; say where they stand. `waiting` counts the records some stage still
    owes; at 0 the forward check takes over."""
    from .tables import in_scope
    agents = agents or AGENTS
    corpus = _corpus(store, run_id)
    protocol = load_protocol(corpus, root)[2]
    new = dict(added(store, run_id))
    if (protocol.get("mining") or {}).get("screen_all"):
        mined = {r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? AND source = "
                                                "'citation_search'", (run_id,))}
        new.update({w: "citation_search" for w in sheet_ids(store, run_id) if w in mined and w not in new})
    written: list[Path] = []
    open_: list[Path] = []
    stages: dict[str, dict] = {}
    waiting: set[int] = set()
    if not new:
        return {"added": new, "written": written, "open": open_, "stages": stages, "waiting": 0}
    for kind in STAGES:
        if kind == "eligibility" and not protocol.get("eligibility"):
            continue                            # a protocol without the stage keeps what title and abstract kept
        todo = set(new)
        if kind == "eligibility":
            todo &= set(kept_ids(store, run_id, "title_abstract"))
        elif kind in ("extract", "findings"):
            todo &= set(kept_ids(store, run_id))
        if kind == "findings":
            scope = in_scope(store, run_id, protocol, sorted(todo))
            todo = todo if scope is None else todo & scope
        owed = set()
        for agent in agents[kind]:
            done, opened = coverage(store, root, corpus, run_id, kind, agent)
            missing = todo - done
            stages[f"{kind} ({agent})"] = {"records": len(todo), "done": len(todo & done)}
            if opened:
                open_ += opened
            elif missing:
                written.append(write_tasks(store, run_id, kind, agent, root, work_ids=sorted(missing)))
            owed |= missing
        if owed:
            waiting = owed
            break
    return {"added": new, "written": written, "open": [str(f) for f in open_], "stages": stages,
            "waiting": len(waiting)}


# What the updates found

def summary(store: Store, run_id: int, root: Path = ROOT) -> dict:
    """Per update day: what each source returned and what became of the records it added; and the forward check."""
    from . import referee
    from .tables import in_scope
    new = added(store, run_id)
    title_kept = set(kept_ids(store, run_id, "title_abstract"))
    kept = set(kept_ids(store, run_id))
    scope = in_scope(store, run_id, load_protocol(_corpus(store, run_id), root)[2], sorted(kept & set(new)))
    with_f = Counter(r[0] for r in store.db.execute("SELECT work_id FROM findings WHERE run_id = ?", (run_id,)))
    searched = {}
    for r in store.db.execute("SELECT source, reported_total, fetched, status, error FROM run_sources WHERE run_id = ? "
                              "ORDER BY source", (run_id,)):
        if is_update(r["source"]):
            name, day = r["source"].split(SEP, 1)
            searched.setdefault(day, []).append({"source": name, "reported": r["reported_total"], "fetched": r["fetched"],
                                                 "status": r["status"], "error": r["error"]})
    days = []
    for day in sorted(searched):
        ws = [w for w, d in new.items() if d == day]
        days.append({"day": day, "sources": searched[day], "added": len(ws),
                     "kept_title_abstract": sum(w in title_kept for w in ws), "kept": sum(w in kept for w in ws),
                     "in_scope": None if scope is None else sum(w in scope for w in ws),
                     "with_findings": sum(1 for w in ws if with_f[w]), "findings": sum(with_f[w] for w in ws),
                     "records": ws})
    return {"run": run_id, "days": days, "forward": referee.claims_summary(store, run_id, root)}


def report(s: dict) -> str:
    from .paper import source_name
    L = [f"# Search updates of run {s['run']}", "",
         "Generated by `python -m psyscientist update " + str(s["run"]) + " --report` from the evidence store.", ""]
    if not s["days"]:
        L += ["No update search yet."]
    for d in s["days"]:
        day, _, supp = d["day"].partition(PLUS)
        L += [f"## Supplementary search {supp} of {day}" if supp else f"## Update of {day}", "",
              ("Searched with its own string: " if supp else "Searched again with the run's string: ") + "; ".join(
                  f"{source_name(x['source'])} {x['fetched']} records" + (f" (failed: {x['error']})" if x["status"] ==
                                                                          "failed" else "") for x in d["sources"]) + ".",
              "", f"- New to the review: {d['added']} records.",
              f"- Kept at title and abstract: {d['kept_title_abstract']}; kept after eligibility: {d['kept']}"
              + (f"; within the findings scope (charted as the review's topic): {d['in_scope']}"
                 if d["in_scope"] is not None else "") + ".",
              f"- With findings: {d['with_findings']} studies, {d['findings']} findings.", ""]
    f = s.get("forward")
    if f:
        L += ["## The forward check of the papers", "",
              f"Claims frozen {f['frozen_at'][:10] if f['frozen_at'] else 'not yet'}; later studies (first published from "
              f"{f['test_from']} on) so far: {f['test_studies']}, with {f['test_findings']} findings, {f['judged']} judged"
              + (f", {f['unjudged']} not yet" if f["unjudged"] else "") + "."]
        for p in f["papers"]:
            tested = [c for c in p["claims"] if c["status"] != "not tested yet"]
            L += ["", f"### {p['name']}", "",
                  f"{len(tested)} of {len(p['claims'])} claims tested so far." if tested else
                  f"None of its {len(p['claims'])} claims tested yet."]
            L += [f"- **{c['status']}** ({c['studies_for']} studies for, {c['studies_against']} against): {c['claim']}"
                  for c in tested]
    return "\n".join(L) + "\n"
