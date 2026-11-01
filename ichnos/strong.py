"""The strong-evidence aim: a few dozen studies with the strongest designs, each read for what changed, by how much and
for whom, in place of counting directions across every study.

An aim of kind "strong" names its selection rule in `[aims.select]`: the order of designs (`tiers`), how many studies
each focused rule gets (`quota`) and how many may come from one country (`per_country`). Two agent tasks serve it:

- `appraise`: every candidate (a study of the focused view with a finding from an experiment or a natural
  experiment) is described on a fixed rubric: how its effect is identified, the source of variation, the natural
  experiment it uses, publication, outcome and the main threat, with the passage that shows the design. Code then ranks
  the candidates and applies the rule (`select`), before any effect is extracted, so the set is fixed by design alone.
- `effects`: each selected study, read in full text, gives every estimate of an effect on births: the exposure and its
  unit, the outcome, the estimate as reported, the population and the horizon, with its verbatim passage. Estimates are
  stored as findings outside the run (run_id NULL, extractor "<agent>:effects"), so the run's evidence map and its
  counts stay as they are, while a paper can cite an estimate like any finding.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from . import tables
from .store import Store, now

KINDS = ("appraise", "effects")
TIERS = ("randomised", "regression_discontinuity", "difference_in_differences", "instrumental_variable",
         "other_natural_experiment")
IDENTIFICATION = TIERS + ("not_causal",)
PUBLICATION = ("peer_reviewed", "working_paper", "thesis", "other")
OUTCOME = ("number", "both", "timing", "intentions", "other")      # what the study measures, in the order preferred
CAUSAL = ("natural_experiment", "experiment")
EFFECTS_SUFFIX = ":effects"
ESTIMATE_OUTCOMES = ("first_birth_timing", "birth_timing", "completed_fertility", "births_by_age", "childlessness",
                     "birth_rate", "intentions", "other")
DIRECTIONS = ("more", "fewer", "earlier", "later", "no_effect")

SCHEMA = """
CREATE TABLE IF NOT EXISTS study_appraisals (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    appraiser   TEXT NOT NULL,
    content     TEXT NOT NULL,                   -- JSON: the rubric's answers
    grounded    INTEGER,                         -- the design passage occurs in what the appraiser read
    read_from   TEXT NOT NULL,                   -- full text or abstract
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, work_id, appraiser)
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def candidates(store: Store, run_id: int, protocol: dict) -> dict[int, list[str]]:
    """{work_id: the focused rules it is chosen for}, for the studies of the focused view with at least one finding
    from an experiment or a natural experiment."""
    from .rulemap import selection
    chosen, _ = selection(store, run_id, protocol)
    out: dict[int, list[str]] = {}
    for rule, by_study in chosen.items():
        for w, (_, fs) in by_study.items():
            if any(set(str(f["fields"].get("evidence_type") or "").replace(" ", "").split(";")) & set(CAUSAL)
                   for f in fs):
                out.setdefault(w, []).append(rule)
    return out


def _fulltext(store: Store, work_id: int, pick: str, limit: int) -> tuple[str | None, str | None]:
    from .fulltext import findings_text, methods_text, sections_of
    ft = store.get_fulltext(work_id)
    if not ft or ft["status"] != "parsed":
        return None, None
    sections = sections_of(ft)
    if pick == "results" and sum(len(s.get("text") or "") for s in sections) > limit:
        text = _birth_sections(sections, limit)
    else:
        text = (methods_text if pick == "methods" else findings_text)(sections, limit)
    return text, f"{ft['source']}, {ft['parser']}, licence {ft['license'] or 'not stated'}"


BIRTH_TERMS = re.compile(r"fertil|births?\b|childbearing|children ever born|first birth|childless|number of children|"
                         r"motherhood|pregnan", re.I)


def _birth_sections(sections: list[dict], limit: int) -> str:
    """A long text (a thesis, a book) cut to the sections that speak most of births, kept in their order: a thesis's
    fertility chapter may lie far beyond the first results section."""
    scored = sorted(range(len(sections)), key=lambda i: -len(BIRTH_TERMS.findall(sections[i].get("text") or "")))
    keep, used = set(), 0
    for i in scored:
        n = len(sections[i].get("text") or "")
        if not BIRTH_TERMS.search(sections[i].get("text") or "") or used >= limit:
            break
        keep.add(i)
        used += n
    parts = [(f"## {sections[i].get('heading')}\n" if sections[i].get("heading") else "") + (sections[i].get("text") or "")
             for i in sorted(keep)]
    return "\n\n".join(parts)[:limit]


def appraise_instructions() -> str:
    cat = lambda xs: ", ".join(f"`{x}`" for x in xs)   # noqa: E731
    return "\n".join([
        "# Appraisal task: how strong is each study's design?", "",
        "Each batch_NN.json lists studies of a review on what changes fertility, each with its title, abstract, where "
        "available `fulltext` (methods first, then results), and the review's findings from it. Describe each study's "
        "design on the rubric below, from the batch alone. Do not search the web or add outside knowledge.", "",
        "For each study give:",
        f"- `identification`: how the study identifies the effect on births. One of {cat(IDENTIFICATION)}. "
        "`randomised`: assignment by the researchers or by lottery. `regression_discontinuity`: a cut-off in birth "
        "date, age, score or eligibility. `difference_in_differences`: a reform or shock compared before and after "
        "with a comparison group (event studies included). `instrumental_variable`: an instrument such as a reform "
        "that shifts the exposure. `other_natural_experiment`: an exogenous shock or reform without one of those "
        "designs (for example a plain cohort comparison around a reform). `not_causal`: none of these, whatever the "
        "study calls itself.",
        "- `variation`: one sentence: what changed, for whom, where and when (the reform, shock or assignment).",
        "- `experiment`: a short name for the natural experiment or trial, the same for every study that uses it "
        "(\"Netherlands 2006 pension age reform\", \"Turkey 1997 compulsory schooling reform\"); for a study with no "
        "shared source, the study's own short name.",
        "- `country`: the country (or \"several\").",
        f"- `publication`: {cat(PUBLICATION)}, from the record (venue, type, wording).",
        f"- `outcome`: what the study measures: {cat(OUTCOME)}. `number`: completed fertility, children ever born or "
        "childlessness; `timing`: age at or hazard of a birth; `both`: both.",
        "- `completed`: true if fertility is observed to age 40 or later (or completed fertility is reported).",
        "- `sample`: the sample as reported (size and who), a few words.",
        "- `threat`: the main threat to the design in one sentence (what else could explain the effect).",
        "- `evidence`: the passage that shows the design, copied exactly (one or two sentences).", "",
        "Output: one JSON object per batch, written to batch_NN_result.json:", "",
        "```json",
        json.dumps({"studies": [{"work_id": 123, "identification": "regression_discontinuity", "variation": "...",
                                 "experiment": "...", "country": "...", "publication": "peer_reviewed",
                                 "outcome": "both", "completed": True, "sample": "...", "threat": "...",
                                 "evidence": "exact words"}]}, ensure_ascii=False),
        "```", "", "Every study of the batch exactly once."]) + "\n"


def effects_instructions() -> str:
    cat = lambda xs: ", ".join(f"`{x}`" for x in xs)   # noqa: E731
    return "\n".join([
        "# Effects task: what changed, by how much, for whom", "",
        "Each batch_NN.json lists studies with a strong design, each with its title, abstract, `fulltext` (results "
        "first), the design as appraised (`design`), and the review's earlier findings from it. List the study's "
        "estimates of the effect of its exposure on fertility, precisely and from the batch alone. Do not search the "
        "web or add outside knowledge.", "",
        "For each estimate give:",
        "- `statement`: one sentence in plain words: what changed, in whom, and what happened to births, with the "
        "number and its unit (\"each extra year of schooling delayed the first birth by 0.42 years among Kenyan "
        "women born 1977-1986\").",
        "- `exposure`: the change the estimate is about and its unit (\"one more year of schooling\", \"the reform\", "
        "\"one more year of the grandmother's retirement delay\").",
        f"- `outcome`: {cat(ESTIMATE_OUTCOMES)}, and `outcome_detail`: the outcome as the study defines it.",
        "- `estimate`: the estimate as reported, with its unit; `uncertainty`: its standard error, confidence interval "
        "or significance as reported (empty if none); `relative`: the effect relative to the mean or baseline if the "
        "study reports it (empty otherwise).",
        f"- `direction`: {cat(DIRECTIONS)}: what the exposure does to births.",
        "- `population`: who (country, cohorts or years, sex, other conditions).",
        "- `horizon`: when fertility is observed (\"by age 37\", \"within 3 years\", \"completed\").",
        "- `headline`: true for the study's main estimate (one or two per study).",
        "- `evidence`: the passage that states the estimate, copied exactly (one or two sentences; a table row's words "
        "are allowed if the text gives none); `location`: the section heading.", "",
        "At most 6 estimates per study: the main estimate, the split between timing and number of births, and the "
        "subgroups that matter (women and men, kin type, parity). Do not convert or combine numbers; report them as the "
        "study does. A study with no estimate of an effect on fertility gets an empty list.", "",
        "Output: one JSON object per batch, written to batch_NN_result.json:", "",
        "```json",
        json.dumps({"studies": [{"work_id": 123, "estimates": [{
            "statement": "...", "exposure": "...", "outcome": "completed_fertility", "outcome_detail": "...",
            "estimate": "...", "uncertainty": "...", "relative": "", "direction": "fewer", "population": "...",
            "horizon": "...", "headline": True, "evidence": "exact words", "location": "Results"}]}]},
            ensure_ascii=False),
        "```", "", "Every study of the batch exactly once."]) + "\n"


def write_task(store: Store, run_id: int, kind: str, agent: str, protocol: dict, sha: str, root: Path,
               options: dict, batch_size: int = 8) -> Path:
    from .agents import _new_folder
    from . import aim as aim_mod
    ensure(store)
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    protocol = aim_mod.with_aim(protocol, options.get("aim"))
    if (protocol.get("aim") or {}).get("kind") != "strong":
        raise ValueError(f"{kind} needs --aim naming an aim of kind \"strong\"")
    cands = candidates(store, run_id, protocol)
    if kind == "appraise":
        ids = sorted(cands)
    else:
        picked = select(store, run_id, protocol)
        ids = sorted(w for w in picked["selected"] if picked["fulltext"].get(w))
        if options.get("works"):                     # a later reading of the studies that gained a full text
            ids = [w for w in ids if w in set(options["works"])]
        if not ids:
            raise ValueError("no selected study has a full text yet")
    finds = {}
    for f in tables.finding_rows(store, run_id):
        if f["work_id"] in cands:
            finds.setdefault(f["work_id"], []).append({"finding": f["finding"], "effect": f["effect"],
                                                       "for_whom": f["for_whom"]})
    meta = tables.works(store, ids, run_id)
    designs = latest_appraisals(store, run_id) if kind == "effects" else {}
    records, read = [], Counter()
    for w in ids:
        text, source = _fulltext(store, w, "methods" if kind == "appraise" else "results",
                                 14000 if kind == "appraise" else 60000)
        read["full text" if text else "abstract"] += 1
        r = {"work_id": w, "cite": meta[w]["cite"], "year": meta[w]["year"], "title": meta[w]["title"],
             "venue": meta[w]["venue"], "type": meta[w]["type"], "abstract": meta[w]["abstract"],
             "rules": cands[w], "findings": finds.get(w, [])}
        if text:
            r.update(fulltext=text, fulltext_from=source)
        if kind == "effects":
            r["design"] = {k: designs[w].get(k) for k in ("identification", "variation", "experiment", "outcome")} \
                if w in designs else {}
        records.append(r)
    out = _new_folder(root, corpus, run_id, kind, agent)
    batches = [records[i:i + batch_size] for i in range(0, len(records), batch_size)]
    for n, b in enumerate(batches, 1):
        (out / f"batch_{n:02d}.json").write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "instructions.md").write_text(appraise_instructions() if kind == "appraise" else effects_instructions(),
                                         encoding="utf-8")
    (out / "task.json").write_text(json.dumps({"kind": kind, "agent": agent, "run_id": run_id, "corpus": corpus,
                                               "protocol_sha256": sha, "aim": options.get("aim"),
                                               "batches": len(batches), "records": len(records),
                                               "read": dict(read)}, indent=1), encoding="utf-8")
    return out


def _check(kind: str, batch: list[dict], answer) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("studies"), list):
        return ['the answer must be {"studies": [...]}']
    want = sorted(r["work_id"] for r in batch)
    got = sorted(int(a.get("work_id", -1)) for a in answer["studies"])
    errs = [] if got == want else [f"every study exactly once (missing {sorted(set(want) - set(got))}, "
                                   f"unknown {sorted(set(got) - set(want))})"]
    for a in answer["studies"]:
        where = f"study {a.get('work_id')}"
        if kind == "appraise":
            for key, allowed in (("identification", IDENTIFICATION), ("publication", PUBLICATION),
                                 ("outcome", OUTCOME)):
                if str(a.get(key) or "").strip().lower() not in allowed:
                    errs.append(f"{where}: {key} must be one of {allowed}")
            for key in ("variation", "experiment", "threat", "evidence"):
                if not str(a.get(key) or "").strip():
                    errs.append(f"{where}: {key} is missing")
        else:
            ests = a.get("estimates")
            if not isinstance(ests, list) or len(ests) > 6:
                errs.append(f"{where}: estimates must be a list of at most 6")
                continue
            for i, e in enumerate(ests, 1):
                if str(e.get("outcome") or "").strip().lower() not in ESTIMATE_OUTCOMES:
                    errs.append(f"{where} estimate {i}: outcome must be one of {ESTIMATE_OUTCOMES}")
                if str(e.get("direction") or "").strip().lower() not in DIRECTIONS:
                    errs.append(f"{where} estimate {i}: direction must be one of {DIRECTIONS}")
                for key in ("statement", "exposure", "estimate", "population", "evidence"):
                    if not str(e.get(key) or "").strip():
                        errs.append(f"{where} estimate {i}: {key} is missing")
    return errs


def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions_text: str) -> dict:
    from .agents import _record_text, _strip_fences, batch_files, grounded
    ensure(store)
    kind, run_id, agent = meta["kind"], meta["run_id"], meta["agent"]
    problems, pending = [], []
    for batch in batch_files(folder):
        result = batch.with_name(batch.stem + "_result.json")
        if not result.exists():
            problems.append(f"{result.name} is missing")
            continue
        data = json.loads(batch.read_text(encoding="utf-8"))
        raw = result.read_text(encoding="utf-8")
        try:
            answer = json.loads(_strip_fences(raw))
        except ValueError as err:
            problems.append(f"{result.name}: not valid JSON ({err})")
            continue
        errs = _check(kind, data, answer)
        if errs:
            problems += [f"{result.name}: {e}" for e in errs]
            continue
        pending.append((batch, data, raw, answer))
    if problems:
        return {"stored": 0, "problems": problems}
    counts = Counter()
    for batch, data, raw, answer in pending:
        call_id = store.log_call(run_id, "llm", agent, f"{instructions_text}\n\n[{kind} batch {batch.name}, sha256 "
                                 f"{hashlib.sha256(batch.read_bytes()).hexdigest()}]", 200, 0, raw.encode("utf-8"),
                                 {"agent": agent, "task": kind, "aim": meta.get("aim")})
        records = {r["work_id"]: r for r in data}
        with store.db:
            for a in answer["studies"]:
                w = int(a["work_id"])
                text = _record_text(records[w])
                read_from = "full text" if records[w].get("fulltext") else "abstract"
                if kind == "appraise":
                    content = {k: a.get(k) for k in ("identification", "variation", "experiment", "country",
                                                     "publication", "outcome", "completed", "sample", "threat",
                                                     "evidence")}
                    for k in ("identification", "publication", "outcome"):
                        content[k] = str(content[k]).strip().lower()
                    ok = grounded(a.get("evidence"), text)
                    store.db.execute("INSERT OR REPLACE INTO study_appraisals (run_id, work_id, appraiser, content, "
                                     "grounded, read_from, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                     (run_id, w, agent, json.dumps(content, ensure_ascii=False),
                                      None if ok is None else int(ok), read_from, call_id, now()))
                    counts["stored"] += 1
                    counts["grounded"] += bool(ok)
                else:
                    counts["stored"] += _store_estimates(store, run_id, agent, w, a["estimates"], records[w], text,
                                                         call_id, counts)
    return {**counts, "problems": []}


def _store_estimates(store, run_id, agent, w, estimates, record, text, call_id, counts) -> int:
    """A study's estimates replace that extractor's earlier ones; each is a finding outside the run."""
    from .agents import grounded
    extractor = agent + EFFECTS_SUFFIX
    store.db.execute("UPDATE findings SET status = 'superseded' WHERE work_id = ? AND extractor = ? AND run_id IS NULL",
                     (w, extractor))
    for e in estimates:
        ok = grounded(e.get("evidence"), text)
        fields = {"set": "strong", "corpus_run": run_id, "rule": "; ".join(record.get("rules") or []),
                  **{k: (str(e.get(k) or "").strip().lower() if k in ("outcome", "direction") else e.get(k))
                     for k in ("exposure", "outcome", "outcome_detail", "estimate", "uncertainty", "relative",
                               "direction", "population", "horizon", "headline")}}
        effect = "; ".join(str(x) for x in (e.get("estimate"), e.get("uncertainty"), e.get("relative")) if x)
        store.db.execute(
            "INSERT INTO findings (work_id, run_id, extractor, finding, effect, for_whom, fields, evidence, location, "
            "source, grounded, call_id, created_at) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (w, extractor, e["statement"].strip(), effect, e.get("population"), json.dumps(fields, ensure_ascii=False),
             e["evidence"].strip(), e.get("location"), "fulltext" if record.get("fulltext") else "abstract",
             None if ok is None else int(ok), call_id, now()))
        counts["grounded"] += bool(ok)
    return len(estimates)


def latest_appraisals(store: Store, run_id: int) -> dict[int, dict]:
    ensure(store)
    out = {}
    for r in store.db.execute("SELECT * FROM study_appraisals WHERE run_id = ? ORDER BY created_at", (run_id,)):
        out[r["work_id"]] = {**json.loads(r["content"]), "grounded": r["grounded"], "read_from": r["read_from"]}
    return out


def rank_key(a: dict, year) -> tuple:
    """The order of the selection rule: design first, then a measure of the number of births, then publication, then
    the more recent study."""
    tier = TIERS.index(a["identification"]) if a.get("identification") in TIERS else len(TIERS)
    return (tier, OUTCOME.index(a["outcome"]) if a.get("outcome") in OUTCOME else len(OUTCOME),
            0 if a.get("completed") else 1,
            PUBLICATION.index(a["publication"]) if a.get("publication") in PUBLICATION else len(PUBLICATION),
            -(year or 0))


def select(store: Store, run_id: int, protocol: dict) -> dict:
    """The rule of `[aims.select]` applied to the appraisals: candidates that are not causal drop out; of studies
    sharing a natural experiment the first in rank order stays; then each rule, in the rule's `order` (the scarcest
    first, so that a study chosen for two rules goes to the one with fewer studies), takes its
    quota in rank order, at most `per_country` from one country, skipping studies another rule already took."""
    rule = (protocol.get("aim") or {}).get("select") or {}
    quota = rule.get("quota") or {}
    cap = int(rule.get("per_country", 99))
    cands = candidates(store, run_id, protocol)
    apps = latest_appraisals(store, run_id)
    years = {w: r["year"] for w, r in tables.works(store, cands).items()}
    missing = sorted(set(cands) - set(apps))
    causal = {w for w in cands if w in apps and apps[w]["identification"] in TIERS}
    # `births_only`: a study that measures only intentions or attitudes says nothing about births
    off = {w for w in causal if rule.get("births_only") and apps[w].get("outcome") == "intentions"}
    causal -= off
    order = sorted(causal, key=lambda w: (rank_key(apps[w], years.get(w)), w))
    # `same`: {canonical name: [other names]} for one natural experiment that appraisers named differently
    alias = {" ".join(v.lower().split()): " ".join(k.lower().split())
             for k, vs in (rule.get("same") or {}).items() for v in vs}
    seen, dropped = {}, {}
    for w in order:
        key = " ".join(str(apps[w].get("experiment") or w).lower().split())
        key = alias.get(key, key)
        if key in seen:
            dropped[w] = seen[key]
        else:
            seen[key] = w
    pool = [w for w in order if w not in dropped]
    selected, by_rule = {}, {}
    rules = rule.get("order") or (protocol.get("focus") or {}).get("rules") or list(quota)
    for r in rules:
        # `total`: the last rule takes what the others left of the total, so the set keeps its size
        want = int(rule["total"]) - len(selected) if rule.get("total") and r == rules[-1] else int(quota.get(r, 0))
        taken, countries = [], Counter()
        for w in pool:
            if len(taken) >= want:
                break
            if w in selected or r not in cands[w]:
                continue
            c = str(apps[w].get("country") or "").strip().lower()
            if c and countries[c] >= cap:
                continue
            taken.append(w)
            selected[w] = r
            countries[c] += 1
        by_rule[r] = taken
    fulltext = {w: bool(store.get_fulltext(w) and store.get_fulltext(w)["status"] == "parsed") for w in selected}
    return {"candidates": len(cands), "appraised": len(cands) - len(missing), "missing": missing,
            "not_causal": sorted(set(cands) - causal - off - set(missing)), "intentions_only": sorted(off),
            "same_experiment": dropped,
            "eligible": {r: sum(1 for w in pool if r in cands[w]) for r in by_rule},
            "by_rule": by_rule, "selected": selected, "fulltext": fulltext}


def estimates(store: Store, work_ids) -> list[dict]:
    ids = list(work_ids)
    if not ids:
        return []
    rows = store.db.execute(f"SELECT * FROM findings WHERE run_id IS NULL AND extractor LIKE ? AND status != "
                            f"'superseded' AND work_id IN ({','.join('?' * len(ids))}) ORDER BY work_id, finding_id",
                            ("%" + EFFECTS_SUFFIX, *ids)).fetchall()
    return [{**dict(r), "fields": json.loads(r["fields"] or "{}")} for r in rows]


# The paper: its evidence, its tables and its method facts

def _label(value) -> str:
    return str(value or "").replace("_", " ")


def writer_findings(store: Store, run_id: int, protocol: dict) -> list[dict]:
    """The estimates of the selected studies, in the shape the writer's findings have."""
    picked = select(store, run_id, protocol)
    return [{"id": e["finding_id"], "work_id": e["work_id"], "finding": e["finding"], "effect": e["effect"],
             "for_whom": e["for_whom"], "fields": {**e["fields"], "rule": picked["selected"].get(e["work_id"])},
             "evidence": e["evidence"], "source": e["source"]}
            for e in estimates(store, picked["selected"])]


def _cell(text, n: int = 240) -> str:
    text = " ".join(str(text or "").split()).replace("|", "/")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def effects_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """Table 1 of a strong-evidence paper: per rule, each selected study with what changed, its design and its main
    estimates as reported."""
    picked = select(store, run_id, protocol)
    apps = latest_appraisals(store, run_id)
    meta = tables.works(store, picked["selected"], run_id)
    ests: dict[int, list[dict]] = {}
    for e in estimates(store, picked["selected"]):
        ests.setdefault(e["work_id"], []).append(e)
    lines = [f"**{label}.** The strongest studies and what they found: for each rule, the studies selected by design "
             "under the rule fixed before any effect was read (one per natural experiment; design tier, then a measure "
             "of the number of births, then publication), with what changed, the design and the main estimates as the "
             "studies report them. Estimates are not converted to a common scale.", "",
             "| Rule | Study | Setting | What changed | Design | Main estimates | Main threat |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    for rule, ids in picked["by_rule"].items():
        for w in ids:
            a = apps.get(w, {})
            main = [e for e in ests.get(w, []) if e["fields"].get("headline")] or ests.get(w, [])[:2]
            shown = "; ".join(_cell(f"{e['fields'].get('estimate')} ({_label(e['fields'].get('outcome'))}"
                                    + (f", {e['fields'].get('horizon')}" if e["fields"].get("horizon") else "") + ")", 160)
                              for e in main[:2]) or "not yet read in full"
            lines.append(f"| {_label(rule)} | {meta[w]['cite']} | {_cell(a.get('country'), 40)} | "
                         f"{_cell(a.get('variation'), 200)} | {_label(a.get('identification'))} | {shown} | "
                         f"{_cell(a.get('threat'), 160)} |")
    return "\n".join(lines)


def appraisal_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """Supplement: every candidate with its appraisal and why it was or was not selected."""
    picked = select(store, run_id, protocol)
    cands = candidates(store, run_id, protocol)
    apps = latest_appraisals(store, run_id)
    meta = tables.works(store, cands, run_id)
    lines = [f"**{label}.** Every candidate study (a focused study with a finding from an experiment or natural "
             "experiment), its appraised design and the selection: selected for a rule, not causal, the same natural "
             "experiment as a stronger study, or below its rule's quota or country cap.", "",
             "| Study | Rules | Design | Natural experiment | Country | Outcome | Publication | Read from | Selection |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for w in sorted(cands, key=lambda w: meta[w]["cite"]):
        a = apps.get(w, {})
        if w in picked["selected"]:
            why = f"selected ({_label(picked['selected'][w])})"
        elif w in picked["missing"]:
            why = "not appraised"
        elif w in picked["not_causal"]:
            why = "not causal"
        elif w in picked["same_experiment"]:
            why = f"same experiment as {meta[picked['same_experiment'][w]]['cite']}"
        else:
            why = "below quota or country cap"
        lines.append(f"| {meta[w]['cite']} | {', '.join(_label(r) for r in cands[w])} | {_label(a.get('identification'))}"
                     f" | {_cell(a.get('experiment'), 80)} | {_cell(a.get('country'), 30)} | {_label(a.get('outcome'))} | "
                     f"{_label(a.get('publication'))} | {a.get('read_from', '')} | {why} |")
    return "\n".join(lines)


def facts(store: Store, run_id: int, protocol: dict) -> list[dict]:
    """Method facts A0-A5 of a strong-evidence paper: the aim, the candidates, the selection rule and its result, the
    reading of effects, and what no person checked."""
    a, rule = protocol["aim"], (protocol["aim"].get("select") or {})
    picked = select(store, run_id, protocol)
    sel = picked["selected"]
    full = sum(1 for w in sel if picked["fulltext"][w])
    ests = estimates(store, sel)
    apps = latest_appraisals(store, run_id)
    design = Counter(apps[w]["identification"] for w in sel if w in apps)
    read = Counter(r["read_from"] for r in apps.values())
    quota = rule.get("quota") or {}
    return [
        {"id": "A0", "group": "Aim", "text": (
            f"This paper asks: {' '.join(str(a.get('question', '')).split())} It rests on {len(sel)} studies selected "
            f"by the strength of their design from {picked['candidates']} candidates ("
            + ", ".join(f"{_label(r)} {len(ids)}" for r, ids in picked["by_rule"].items())
            + f"), {full} of them read in full for their effects.")},
        {"id": "A1", "group": "Aim", "text": (
            f"Candidates were the {picked['candidates']} studies of the review's focused view on the three rules with at "
            "least one finding from an experiment or a natural experiment. A language-model agent appraised each on a "
            "fixed rubric (how the effect is identified, the source of variation, the natural experiment used, country, "
            "publication, outcome, follow-up and the main threat), giving the passage that shows the design; "
            f"{read.get('full text', 0)} were appraised from full texts and {read.get('abstract', 0)} from abstracts.")},
        {"id": "A2", "group": "Aim", "text": (
            "The selection rule was written into the protocol before any effect was extracted: studies appraised as not "
            "causal drop out; of studies using the same natural experiment the strongest stays; studies are ranked by "
            "design (randomised, regression discontinuity, difference-in-differences, instrumental variable, other "
            "natural experiment), then by a measure of the number of births over timing alone, then by fertility "
            "observed to age 40 or completed, then by publication (peer-reviewed, working paper, thesis); each rule takes "
            "its quota in rank order ("
            + ", ".join(f"{_label(r)} {q}" for r, q in quota.items())
            + f"), the scarcest rule first, at most {rule.get('per_country', 'any number of')} studies from one country. "
            f"{len(picked['not_causal'])} candidates were appraised as not causal and {len(picked['same_experiment'])} "
            "shared a natural experiment with a stronger study.")},
        {"id": "A3", "group": "Aim", "text": (
            "Designs of the selected studies: " + ", ".join(f"{_label(k)} {v}" for k, v in design.most_common()) + ".")},
        {"id": "A4", "group": "Aim", "text": (
            "A language-model agent read each selected study with a full text and listed its estimates of the effect on "
            "births (at most six per study: the exposure and its unit, the outcome, the estimate, its uncertainty, the "
            "population and the horizon as reported, with the passage that states it): "
            f"{len(ests)} estimates from {len({e['work_id'] for e in ests})} studies, "
            f"{sum(1 for e in ests if e['grounded'])} of whose passages were found verbatim in the text read. "
            "Estimates are reported as the studies report them, not converted to a common scale or pooled.")},
        {"id": "A5", "group": "Aim", "text": (
            "What no person checked: the search, screening, eligibility, charting, the appraisal of designs and the "
            "reading of effects were done by language-model agents; every quoted passage was checked in code against "
            "the text read, but no person has checked the appraisals or the estimates against the studies.")},
    ]
