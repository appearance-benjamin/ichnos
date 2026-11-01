"""Agent steps: the code writes the task, an agent does it, the code checks and stores the answer.

Four kinds of task, all generated from the corpus protocol so they work in any
psychological domain:

- screen:     score each record on the protocol's [[screening.metrics]] (0 not
              met, 1 cannot tell, 2 met), each score backed by a quote. The
              decision is not the agent's: `decide` derives it with a fixed rule.
- extract:    fill the protocol's [map] dimensions for each kept study and,
              when the protocol lists [[claims]], propose which claims each
              study supports, challenges or qualifies.
- findings:   list each kept study's results that bear on the protocol's
              [findings] question, one row per finding with the passage that
              states it; these rows are what a writer may later cite.
- synthesize: read all findings of a run together and propose insights that
              only show across studies, each citing finding rows.

Every task may also return `insights`: things noticed along the way that the
task did not ask for (see insights.py).

A task folder holds task.json, instructions.md and batch_NN.json files. The
agent writes batch_NN_result.json next to each batch; any agent can do this
(a Claude Code subagent now, an API runner later). `import_task` validates
every answer, checks that each quote really occurs in the record, logs prompt
and answer in `calls`, and stores the results.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
import unicodedata
from pathlib import Path

from . import directions, evolution, insights, lenses, novelty, paper, recall, recode, referee, strong, writer
from .config import ROOT, load_protocol
from .fulltext import findings_text, methods_text, sections_of
from .store import Store, now

KINDS = ("screen", "eligibility", "extract", "findings", "synthesize", "write") + paper.KINDS + recode.KINDS + lenses.KINDS + novelty.KINDS + recall.KINDS + directions.KINDS + strong.KINDS
ELIGIBILITY = "eligibility"     # the task for the stage after title and abstract; criteria in [eligibility]
FULL_TEXT = "full_text"         # how that stage is stored (PRISMA's full-text eligibility stage)
RECORD_KINDS = ("study", "review", "other")
RELATIONS = ("supports", "challenges", "qualifies")
NOT_REPORTED = "not_reported"


# Task folders

def task_dir(root: Path, corpus: str, run_id: int, kind: str, agent: str) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", agent.lower()).strip("-")
    return root / "output" / f"corpus_{corpus}" / f"run_{run_id:03d}" / "agents" / f"{kind}__{slug}"


def _new_folder(root, corpus, run_id, kind, agent) -> Path:
    out = base = task_dir(root, corpus, run_id, kind, agent)
    n = 2
    while any(out.glob("batch_*_result.json")):     # answered tasks are kept as they are
        out = base.with_name(f"{base.name}__{n}")
        n += 1
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("batch_*.json"):
        old.unlink()
    return out


def write_tasks(store: Store, run_id: int, kind: str, agent: str, root: Path = ROOT,
                batch_size: int = 25, work_ids: list[int] | None = None, use_fulltext: bool = True,
                options: dict | None = None) -> Path:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    run = store.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"no run {run_id}")
    corpus = run["corpus"]
    path, text, protocol = load_protocol(corpus, root)
    sha = store.add_protocol(corpus, path.relative_to(root).as_posix(), text)
    check_protocol(protocol, kind)
    dims = [d.strip() for d in str((options or {}).get("dims") or "").split(",") if d.strip()]
    second_coder = bool((options or {}).get("second_coder"))
    if second_coder and not ((kind == "extract" and dims) or (kind in recode.KINDS and dims)):
        raise ValueError("--second-coder is for extract or recode tasks with --dims: an independent coding of those "
                         "dimensions or fields")
    if dims:
        if kind not in ("extract",) + recode.KINDS:
            raise ValueError("--dims is for extract and recode tasks")
        if kind == "extract":
            protocol = only_dimensions(protocol, dims)
    if kind == "synthesize":
        return _write_synthesis(store, run_id, corpus, agent, protocol, sha, root)
    if kind in paper.KINDS:
        return paper.write_task(store, run_id, kind, agent, protocol, sha, root, options or {})
    if kind in directions.KINDS:
        return directions.write_task(store, run_id, kind, agent, protocol, sha, root, options or {})
    if kind in recode.KINDS:
        return recode.write_task(store, run_id, kind, agent, protocol, sha, root, options or {})
    if kind in lenses.KINDS:
        return lenses.write_task(store, run_id, kind, agent, protocol, sha, root, options or {})
    if kind in novelty.KINDS:
        return novelty.write_task(store, run_id, kind, agent, protocol, sha, root, options or {})
    if kind in recall.KINDS:
        return recall.write_task(store, run_id, kind, agent, protocol, sha, root, options or {}, batch_size)
    if kind in strong.KINDS:
        return strong.write_task(store, run_id, kind, agent, protocol, sha, root, dict(options or {}, works=work_ids))
    if kind == "write":
        data = writer.build_input(store, run_id, protocol)
        out = _new_folder(root, corpus, run_id, "write", agent)
        (out / "instructions.md").write_text(writer.instructions(protocol), encoding="utf-8")
        (out / "batch_01.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        meta = {"kind": "write", "agent": agent, "run_id": run_id, "corpus": corpus, "protocol_sha256": sha,
                "batches": 1, "records": len(data["studies"]), "findings": len(data["findings"]),
                "insights": len(data["insights"])}
        (out / "task.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        return out

    if work_ids is None:
        work_ids = sheet_ids(store, run_id) if kind == "screen" else \
            kept_ids(store, run_id, "title_abstract") if kind == ELIGIBILITY else kept_ids(store, run_id)
        if kind == "findings":
            from .tables import in_scope
            scope = in_scope(store, run_id, protocol, work_ids)
            work_ids = work_ids if scope is None else [w for w in work_ids if w in scope]
    if not work_ids:
        raise ValueError("no records for this task" + (" (no study is kept yet)" if kind != "screen" else ""))
    records = []
    for w in store.db.execute(
            f"SELECT work_id, title, abstract, year, venue, type FROM works WHERE work_id IN "
            f"({','.join('?' * len(work_ids))}) ORDER BY work_id", tuple(work_ids)):
        records.append({k: w[k] for k in ("work_id", "year", "title", "abstract", "venue", "type")})
    brief = 0
    if kind == "findings" and (protocol.get("findings") or {}).get("brief"):
        from .tables import brief_ids
        short = brief_ids(store, run_id, protocol, [r["work_id"] for r in records])
        cap = int(protocol["findings"]["brief"].get("max_per_study", 3))
        for r in records:
            if r["work_id"] in short:
                r["max_findings"] = cap
                brief += 1
    with_fulltext = 0
    if kind in ("extract", "findings", ELIGIBILITY) and use_fulltext:
        pick = findings_text if kind == "findings" else methods_text
        for r in records:
            ft = store.get_fulltext(r["work_id"])
            if ft and ft["status"] == "parsed":
                r["fulltext"] = pick(sections_of(ft))
                r["fulltext_from"] = f"{ft['source']}, {ft['parser']}, licence {ft['license'] or 'not stated'}"
                with_fulltext += 1
        if with_fulltext:
            batch_size = min(batch_size, 6)   # full texts are long

    out = _new_folder(root, corpus, run_id, kind, agent)
    write = {"screen": screen_instructions, ELIGIBILITY: eligibility_instructions, "extract": extract_instructions,
             "findings": findings_instructions}[kind]
    (out / "instructions.md").write_text(write(protocol, with_fulltext), encoding="utf-8")
    batches = [records[i:i + batch_size] for i in range(0, len(records), batch_size)]
    for n, batch in enumerate(batches, 1):
        (out / f"batch_{n:02d}.json").write_text(json.dumps(batch, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = {"kind": kind, "agent": agent, "run_id": run_id, "corpus": corpus, "protocol_sha256": sha,
            "batches": len(batches), "records": len(records), "with_fulltext": with_fulltext}
    if brief:
        meta["brief"] = brief
    if dims:
        meta["dims"] = dims
    if second_coder:
        meta["second_coder"] = True     # its answers go to second_codings, not to the charting
    (out / "task.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return out


def only_dimensions(protocol: dict, names: list[str]) -> dict:
    """The protocol with only these map dimensions: a charting pass for dimensions added later, which leaves the
    study's other charted values as they are."""
    dims = (protocol.get("map") or {}).get("dimensions") or []
    unknown = sorted(set(names) - {d["name"] for d in dims})
    if unknown:
        raise ValueError(f"no map dimension {unknown} in the protocol")
    return dict(protocol, map=dict(protocol["map"], dimensions=[d for d in dims if d["name"] in names]),
                claims=[])


def _write_synthesis(store, run_id, corpus, agent, protocol, sha, root) -> Path:
    rows = store.db.execute(
        """SELECT f.*, w.title, w.year FROM findings f JOIN works w ON w.work_id = f.work_id
           WHERE f.run_id = ? AND f.status != 'superseded' AND f.extractor = (SELECT extractor FROM findings
                                                 WHERE run_id = ? ORDER BY created_at DESC LIMIT 1)
           ORDER BY f.work_id, f.finding_id""", (run_id, run_id)).fetchall()
    if not rows:
        raise ValueError("no findings in this run yet: run a findings task first")
    studies = {}
    for r in rows:
        studies.setdefault(r["work_id"], {"title": r["title"], "year": r["year"]})
    feats = store.db.execute(
        """SELECT work_id, dimension, canonical FROM study_features WHERE run_id = ? AND extractor =
           (SELECT extractor FROM study_features WHERE run_id = ? ORDER BY created_at DESC LIMIT 1)""",
        (run_id, run_id)).fetchall()
    for f in feats:
        if f["work_id"] in studies:
            studies[f["work_id"]].setdefault("map", {})[f["dimension"]] = f["canonical"]
    data = {
        "question": " ".join((protocol.get("findings") or {}).get("question", "").split()),
        "findings": [{"id": r["finding_id"], "work_id": r["work_id"], "finding": r["finding"], "effect": r["effect"],
                      "for_whom": r["for_whom"], "fields": json.loads(r["fields"] or "{}"), "source": r["source"],
                      "evidence": r["evidence"]}
                     for r in rows],
        "studies": {str(k): v for k, v in studies.items()},
        "existing_insights": [{"id": i["insight_id"], "kind": i["kind"], "status": i["status"],
                               "statement": i["statement"]}
                              for i in insights.rows(store, corpus) if i["status"] != "dropped"],
    }
    out = _new_folder(root, corpus, run_id, "synthesize", agent)
    (out / "instructions.md").write_text(synthesis_instructions(protocol), encoding="utf-8")
    (out / "batch_01.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = {"kind": "synthesize", "agent": agent, "run_id": run_id, "corpus": corpus, "protocol_sha256": sha,
            "batches": 1, "records": len(studies), "findings": len(rows)}
    (out / "task.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return out


def sheet_ids(store: Store, run_id: int) -> list[int]:
    return [r[0] for r in store.db.execute(
        "SELECT work_id FROM samples WHERE run_id = ? AND purpose = 'pilot_screening' ORDER BY work_id", (run_id,))]


def screened_ids(store: Store, run_id: int) -> list[int]:
    """Records on the sheet that a screener judged at title and abstract. A record can stay on the sheet unscreened
    by a decision recorded in the protocol (e.g. citation-search records outside a chosen subset); it is not kept."""
    done = {r[0] for r in store.db.execute("SELECT DISTINCT work_id FROM screening WHERE run_id = ? AND stage != ?",
                                           (run_id, FULL_TEXT))}
    return [w for w in sheet_ids(store, run_id) if w in done]


_EXCLUSIONS: dict = {}


def protocol_exclusions(store: Store, run_id: int) -> dict[int, dict]:
    """Records excluded after the eligibility stage by a decision listed in the run's protocol ([[exclusions]],
    each with who, when and why), matched by DOI, or by a source identifier (s2_id, openalex_id, pmid) for a
    record without one, or by the record's id in a source it was retrieved from (`source_id`, optionally with
    `source`, e.g. source = "eric", source_id = "EJ1458030") for a record that has neither."""
    run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return {}
    path = ROOT / "protocol" / f"corpus_{run['corpus'].lower()}.toml"
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {}
    if _EXCLUSIONS.get(path, (None,))[0] != stamp:
        _EXCLUSIONS[path] = (stamp, tomllib.loads(path.read_text(encoding="utf-8")).get("exclusions") or [])
    out = {}
    for e in _EXCLUSIONS[path][1]:
        for key in ("doi", "s2_id", "openalex_id", "pmid"):
            if e.get(key):
                where = "lower(doi) = ?" if key == "doi" else f"{key} = ?"
                value = str(e[key]).lower() if key == "doi" else str(e[key])
                for r in store.db.execute(f"SELECT work_id FROM works WHERE {where}", (value,)):
                    out[r["work_id"]] = e
        if e.get("source_id"):
            where, args = "source_id = ?", [str(e["source_id"])]
            if e.get("source"):
                where, args = where + " AND source = ?", args + [str(e["source"])]
            for r in store.db.execute(f"SELECT DISTINCT work_id FROM retrievals WHERE {where}", args):
                out[r["work_id"]] = e
    return out


def removed_ids(store: Store, run_id: int) -> set[int]:
    """Records whose findings no longer count: excluded by a decision in the protocol, or by a person at title and
    abstract or at the eligibility stage (a person's call wins over the agents')."""
    out = set(protocol_exclusions(store, run_id))
    calls: dict[tuple, list[str]] = {}
    for r in store.db.execute("SELECT work_id, stage, decision FROM screening WHERE run_id = ? AND screener LIKE "
                              "'human:%'", (run_id,)):
        calls.setdefault((r["work_id"], r["stage"]), []).append(r["decision"])
    return out | {w for (w, _), ds in calls.items() if all(d == "exclude" for d in ds)}


def kept_ids(store: Store, run_id: int, stage: str | None = None) -> list[int]:
    """Records on the sheet that are still in: a human's call wins; otherwise any model's include or unsure.
    Records kept at title and abstract are then judged at the eligibility stage, if that has begun; a record
    not yet judged there stays in. stage="title_abstract" stops before the eligibility stage. Records the
    protocol excludes ([[exclusions]]) leave the final set."""
    rows = store.db.execute("SELECT work_id, screener, decision, stage FROM screening WHERE run_id = ?",
                            (run_id,)).fetchall()
    calls = {}
    for r in rows:
        level = calls.setdefault(r["stage"] == FULL_TEXT, {})
        target = level.setdefault("human" if r["screener"].startswith("human:") else "model", {})
        target.setdefault(r["work_id"], []).append(r["decision"])

    def keeps(level, w, default):
        decided = (level.get("human") or {}).get(w) or (level.get("model") or {}).get(w)
        return default if not decided else any(d != "exclude" for d in decided)
    kept = [w for w in sheet_ids(store, run_id) if keeps(calls.get(False, {}), w, False)]
    if stage == "title_abstract":
        return kept
    if True in calls:
        kept = [w for w in kept if keeps(calls[True], w, True)]
    out = protocol_exclusions(store, run_id)
    return [w for w in kept if w not in out] if out else kept


# Instructions, generated from the protocol

def _title(protocol: dict, what: str) -> str:
    return f"# {what}: {protocol.get('title', 'corpus ' + protocol.get('corpus', ''))}"


def _output(example: dict, unit: str, required: str) -> list[str]:
    return [
        "",
        insights.instructions(),
        "",
        f"Output: one JSON object. `records` has one object per {unit} in the batch, in batch order, every "
        "work_id exactly once; `insights` is a list (usually empty):",
        "",
        "```json",
        json.dumps({"records": [example], "insights": []}, ensure_ascii=False),
        "```",
        "",
        required,
    ]


def stage_rules(protocol: dict, kind: str) -> dict:
    """The screening rules of a stage: [screening], or [eligibility] (which borrows what it does not set)."""
    s = protocol.get("screening") or {}
    if kind != ELIGIBILITY:
        return s
    e = protocol.get("eligibility") or {}
    if not e:
        raise ValueError("the protocol has no [eligibility] section")
    return {**s, **e, "stage": FULL_TEXT, "exclude_reasons": e.get("exclude_reasons") or s.get("exclude_reasons", [])}


def eligibility_instructions(protocol: dict, with_fulltext: int = 0) -> str:
    return screen_instructions(protocol, with_fulltext, ELIGIBILITY)


def screen_instructions(protocol: dict, with_fulltext: int = 0, kind: str = "screen") -> str:
    s = stage_rules(protocol, kind)
    lines = [
        _title(protocol, "Screening task" if kind == "screen" else "Eligibility task"),
        "",
        ("You screen records for a literature review at the title-and-abstract stage. Judge each record on "
         "its own, from its title, abstract, venue, year and type only. Do not search the web or look anything up.")
        if kind == "screen" else
        ("You judge the eligibility of records that passed title-and-abstract screening, against the stricter "
         "criteria below. Judge each record on its own, from its title, abstract, venue, year, type and, where "
         "given, `fulltext`. Do not search the web or look anything up."),
        "",
        "Inclusion rule (from the protocol):",
        "",
        "> " + " ".join(s.get("rule", "").split()),
        "",
        "Score every record on every metric below:",
        "- 2: clearly met; 1: cannot tell from what is given; 0: clearly not met.",
        "- For 0 and 2, `evidence` must be words copied exactly from the title, abstract or full text that show it "
        "(a short phrase is enough). For 1, leave evidence empty or quote what made you unsure.",
        "- Never fill a gap with outside knowledge, e.g. what a named dataset usually contains.",
        "",
        "Metrics:",
    ]
    for m in s["metrics"]:
        lines.append(f"- `{m['id']}`: {' '.join(m['question'].split())}")
    lines += [
        "",
        "Also give `kind`: `study` (original empirical data), `review` (review or meta-analysis) or `other` "
        "(editorial, theory, protocol, book, proceedings, front matter).",
        "",
        "You do not decide include or exclude; the pipeline derives that from your scores.",
    ]
    lines += _output({"work_id": 123, "kind": "study",
                      "scores": {m["id"]: {"score": 2, "evidence": "quoted words"} for m in s["metrics"][:2]},
                      "note": "at most 25 words"}, "record", "(`scores` must contain every metric id listed above.)")
    return "\n".join(lines) + "\n"


def extract_instructions(protocol: dict, with_fulltext: int = 0) -> str:
    m = protocol["map"]
    source = "its title, abstract and, where given, `fulltext`" if with_fulltext else "its title and abstract"
    lines = [
        _title(protocol, "Extraction task"),
        "",
        f"For each study in the batch, record what it did, using only {source}. Do not search "
        "the web or add outside knowledge. When the record does not say, use `not_reported`.",
        "",
    ]
    if with_fulltext:
        lines += [
            "Some studies come with `fulltext`: the methods and results sections of an open-access copy "
            "(`fulltext_from` says where it came from). Prefer it for instruments, designs, analyses and "
            "subgroups, which abstracts often leave out. Quotes may come from it.",
            "",
        ]
    lines += [
        "For every dimension give:",
        "- `value`: what the record says, in a few words (several items separated by `; `).",
        "- `canonical`: the short label used to match studies. For dimensions with categories it must be one "
        "of the listed categories (several separated by `; `). Otherwise use the most common short name "
        "(e.g. the standard abbreviation of an instrument), lowercase, so that two studies using the same "
        "thing get the same label. When the record does not say, set both `value` and `canonical` to "
        "`not_reported`; never pick a category for something the record does not report.",
        "- `evidence`: words copied exactly from the record (title, abstract"
        + (" or fulltext" if with_fulltext else "") + "); empty only for not_reported.",
        "",
        "Dimensions:",
    ]
    for d in m["dimensions"]:
        cats = d.get("categories")
        lines.append(f"- `{d['name']}`: {' '.join(d['question'].split())}"
                     + (f" Categories: {', '.join(f'`{c}`' for c in cats)}." if cats else "")
                     + "".join(f"\n  - {' '.join(str(x).split())}" for x in d.get("definitions") or []))
    claims = protocol.get("claims") or []
    if claims:
        lines += ["", "Claims. For each claim below that the study bears on, add an entry to `claims` with "
                  "`relation` supports, challenges or qualifies, an exact `evidence` quote and a one-sentence "
                  "`reason`. Leave out claims the study does not bear on; an empty list is fine.", ""]
        for c in claims:
            lines.append(f"- `{c['id']}` ({c.get('source', '')} {c.get('locator', '')}): {' '.join(c['claim'].split())}")
    lines += _output({"work_id": 123, "features": {d["name"]: {"value": "...", "canonical": "...", "evidence": "..."}
                                                   for d in m["dimensions"][:2]}, "claims": []},
                     "study", "(`features` must contain every dimension listed above.)")
    return "\n".join(lines) + "\n"


def findings_instructions(protocol: dict, with_fulltext: int = 0) -> str:
    f = protocol["findings"]
    limit = int(f.get("max_per_study", 6))
    source = "its title, abstract and, where given, `fulltext`" if with_fulltext else "its title and abstract"
    lines = [
        _title(protocol, "Findings task"),
        "",
        f"For each study in the batch, list the results that bear on the question below, using only {source}. "
        "Do not search the web or add outside knowledge.",
        "",
        "Question: " + " ".join(f["question"].split()),
        "",
    ]
    if with_fulltext:
        lines += ["Some studies come with `fulltext`: the results and discussion (then methods) of an open-access "
                  "copy. Prefer it for effect sizes, subgroups and exact wording.", ""]
    lines += [
        "For each finding give:",
        "- `finding`: one sentence in your own words: what was found, in whom.",
        "- `effect`: the effect size, confidence interval or test exactly as reported; empty if none is given.",
        "- `for_whom`: the subgroup, moderator or trajectory class it applies to; empty if it applies to all.",
        "- `fields`: the fields below.",
        "- `evidence`: the passage that states the finding, copied exactly (one or two sentences).",
        "- `location`: `abstract`, or the heading of the full-text section the evidence comes from.",
        "",
        f"At most {limit} findings per study, the ones that answer the question most directly. Conclusions the "
        "authors draw without data are not findings. A study with nothing relevant gets an empty list.",
        "",
    ]
    brief = f.get("brief") or {}
    if brief:
        lines += [f"A study with `max_findings` gets at most that many findings, on "
                  + " ".join(brief.get("focus", "the question").split()) + ".", ""]
    lines += [
        "Fields:",
    ]
    for d in f.get("fields") or []:
        cats = d.get("categories")
        lines.append(f"- `{d['name']}`: {' '.join(d['question'].split())}"
                     + (f" Categories: {', '.join(f'`{c}`' for c in cats)}." if cats else "")
                     + "".join(f"\n  - {' '.join(str(x).split())}" for x in d.get("definitions") or []))
    example = {"work_id": 123, "findings": [{"finding": "...", "effect": "...", "for_whom": "...",
                                             "fields": {d["name"]: "..." for d in (f.get("fields") or [])[:2]},
                                             "evidence": "exact words", "location": "abstract"}]}
    lines += _output(example, "study", "(`fields` must contain every field listed above.)")
    return "\n".join(lines) + "\n"


def synthesis_instructions(protocol: dict, with_fulltext: int = 0) -> str:
    f = protocol.get("findings") or {}
    return "\n".join([
        _title(protocol, "Synthesis task"),
        "",
        "batch_01.json holds every finding extracted so far for this run (`findings`: id, study, what was found, "
        "effect, for whom, fields, and whether it came from an abstract or a full text) and how each study was "
        "done (`studies`: title, year and map labels).",
        "",
        "Read them together and propose insights that only become visible across studies: patterns that recur, "
        "contradictions between studies, groups or settings nobody has studied, method problems that limit "
        "what can be concluded, hypotheses the findings suggest, open questions. Use only these findings, no "
        "outside knowledge and no other file, and say how strong the support is (how many studies, abstract or "
        "full text). Every number you state must appear in a finding you cite: the pipeline checks this and "
        "flags numbers it cannot find.",
        "",
        "Review question: " + " ".join(f.get("question", "").split()),
        "",
        "`existing_insights` lists what has been noticed already. Do not repeat any of them. Propose only "
        "insights that are new, or that newer findings change; for the latter, name the existing insight's id "
        "in the rationale and say what changed.",
        "",
        "Each insight: `kind` (one of " + ", ".join(insights.KINDS) + "), a one-sentence `statement`, a "
        "`rationale` of two or three sentences, `findings` (the finding ids it rests on, at least one) and "
        "`works` (their work_ids). At most 12 insights; fewer strong ones beat many weak ones.",
        "",
        "Output: one JSON object:",
        "",
        "```json",
        json.dumps({"insights": [{"kind": "pattern", "statement": "...", "rationale": "...",
                                  "findings": [1, 2], "works": [10, 11]}]}),
        "```",
    ]) + "\n"


def check_protocol(protocol: dict, kind: str) -> None:
    if kind in ("screen", ELIGIBILITY):
        s = stage_rules(protocol, kind)
        metrics = s.get("metrics") or []
        if not metrics:
            raise ValueError("the protocol has no [[screening.metrics]]")
        for m in metrics:
            for key in ("id", "question", "fail_reason"):
                if not m.get(key):
                    raise ValueError(f"metric {m.get('id', '?')} needs `{key}`")
            for key in ("fail_reason", "review_reason"):
                if m.get(key) and m[key] not in s.get("exclude_reasons", []):
                    raise ValueError(f"metric {m['id']}: {key} {m[key]!r} is not in exclude_reasons")
    elif kind == "extract":
        dims = (protocol.get("map") or {}).get("dimensions") or []
        if not dims:
            raise ValueError("the protocol has no [[map.dimensions]]")
        for d in dims:
            if not d.get("name") or not d.get("question"):
                raise ValueError("every map dimension needs `name` and `question`")
    elif kind in ("findings", "synthesize", "write") + paper.KINDS + recode.KINDS + lenses.KINDS + novelty.KINDS \
            + directions.KINDS + strong.KINDS:
        f = protocol.get("findings") or {}
        if not f.get("question"):
            raise ValueError("the protocol has no [findings] question")
        for d in f.get("fields") or []:
            if not d.get("name") or not d.get("question"):
                raise ValueError("every findings field needs `name` and `question`")


# Decisions from scores

def decide(scores: dict[str, int], kind: str, metrics: list[dict]) -> tuple[str, str | None]:
    """First metric scored 0, in protocol order, excludes; all 2 includes; anything else is unsure."""
    for m in metrics:
        if scores.get(m["id"]) == 0:
            if kind == "review" and m.get("review_reason"):
                return "exclude", m["review_reason"]
            return "exclude", m["fail_reason"]
    if all(scores.get(m["id"]) == 2 for m in metrics):
        return "include", None
    return "unsure", None


# Grounding: does the quote really occur in the record?

def normalize(text: str | None) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"[‘’‚‛`´]", "'", text)
    text = re.sub(r"[“”„‟]", '"', text)
    text = re.sub(r"[‐-―−]", "-", text)
    # Only real tags: "p < .001 ... α > .80" in a methods section is text, not markup.
    text = re.sub(r"</?[a-z][^<>]{0,200}>", " ", text)
    return " ".join(re.sub(r"[^\w\s'\"\-%.,;:()/]", " ", text).split())


def grounded(evidence: str | None, record_text: str) -> bool | None:
    """None when there is nothing to check; quotes with '...', and several quotes joined by ' | ', are checked part
    by part (every part must occur)."""
    if not evidence or not evidence.strip():
        return None
    haystack = normalize(record_text)
    parts = [p for p in re.split(r"\s*(?:\.\.\.|…|\s\|\s)\s*", evidence) if p.strip()]
    return all(normalize(p).strip(" .,;:") in haystack for p in parts)


def _record_text(r: dict) -> str:
    # `byline`: the authors and their affiliations, added to a batch when a dimension is coded from them
    return f"{r.get('title') or ''} {r.get('abstract') or ''} {r.get('fulltext') or ''} {r.get('byline') or ''}"


# Import

IMPORTED = "imported.json"
BATCH_FILE = re.compile(r"batch_(\d+)\.json")


def batch_files(folder: Path) -> list[Path]:
    """A task folder's batches in batch order: batch_01.json ... batch_99.json, batch_100.json ... (a task of more
    than 99 batches gets three-digit names); never the result files."""
    found = [(int(m.group(1)), p) for p in folder.glob("batch_*.json") if (m := BATCH_FILE.fullmatch(p.name))]
    return [p for _, p in sorted(found)]


def task_state(folder: Path) -> str:
    """written (waiting for an agent), partly answered, answered (ready to import) or imported."""
    if (folder / IMPORTED).exists():
        return "imported"
    batches = batch_files(folder)
    done = [b for b in batches if b.with_name(b.stem + "_result.json").exists()]
    if not done:
        return "written"
    return "answered" if len(done) == len(batches) else "partly answered"


def import_task(folder: Path, store: Store) -> dict:
    if (folder / IMPORTED).exists():
        return {"stored": 0, "problems": [f"already imported ({json.loads((folder / IMPORTED).read_text())['at']})"]}
    counts = _import_task(folder, store)
    if not counts["problems"]:
        (folder / IMPORTED).write_text(json.dumps({"at": now(), **{k: v for k, v in counts.items()
                                                                    if k != "problems"}}), encoding="utf-8")
    return counts


def _import_task(folder: Path, store: Store) -> dict:
    meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    if meta.get("scope") == "discovery":        # across projects: no run, no protocol
        from . import discovery
        return discovery.import_task(folder, store, meta, (folder / "instructions.md").read_text(encoding="utf-8"))
    run_id, agent, kind = meta["run_id"], meta["agent"], meta["kind"]
    run = store.db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    content = store.db.execute("SELECT content FROM protocols WHERE sha256 = ?",
                               (meta["protocol_sha256"],)).fetchone()["content"]
    protocol = tomllib.loads(content)
    if kind == "extract" and meta.get("dims"):
        protocol = only_dimensions(protocol, meta["dims"])
    instructions = (folder / "instructions.md").read_text(encoding="utf-8")
    if kind in evolution.KINDS:
        return evolution.import_task(folder, store, meta, protocol, instructions)
    if kind in referee.KINDS:
        return referee.import_task(folder, store, meta, protocol, instructions)
    if kind in paper.KINDS:
        return paper.import_task(folder, store, meta, protocol, instructions)
    if kind in directions.KINDS:
        return directions.import_task(folder, store, meta, protocol, instructions)
    if kind in recode.KINDS:
        return recode.import_task(folder, store, meta, protocol, instructions)
    if kind in lenses.KINDS:
        return lenses.import_task(folder, store, meta, protocol, instructions)
    if kind in novelty.KINDS:
        return novelty.import_task(folder, store, meta, protocol, instructions)
    if kind in recall.KINDS:
        return recall.import_task(folder, store, meta, protocol, instructions)
    if kind in strong.KINDS:
        return strong.import_task(folder, store, meta, protocol, instructions)

    problems, pending = [], []
    # One agent may answer several batches of a task and see patterns across them: its insights may cite any
    # record of the task, and their quotes are checked against that record's text.
    task_texts = {}
    for batch in batch_files(folder):
        data = json.loads(batch.read_text(encoding="utf-8"))
        if isinstance(data, list):
            task_texts.update({int(r["work_id"]): _record_text(r) for r in data})
    for batch in batch_files(folder):
        result = batch.with_name(batch.stem + "_result.json")
        if not result.exists():
            problems.append(f"{result.name} is missing")
            continue
        data = json.loads(batch.read_text(encoding="utf-8"))
        raw = result.read_text(encoding="utf-8")
        errs, answers, found = _check_answer(kind, data, raw, protocol, set(task_texts))
        problems += [f"{result.name}: {e}" for e in errs]
        pending.append((batch, data, raw, answers, found))
    if problems:
        return {"stored": 0, "problems": problems}

    counts = {"stored": 0, "evidence": 0, "grounded": 0, "insights": 0, "problems": []}
    for batch, data, raw, answers, found in pending:
        batch_text = batch.read_text(encoding="utf-8")
        call_id = store.log_call(
            run_id, "llm", agent,
            f"{instructions}\n\n[{kind} batch {batch.name}, sha256 "
            f"{hashlib.sha256(batch_text.encode('utf-8')).hexdigest()}]",
            200, 0, raw.encode("utf-8"),
            {"agent": agent, "task": kind, "records": len(data) if isinstance(data, list) else len(data["studies"]),
             "protocol_sha256": meta["protocol_sha256"]},
        )
        if isinstance(data, list):
            texts = {int(r["work_id"]): _record_text(r) for r in data}
        else:   # synthesis and writing see findings, so their quotes are checked against those passages
            texts = {}
            for f in data.get("findings", []):
                texts[int(f["work_id"])] = texts.get(int(f["work_id"]), "") + " " + (f.get("evidence") or "") + \
                    " " + (f.get("finding") or "")
        if kind == "write":
            verification = writer.verify(answers, data, store)
            counts["draft"] = writer.store_draft(store, run["corpus"], run_id, agent, answers, verification, call_id)
            counts["verification"] = verification["totals"]
            answers = []
        for a in answers:
            w = int(a["work_id"])
            if kind in ("screen", ELIGIBILITY):
                rules = dict(protocol, screening=stage_rules(protocol, kind))
                _store_screen(store, run, rules, agent, w, a, texts[w], call_id, counts,
                              prefix=f"{ELIGIBILITY}:" if kind == ELIGIBILITY else "")
            elif kind == "extract":
                _store_extract(store, run_id, protocol, agent, w, a, texts[w], call_id, counts,
                               second=bool(meta.get("second_coder")))
            else:
                _store_findings(store, run_id, agent, w, a, texts[w], data, call_id, counts)
            counts["stored"] += 1
        n, checked, ok = insights.store_from_agent(store, run["corpus"], run_id, agent, kind, found,
                                                   task_texts if isinstance(data, list) else texts, call_id, grounded)
        if kind == "synthesize":
            for row in store.db.execute("SELECT insight_id FROM insights WHERE call_id = ?", (call_id,)):
                counts["unverified_numbers"] = counts.get("unverified_numbers", 0) + len(
                    insights.verify_numbers(store, row["insight_id"]))
        counts["insights"] += n
        counts["evidence"] += checked
        counts["grounded"] += ok
    return counts


def _unpack(answer) -> tuple[list, list]:
    """Results are {"records": [...], "insights": [...]}; a bare list (records only) is accepted too."""
    if isinstance(answer, list):
        return answer, []
    if isinstance(answer, dict):
        return answer.get("records") or [], answer.get("insights") or []
    raise ValueError("expected a JSON object or array")


def _check_answer(kind: str, data, raw: str, protocol: dict, task_ids: set[int]) -> tuple[list[str], list, list]:
    """One batch's answer, parsed and checked as the import checks it: (problems, answers, insights). `task_ids`:
    the work_ids of every batch of the task, which an insight may cite."""
    try:
        parsed = json.loads(_strip_fences(raw))
        answers, found = ([], parsed.get("insights") or []) if kind == "write" and isinstance(parsed, dict) \
            else _unpack(parsed)
    except ValueError as err:
        return [f"not valid JSON ({err})"], [], []
    if kind == "write":
        errs = writer.check_shape(parsed, data) + insights.check(found, {int(w) for w in data["studies"]},
                                                                  {f["id"] for f in data["findings"]})
        answers = parsed
    elif kind == "synthesize":
        known_findings = {f["id"] for f in data["findings"]}
        known_works = {int(w) for w in data["studies"]}
        errs = insights.check(found, known_works, known_findings)
        if not found:
            errs.append("no insights returned")
    else:
        check = {"screen": _check_screen, ELIGIBILITY: _check_screen, "extract": _check_extract,
                 "findings": _check_findings}[kind]
        rules = dict(protocol, screening=stage_rules(protocol, kind)) if kind == ELIGIBILITY else protocol
        errs = check(data, answers, rules) + insights.check(found, task_ids)
    return errs, answers, found


# The kinds whose batch holds everything the agent needs, so a model without tools can answer from the prompt.
SELF_CONTAINED = ("screen", ELIGIBILITY, "extract", "findings", "synthesize", "write") + recode.KINDS + recall.KINDS


def answer_checker(folder: Path, store: Store):
    """The import's checks for one batch, for a runner to apply before it writes batch_NN_result.json, so that one
    malformed answer cannot block the import of the whole task. Returns check(batch_data, raw) -> problems."""
    meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    kind = meta.get("kind")
    if meta.get("scope") == "discovery" or kind not in SELF_CONTAINED:
        raise ValueError(f"{kind} tasks need an agent that reads the task's files itself; a model answering from the "
                         f"prompt can do {', '.join(SELF_CONTAINED)}")
    row = store.db.execute("SELECT content FROM protocols WHERE sha256 = ?", (meta["protocol_sha256"],)).fetchone()
    if row is None:
        raise ValueError("the task's protocol is not in this evidence store (EVIDENCE_DB points elsewhere?)")
    protocol = tomllib.loads(row["content"])
    if kind == "extract" and meta.get("dims"):
        protocol = only_dimensions(protocol, meta["dims"])

    def parsed(raw, check):
        try:
            return check(json.loads(_strip_fences(raw)))
        except ValueError as err:
            return [f"not valid JSON ({err})"]

    if kind in recode.KINDS:
        fields = recode.only_fields(protocol, meta.get("fields"))
        return lambda data, raw: parsed(raw, lambda answer: recode.check(data, answer, fields))
    if kind in recall.KINDS:
        return lambda data, raw: parsed(raw, lambda answer: _check_screen(data, _unpack(answer)[0], protocol))
    task_ids = set()
    for batch in batch_files(folder):
        data = json.loads(batch.read_text(encoding="utf-8"))
        if isinstance(data, list):
            task_ids.update(int(r["work_id"]) for r in data)
    return lambda data, raw: _check_answer(kind, data, raw, protocol, task_ids)[0]


def recheck_quotes(folder: Path, store: Store) -> dict:
    """Recompute the quote check for an imported task (after the check itself changed); logs no new calls."""
    meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    run_id, agent = meta["run_id"], meta["agent"]
    texts = {}
    for batch in batch_files(folder):
        data = json.loads(batch.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for r in data:
                texts[int(r["work_id"])] = _record_text(r)
    tables = {"screening_scores": ("run_id = ? AND screener = ?", "metric"),
              "study_features": ("run_id = ? AND extractor = ?", "dimension"),
              "claim_links": ("run_id = ? AND extractor = ?", "claim"),
              "findings": ("run_id = ? AND extractor = ?", "finding_id")}
    counts = {"checked": 0, "changed": 0, "grounded": 0}
    with store.db:
        for table, (where, key) in tables.items():
            for r in store.db.execute(f"SELECT work_id, {key} AS k, evidence, grounded FROM {table} WHERE {where}",
                                      (run_id, agent)).fetchall():
                if r["work_id"] not in texts or not r["evidence"]:
                    continue
                g = grounded(r["evidence"], texts[r["work_id"]])
                counts["checked"] += 1
                counts["grounded"] += bool(g)
                if (None if g is None else int(g)) != r["grounded"]:
                    counts["changed"] += 1
                    store.db.execute(f"UPDATE {table} SET grounded = ? WHERE {where} AND work_id = ? AND {key} = ?",
                                     (None if g is None else int(g), run_id, agent, r["work_id"], r["k"]))
    return counts


def _store_screen(store, run, protocol, agent, w, a, text, call_id, counts, prefix=""):
    """`prefix` keeps a later stage's scores apart from the title-and-abstract scores of the same metric ids."""
    metrics = protocol["screening"]["metrics"]
    scores = {}
    for m in metrics:
        entry = a["scores"][m["id"]]
        g = grounded(entry.get("evidence"), text)
        counts["evidence"] += g is not None
        counts["grounded"] += bool(g)
        scores[m["id"]] = int(entry["score"])
        store.add_score(run["run_id"], w, agent, prefix + m["id"], scores[m["id"]], entry.get("evidence") or None, g,
                        call_id)
    decision, reason = decide(scores, a.get("kind", "study"), metrics)
    store.add_screening(run["run_id"], w, run["corpus"], protocol["screening"]["stage"], agent, decision, reason,
                        a.get("note") or None, call_id)


def _store_extract(store, run_id, protocol, agent, w, a, text, call_id, counts, second=False):
    """`second`: an independent second coding, kept apart from the charting (patterns.second_codings)."""
    from . import patterns
    for d in protocol["map"]["dimensions"]:
        f = a["features"][d["name"]]
        value = (f.get("value") or NOT_REPORTED).strip()
        canonical = (f.get("canonical") or value).strip().lower()
        # A category is no stand-in for "not reported" (agents sometimes pick "other"); only "none", which itself
        # says the thing is absent, is kept.
        if value.lower() == NOT_REPORTED and canonical != "none":
            canonical = NOT_REPORTED
        g = grounded(f.get("evidence"), text) if value != NOT_REPORTED else None
        counts["evidence"] += g is not None
        counts["grounded"] += bool(g)
        if second:
            patterns.add_second(store, run_id, w, agent, d["name"], value, canonical, f.get("evidence") or None, g,
                                call_id)
        else:
            store.add_feature(run_id, w, agent, d["name"], value, canonical, f.get("evidence") or None, g, call_id)
    for c in a.get("claims") or []:
        g = grounded(c.get("evidence"), text)
        counts["evidence"] += g is not None
        counts["grounded"] += bool(g)
        store.add_claim_link(run_id, w, agent, c["claim"], c["relation"], c.get("evidence") or None, g,
                             c.get("reason") or None, call_id)


def _store_findings(store, run_id, agent, w, a, text, data, call_id, counts):
    record = next(r for r in data if int(r["work_id"]) == w)
    abstract_text = f"{record.get('title') or ''} {record.get('abstract') or ''}"
    rows = []
    for f in a.get("findings") or []:
        g = grounded(f["evidence"], text)
        source = "abstract" if grounded(f["evidence"], abstract_text) else ("fulltext" if g else None)
        counts["evidence"] += 1
        counts["grounded"] += bool(g)
        rows.append({**f, "fields": {k: (v.strip().lower() if isinstance(v, str) else v)
                                     for k, v in (f.get("fields") or {}).items()},
                     "grounded": g, "source": source})
    store.replace_findings(run_id, w, agent, rows, call_id)
    store.add_findings_read(run_id, w, agent, "full text" if (record.get("fulltext") or "").strip() else "abstract",
                            len(rows), call_id)


def _check_ids(records, answers) -> list[str]:
    if not isinstance(answers, list):
        return ["`records` must be a list"]
    expected = sorted(int(r["work_id"]) for r in records)
    try:
        got = [int(a["work_id"]) for a in answers]
    except (KeyError, TypeError, ValueError):
        return ["every record needs an integer work_id"]
    errs = []
    if sorted(got) != expected:
        missing = sorted(set(expected) - set(got))
        extra = sorted(set(got) - set(expected))
        errs.append(f"work_ids differ from the batch (missing {missing[:10]}, unexpected {extra[:10]})")
    if len(got) != len(set(got)):
        errs.append("a work_id appears twice")
    return errs


def _check_screen(records, answers, protocol) -> list[str]:
    errs = _check_ids(records, answers)
    if errs:
        return errs
    ids = [m["id"] for m in protocol["screening"]["metrics"]]
    for a in answers:
        if a.get("kind", "study") not in RECORD_KINDS:
            errs.append(f"{a['work_id']}: kind {a.get('kind')!r}")
        scores = a.get("scores") or {}
        for i in ids:
            entry = scores.get(i)
            if not isinstance(entry, dict) or entry.get("score") not in (0, 1, 2):
                errs.append(f"{a['work_id']}: metric {i} needs a score of 0, 1 or 2")
    return errs


def _check_extract(records, answers, protocol) -> list[str]:
    errs = _check_ids(records, answers)
    if errs:
        return errs
    dims = protocol["map"]["dimensions"]
    claim_ids = {c["id"] for c in protocol.get("claims") or []}
    for a in answers:
        feats = a.get("features") or {}
        for d in dims:
            f = feats.get(d["name"])
            if not isinstance(f, dict):
                errs.append(f"{a['work_id']}: dimension {d['name']} is missing")
                continue
            canonical = (f.get("canonical") or f.get("value") or NOT_REPORTED).strip().lower()
            errs += _check_category(a["work_id"], d, canonical)
        for c in a.get("claims") or []:
            if c.get("claim") not in claim_ids:
                errs.append(f"{a['work_id']}: unknown claim {c.get('claim')!r}")
            if c.get("relation") not in RELATIONS:
                errs.append(f"{a['work_id']}: relation {c.get('relation')!r}")
    return errs


def _check_findings(records, answers, protocol) -> list[str]:
    errs = _check_ids(records, answers)
    if errs:
        return errs
    spec = protocol["findings"]
    limit = int(spec.get("max_per_study", 6))
    caps = {int(r["work_id"]): int(r.get("max_findings") or limit) for r in records}
    for a in answers:
        items = a.get("findings")
        if not isinstance(items, list):
            errs.append(f"{a['work_id']}: `findings` must be a list")
            continue
        cap = caps.get(int(a["work_id"]), limit)
        if len(items) > cap:
            errs.append(f"{a['work_id']}: {len(items)} findings, at most {cap}")
        for i, f in enumerate(items, 1):
            if not (f.get("finding") or "").strip() or not (f.get("evidence") or "").strip():
                errs.append(f"{a['work_id']} finding {i}: needs `finding` and `evidence`")
            fields = f.get("fields") or {}
            for d in spec.get("fields") or []:
                if d["name"] not in fields:
                    errs.append(f"{a['work_id']} finding {i}: field {d['name']} is missing")
                    continue
                errs += _check_category(f"{a['work_id']} finding {i}", d, str(fields[d["name"]]).strip().lower())
    return errs


def _check_category(where, spec: dict, canonical: str) -> list[str]:
    cats = spec.get("categories")
    if not cats or canonical in (NOT_REPORTED, ""):
        return []
    allowed = {c.lower() for c in cats}
    bad = [c for c in split_values(canonical) if c not in allowed]
    return [f"{where}: {spec['name']} {bad} not in {cats}"] if bad else []


def split_values(canonical: str) -> list[str]:
    return [v.strip() for v in canonical.split(";") if v.strip()]


def _strip_fences(raw: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    return m.group(1) if m else raw
