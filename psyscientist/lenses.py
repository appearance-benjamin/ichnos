"""The lens layer: rival interpretations of the same evidence, scored for fruitfulness.

Psychology's value lies in interpretation, so interpretation is not audited for faithfulness here. It is
attributed and scored. Each lens is a research programme in Lakatos's sense, declared in
protocol/lenses_corpus_X.toml: a hard core (what it holds fixed), a heuristic, and during a run a counted
belt of auxiliary assumptions. Numbers stay verified (numgate); everything else is judged by what it
yields.

Task kinds (task folders like every other agent step):
- interpret:    one lens reads all findings blind (it sees no rival). It labels how every finding fits
                (predicts, accommodates with a named auxiliary, silent, strains), signs a probe grid, takes
                a stance on every seeded contradiction and proposes testable hypotheses, each with what it
                forbids. Each lens runs twice (replicates) with the findings in a different order.
- lens_control: the single-agent controls. `all_cards`: one agent plays every lens at once, so the
                tournament can show whether separate lenses find more than one agent does. `no_card`: one
                agent with no lens proposes hypotheses: what the model says anyway.
- lens_judge:   a judge of the other model tier labels every hypothesis blind (lens names masked):
                testable or not, a restatement of a finding or not, already among the no-card baseline or not.

Code, not agents, counts everything (evaluate): replicate agreement, between- versus within-lens
disagreement with an exact permutation test, divergences on the grid (with the positive control),
hypotheses credited after the judge's gates, the controls side by side, and copying between lenses.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import tomllib
from collections import Counter
from pathlib import Path

from . import agreement, numgate, tables
from .config import ROOT
from .store import Store, now

KINDS = ("interpret", "lens_control", "lens_judge", "lens_derive", "cartograph")
DERIVATION = ("follows_from_core", "needs_auxiliary", "common_sense", "contradicts_core")
CREDIT_DERIVATION = ("follows_from_core", "needs_auxiliary")
NO_VERDICT = {"winner", "verdict", "best", "resolved", "conclusion", "which_is_right"}
FIT = ("predicts", "accommodates", "silent", "strains")
SIGNS = ("+", "-", "0", "?")
STANCES = ("explains", "explains_with_aux", "silent", "conflicts")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lens_outputs (
    output_id   INTEGER PRIMARY KEY,
    corpus      TEXT NOT NULL,
    run_id      INTEGER REFERENCES runs(run_id),
    tournament  TEXT NOT NULL,
    kind        TEXT NOT NULL,                   -- interpret, all_cards, no_card
    lens        TEXT,
    replicate   INTEGER,
    agent       TEXT NOT NULL,
    folder      TEXT,
    content     TEXT NOT NULL,                   -- JSON answer
    sha256      TEXT NOT NULL,
    numbers     TEXT,                            -- JSON number-gate flags
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lens_derivations (
    derivation_id INTEGER PRIMARY KEY,
    tournament  TEXT NOT NULL,
    output_id   INTEGER NOT NULL REFERENCES lens_outputs(output_id),
    lens        TEXT,
    hypothesis  TEXT NOT NULL,
    label       TEXT NOT NULL,                   -- follows_from_core, needs_auxiliary, common_sense, contradicts_core
    auxiliary   TEXT,                            -- the assumption it needs, for needs_auxiliary
    reason      TEXT,
    judge       TEXT NOT NULL,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lens_labels (
    label_id    INTEGER PRIMARY KEY,
    tournament  TEXT NOT NULL,
    output_id   INTEGER NOT NULL REFERENCES lens_outputs(output_id),
    lens        TEXT,                            -- the programme as stored (judges saw it masked)
    hypothesis  TEXT NOT NULL,                   -- id within the output, e.g. H3
    testable    INTEGER,
    restates    INTEGER,                         -- finding id it merely restates, or NULL
    baseline    TEXT,                            -- matching no-card hypothesis id, or NULL
    reason      TEXT,
    judge       TEXT NOT NULL,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


# The lens file

def load(corpus: str, root: Path = ROOT) -> tuple[Path, str, dict]:
    path = root / "protocol" / f"lenses_corpus_{corpus.lower()}.toml"
    if not path.exists():
        raise ValueError(f"no lens file: {path.relative_to(root)}")
    text = path.read_text(encoding="utf-8")
    return path, text, tomllib.loads(text)


def check(cfg: dict) -> None:
    ids = [lens.get("id") for lens in cfg.get("lenses") or []]
    if len(ids) < 2 or len(set(ids)) != len(ids):
        raise ValueError("the lens file needs at least two [[lenses]] with distinct ids")
    for lens in cfg["lenses"]:
        for key in ("id", "name", "role", "core", "heuristic"):
            if not lens.get(key):
                raise ValueError(f"lens {lens.get('id', '?')} needs `{key}`")
        if not isinstance(lens["core"], list):
            raise ValueError(f"lens {lens['id']}: `core` is a list of propositions, e.g. core = [\"J1: ...\"]")
    i = cfg.get("interpret") or {}
    if not i.get("settings") or not i.get("variables"):
        raise ValueError("[interpret] needs settings and [[interpret.variables]]")


def card(lens: dict) -> dict:
    """What an agent sees of a lens; the placebo only under its masked name."""
    return {"name": lens["name"], "role": "a perspective" if lens["role"] == "placebo" else lens["role"],
            "core": lens["core"], "heuristic": lens["heuristic"]}


def grid(cfg: dict) -> list[dict]:
    i = cfg["interpret"]
    cells, n = [], 0
    for setting in i["settings"]:
        for v in i["variables"]:
            n += 1
            cells.append({"cell": f"G{n}", "setting": setting, "variable": v["id"], "contrast": v["contrast"]})
    return cells


def tensions(store: Store, corpus: str) -> list[dict]:
    return [{"insight": r["insight_id"], "statement": r["statement"]} for r in store.db.execute(
        "SELECT insight_id, statement FROM insights WHERE corpus = ? AND kind = 'contradiction' AND status != 'dropped' "
        "ORDER BY insight_id", (corpus,))]


def evidence(store: Store, run_id: int, seed: str) -> tuple[list[dict], dict]:
    """The findings in a seeded order (each replicate reads them in its own order), and the studies."""
    rows = tables.finding_rows(store, run_id)
    meta = tables.works(store, {r["work_id"] for r in rows})
    chart = tables.charting(store, run_id)
    order = sorted(rows, key=lambda r: hashlib.sha256(f"{seed}-{r['finding_id']}".encode()).hexdigest())
    findings = [{"id": r["finding_id"], "study": meta[r["work_id"]]["cite"], "finding": r["finding"],
                 "effect": r["effect"], "for_whom": r["for_whom"], "fields": json.loads(r["fields"] or "{}"),
                 "passage": r["evidence"], "passage_from": r["source"] or "unconfirmed"} for r in order]
    studies = {}
    for w, m in meta.items():
        c = chart.get(w, {})
        studies[m["cite"]] = {"title": m["title"], "type": tables.kind_of(m),
                              "design": ", ".join(c.get("design", {}).get("values", [])) or "not charted",
                              "population": (c.get("population") or {}).get("text") or "not charted"}
    return findings, studies


# Instructions

OUTPUT_INTERPRET = {
    "reading": "your free reading of the evidence through this lens, at most 300 words",
    "fit": [{"finding": 1, "label": "predicts | accommodates | silent | strains", "aux": "A1 (only for accommodates)"}],
    "auxiliaries": [{"id": "A1", "statement": "an extra assumption the core does not contain",
                     "consequence": "an observable result, beyond the case it rescues, that would follow from it"}],
    "hypotheses": [{"id": "H1", "statement": "a new, testable prediction", "setting": "a protocol setting",
                    "outcome": "the outcome", "condition": "for whom or under which condition",
                    "sign": "+ | - | 0", "forbids": "the observation that would refute it",
                    "derived_from": [1], "premises": ["core:J1", "aux:A1", "finding:12"]}],
    "grid": [{"cell": "G1", "sign": "+ | - | 0 | ?", "why": "at most 20 words"}],
    "tensions": [{"insight": 9, "stance": "explains | explains_with_aux | silent | conflicts", "note": "at most 30 words"}],
}


GRID_QUESTION = ("compared with the second group of the contrast, is the change in the outcome after the exit more "
                 "positive (+), more negative (-) or the same (0) in the first group, or can this lens not say (?)")


def _grid_question(i: dict) -> str:
    """The question every grid cell answers; a lens file sets its own under [interpret] grid_question."""
    return " ".join(str(i.get("grid_question") or GRID_QUESTION).split())


def _review(i: dict) -> str:
    """What the review is about, as the agents are told; a lens file sets it under [interpret] review."""
    return " ".join(str(i.get("review") or f"{i.get('outcome', 'the outcome')} when work ends or never begins").split())


def _rules(budget: int, i: dict | None = None) -> list[str]:
    return [
        "Rules:",
        "- Use only batch_01.json. Do not open any other file of the project (no other task folders, no other "
        "lenses' answers, no manuscripts, no database), do not search the web.",
        "- Interpret freely: this is interpretation, not a summary. But every number you write must come from the "
        "findings you cite, and every finding you lean on must be cited by id.",
        "- fit: label every finding exactly once. predicts = the core leads you to expect it; accommodates = only "
        "with an auxiliary assumption, which you must name (aux) and list; silent = the lens says nothing about it; "
        "strains = it sits badly with the core. An admitted anomaly costs nothing; a hidden one does.",
        "- auxiliaries: every extra assumption beyond the core, each with a consequence: an observable result "
        "beyond the case it rescues. An assumption without such a consequence is ad hoc and will be counted as such.",
        f"- hypotheses: at most {budget}. Each must be new (not a restatement of a finding), testable, and say what "
        "it forbids. Prefer predictions where your lens would differ from other perspectives.",
        f"- grid: sign every cell exactly once: {_grid_question(i or {})}. Say ? rather than guess.",
        "- tensions: every listed contradiction exactly once.",
    ]


def interpret_instructions(lens: dict, cfg: dict) -> str:
    c = card(lens)
    i = cfg["interpret"]
    return "\n".join([
        f"# Interpretation through one lens: {c['name']}", "",
        f"You interpret the evidence of a review ({_review(i)}, "
        "or as the batch describes it) through exactly one perspective, as a committed proponent of it would. Other "
        "agents interpret the same evidence through rival perspectives; you do not see them.", "",
        f"## The lens: {c['name']}", "", "Hard core (fixed; you may not change it):",
        *[f"- {p}" for p in c["core"]], "", f"Heuristic: {c['heuristic']}", "",
        "batch_01.json holds `findings` (each with its verbatim passage and the protocol's coded fields), "
        "`studies` (design, population), `grid` (the probe cells) and `tensions` "
        "(contradictions noticed in the evidence).", "",
        *_rules(int(i.get("budget", 8)), i), "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps(OUTPUT_INTERPRET, ensure_ascii=False, indent=1), "```"]) + "\n"


def control_instructions(mode: str, lenses: list[dict], cfg: dict) -> str:
    i = cfg["interpret"]
    if mode == "no_card":
        return "\n".join([
            "# Hypotheses from the evidence", "",
            "batch_01.json holds the `findings` of a review (each with its verbatim passage and coded fields) and "
            "`studies`. Propose new, testable hypotheses that the evidence suggests.", "",
            "- Use only batch_01.json; open no other file, do not search the web.",
            f"- At most {int(i.get('baseline_budget', 16))} hypotheses. Each new (not a restatement of a finding), "
            "testable, and with what it forbids. Numbers only from the findings you cite.", "",
            "Output: one JSON object in batch_01_result.json:", "", "```json",
            json.dumps({"hypotheses": OUTPUT_INTERPRET["hypotheses"]}, ensure_ascii=False, indent=1), "```"]) + "\n"
    cards = [card(lens) for lens in lenses]
    return "\n".join([
        "# Several lenses, one agent", "",
        "You interpret the evidence of a review through each of the perspectives below in turn, as a committed "
        "proponent of each would, and then say where they predict different results.", "",
        *[line for c in cards for line in (f"## {c['name']}", *[f"- {p}" for p in c["core"]],
                                           f"Heuristic: {c['heuristic']}", "")],
        "batch_01.json holds `findings`, `studies` and `grid` (the probe cells).", "",
        "- Use only batch_01.json; open no other file, do not search the web.",
        f"- For each lens, sign every grid cell exactly once: {_grid_question(i)}.",
        f"- For each lens, at most {int(i.get('budget', 8))} new, testable hypotheses, each with what it forbids.",
        "- divergences: the cells or questions on which the lenses predict different results, and which lenses.", "",
        "Output: one JSON object in batch_01_result.json, `lenses` keyed by the lens names exactly as above:", "",
        "```json",
        json.dumps({"lenses": {cards[0]["name"]: {"grid": OUTPUT_INTERPRET["grid"],
                                                  "hypotheses": OUTPUT_INTERPRET["hypotheses"]}},
                    "divergences": [{"cell": "G1", "lenses": ["name", "name"], "note": "..."}]},
                   ensure_ascii=False, indent=1), "```"]) + "\n"


def judge_instructions() -> str:
    return "\n".join([
        "# Judging hypotheses, blind", "",
        "batch_01.json holds `hypotheses` proposed by several research programmes, with their names masked and in "
        "shuffled order, the `baseline` hypotheses one agent proposed with no programme, and the `findings` of the "
        "review. Label every hypothesis in `hypotheses`:", "",
        "- testable: true if it names an observable result and what would refute it (`forbids`), so that a study "
        "with a realistic design could test it;",
        "- restates: the id of the finding it merely restates (the evidence already shows exactly this), or null;",
        "- baseline: the key of a baseline hypothesis that predicts essentially the same thing (same outcome, "
        "condition and direction), or null;",
        "- reason: at most 20 words.", "",
        "Use only batch_01.json and open no other file (not task.json). Judge the content, not the wording.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"labels": [{"key": "h1", "testable": True, "restates": None, "baseline": None,
                                "reason": "..."}]}, indent=1), "```"]) + "\n"


# Writing tasks

def write_task(store: Store, run_id: int, kind: str, agent: str, protocol: dict, sha: str, root: Path,
               options: dict) -> Path:
    from .agents import _new_folder
    ensure(store)
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    path, text, cfg = load(corpus, root)
    check(cfg)
    lens_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    tour = options.get("tournament") or cfg["interpret"].get("tournament", "pilot")
    meta = {"kind": kind, "agent": agent, "run_id": run_id, "corpus": corpus, "protocol_sha256": sha,
            "lens_file": path.relative_to(root).as_posix(), "lens_sha256": lens_sha, "tournament": tour,
            "batches": 1}
    by_id = {lens["id"]: lens for lens in cfg["lenses"]}
    if kind == "interpret":
        lens = by_id.get(options.get("lens"))
        if lens is None:
            raise ValueError(f"--lens must be one of {sorted(by_id)}")
        rep = int(options.get("replicate") or 1)
        findings, studies = evidence(store, run_id, f"{tour}-{lens['id']}-{rep}")
        data = {"lens": card(lens), "findings": findings, "studies": studies, "grid": grid(cfg),
                "tensions": tensions(store, corpus)}
        instructions = interpret_instructions(lens, cfg)
        meta.update(lens=lens["id"], replicate=rep, records=len(findings))
        label = f"{agent}-{lens['id']}-r{rep}"
    elif kind == "lens_control":
        mode = options.get("mode") or "all_cards"
        if mode not in ("all_cards", "no_card"):
            raise ValueError("--mode must be all_cards or no_card")
        findings, studies = evidence(store, run_id, f"{tour}-{mode}")
        data = {"findings": findings, "studies": studies}
        lenses = [by_id[i] for i in (options.get("lenses") or by_id)]
        if mode == "all_cards":
            data["grid"] = grid(cfg)
            meta["names"] = {card(lens)["name"]: lens["id"] for lens in lenses}
        instructions = control_instructions(mode, lenses, cfg)
        meta.update(mode=mode, records=len(findings))
        label = f"{agent}-{mode}"
    elif kind == "lens_derive":
        data, keys, programmes = derive_batch(store, run_id, tour, cfg)
        instructions = derive_instructions()
        meta.update(keys=keys, programmes=programmes, records=len(data["hypotheses"]))
        label = agent
    elif kind == "cartograph":
        data = cartograph_batch(store, run_id, root, tour, cfg, options.get("lenses"))
        instructions = cartograph_instructions(data)
        meta.update(lenses=[lens["id"] for lens in data["lenses"]], records=len(data["divergences"]))
        label = agent
    else:   # lens_judge
        data, keys = judge_batch(store, run_id, tour)
        instructions = judge_instructions()
        meta.update(keys=keys, records=len(data["hypotheses"]))
        label = agent
    out = _new_folder(root, corpus, run_id, kind, label)
    (out / "instructions.md").write_text(instructions, encoding="utf-8")
    (out / "batch_01.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "task.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def outputs(store: Store, run_id: int, tournament: str, kind: str | None = None) -> list:
    ensure(store)
    sql = "SELECT * FROM lens_outputs WHERE run_id = ? AND tournament = ?" + (" AND kind = ?" if kind else "")
    rows = store.db.execute(sql + " ORDER BY output_id", (run_id, tournament) + ((kind,) if kind else ())).fetchall()
    latest = {}
    for r in rows:                       # a re-run of the same lens replicate replaces the earlier one
        latest[(r["kind"], r["lens"], r["replicate"])] = r
    return list(latest.values())


def _hypothesis_items(store: Store, run_id: int, tournament: str) -> list[tuple]:
    """(output_id, lens, hypothesis) for the tournament and the all-cards control, in a seeded order."""
    items = []
    for o in outputs(store, run_id, tournament):
        content = json.loads(o["content"])
        if o["kind"] == "interpret":
            hyps = [(o["lens"], h) for h in content.get("hypotheses") or []]
        elif o["kind"] == "all_cards":
            hyps = [(lens, h) for lens, part in content.get("lenses", {}).items() for h in part.get("hypotheses") or []]
        else:
            continue
        items += [(o["output_id"], lens, h) for lens, h in hyps]
    if not items:
        raise ValueError("no hypotheses yet: import the interpret and control tasks first")
    return sorted(items, key=lambda x: hashlib.sha256(f"{tournament}-{x[0]}-{x[1]}-{x[2].get('id')}".encode())
                  .hexdigest())


def derive_batch(store: Store, run_id: int, tournament: str, cfg: dict) -> tuple[dict, dict, dict]:
    """Every hypothesis with only its own programme's core, programme names masked as letters."""
    ids = sorted((lens["id"] for lens in cfg["lenses"]),
                 key=lambda i: hashlib.sha256(f"{tournament}-programme-{i}".encode()).hexdigest())
    letters = {lens_id: chr(65 + n) for n, lens_id in enumerate(ids)}
    by_id = {lens["id"]: lens for lens in cfg["lenses"]}
    programmes = [{"programme": letters[i], "core": by_id[i]["core"], "heuristic": by_id[i]["heuristic"]}
                  for i in sorted(ids, key=lambda i: letters[i])]
    hyps, keys = [], {}
    for n, (oid, lens, h) in enumerate(_hypothesis_items(store, run_id, tournament), 1):
        key = f"d{n}"
        keys[key] = {"output_id": oid, "lens": lens, "hypothesis": h.get("id")}
        hyps.append({"key": key, "programme": letters.get(lens, "?"),
                     **{k: h.get(k) for k in ("statement", "condition", "sign", "forbids", "premises")}})
    return ({"programmes": programmes, "hypotheses": hyps}, keys,
            {letter: lens_id for lens_id, letter in letters.items()})


def derive_instructions() -> str:
    return "\n".join([
        "# Does the hypothesis follow from its programme, or from common sense?", "",
        "batch_01.json holds research `programmes` (each a hard core and a heuristic, names masked as letters) and "
        "`hypotheses`, each proposed by one programme (its letter). For every hypothesis decide, using only its own "
        "programme's core and heuristic and your general knowledge of psychology:", "",
        "- follows_from_core: the core leads to this prediction, and a knowledgeable psychologist without this core "
        "would not specifically predict it (the core does the work);",
        "- needs_auxiliary: it follows only with an added assumption that the core does not contain; name that "
        "assumption in `auxiliary`;",
        "- common_sense: a knowledgeable psychologist would predict this anyway; the core adds nothing specific "
        "(for example 'people differ', 'context matters', 'more resources help', 'heterogeneity hides effects');",
        "- contradicts_core: it goes against the core.", "",
        "Judge the content, not the confidence of the wording. Be strict: a prediction that merely mentions the "
        "core's vocabulary but would be made by anyone is common_sense.", "",
        "Use only batch_01.json; do not open task.json or any other file.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"labels": [{"key": "d1", "label": "follows_from_core", "auxiliary": None,
                                "reason": "at most 25 words"}]}, indent=1), "```"]) + "\n"


def cartograph_batch(store: Store, run_id: int, root: Path, tournament: str, cfg: dict, pair=None) -> dict:
    """What the cartographer needs: the theory divergences, the contested findings with their passages, the
    lenses' credited hypotheses and the studies' designs. The two lenses default to the positive control."""
    result = evaluate(store, run_id, root, tournament)
    pc = (cfg["interpret"].get("positive_control") or {}).get("lenses") or []
    chosen = list(pair or pc)
    adversary = [lens["id"] for lens in cfg["lenses"] if lens["role"] == "adversary"]
    by_id = {lens["id"]: lens for lens in cfg["lenses"]}
    lenses_ = [{"id": i, "name": by_id[i]["name"], "role": by_id[i]["role"], "core": by_id[i]["core"],
                "heuristic": by_id[i]["heuristic"]} for i in chosen + [a for a in adversary if a not in chosen]]
    wanted = {lens["id"] for lens in lenses_}
    divergences = [d for d in result["divergences"] if set(d["lenses"]) <= wanted]
    contested = [c for c in result["contested_findings"] if any(c["labels"].get(i) for i in chosen)]
    rows = {r["finding_id"]: r for r in tables.finding_rows(store, run_id)}
    meta = tables.works(store, {r["work_id"] for r in rows.values()})
    used = {c["finding"] for c in contested}
    hypotheses = []
    for k, v in result.get("hypothesis_items", {}).items():
        lens = k.split(":", 1)[1]
        if k.startswith("tournament:") and lens in wanted:
            hypotheses += [{"lens": lens, **h} for h in v]
            for h in v:
                used |= set(h.get("derived_from") or [])
    findings = [{"id": f, "study": meta[rows[f]["work_id"]]["cite"], "finding": rows[f]["finding"],
                 "effect": rows[f]["effect"], "passage": rows[f]["evidence"], "fields": json.loads(rows[f]["fields"] or "{}")}
                for f in sorted(used) if f in rows]
    studies = {}
    chart = tables.charting(store, run_id)
    for w in {rows[f["id"]]["work_id"] for f in findings}:
        c = chart.get(w, {})
        studies[meta[w]["cite"]] = {"design": ", ".join(c.get("design", {}).get("values", [])) or "not charted",
                                    "population": (c.get("population") or {}).get("text") or "not charted"}
    return {"lenses": lenses_, "divergences": divergences,
            "contested_findings": [{"finding": c["finding"], "labels": {k: v for k, v in c["labels"].items() if k in wanted}}
                                   for c in contested],
            "hypotheses": hypotheses, "findings": findings, "studies": studies,
            "tensions": tensions(store, store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,))
                                 .fetchone()["corpus"])}


def cartograph_instructions(data: dict) -> str:
    names = ", ".join(f"{lens['id']} ({lens['name']}, {lens['role']})" for lens in data["lenses"])
    return "\n".join([
        "# Tension cartographer: from disagreement to a decisive test", "",
        f"Rival lenses ({names}) read the same evidence of a review and predict different things. batch_01.json holds "
        "their cores, the grid cells where they diverge, the findings one lens predicts and another strains at, "
        "their credited hypotheses, the findings involved (with verbatim passages) and the studies' designs.", "",
        "Turn the sharpest disagreements into at most five tension cards. Each card is a test that would tell the "
        "lenses apart:", "",
        "- question: the disagreement in one sentence;",
        "- positions: for each lens involved, what it predicts and from which core proposition or hypothesis;",
        "- test: design (e.g. panel with a matched control group, event study, randomised trial), data (a data set "
        "named in a cited study, or 'new data'), population, measures (predictor, moderator, outcome), contrast, "
        "timing and analysis;",
        "- patterns: the possible results, each with the lenses it favours. The patterns must split the lenses: at "
        "least two different lenses must be favoured by different patterns;",
        "- existing_evidence: findings (ids) that bear on it and why they are not decisive;",
        "- artifact_threats: how selection, anticipation, measurement or design could fake each pattern, and how the "
        "test guards against it;",
        "- facts_to_verify: every claim about a data set (its waves, its variables, its access) that no cited finding "
        "states. Do not present such claims as facts.", "",
        "Do not decide which lens is right and do not merge positions: there is no winner field. A contradiction "
        "a feasible study could settle is the most valuable thing you can return.", "",
        "Use only batch_01.json; open no other file, do not search the web.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"cards": [{"id": "T1", "question": "...", "cells": ["G1"],
                               "positions": [{"lens": "jahoda", "prediction": "...", "from": "core J2 / hypothesis H3"}],
                               "test": {"design": "...", "data": "...", "population": "...",
                                        "measures": {"predictor": "...", "moderator": "...", "outcome": "..."},
                                        "contrast": "...", "timing": "...", "analysis": "..."},
                               "patterns": [{"result": "...", "favours": ["jahoda"]}],
                               "existing_evidence": [{"finding": 32, "why_not_decisive": "..."}],
                               "artifact_threats": ["..."], "facts_to_verify": ["..."]}]},
                   ensure_ascii=False, indent=1), "```"]) + "\n"


def judge_batch(store: Store, run_id: int, tournament: str) -> tuple[dict, dict]:
    """All hypotheses of the tournament and of the all-cards control, masked and shuffled."""
    items, keys = [], {}
    for o in outputs(store, run_id, tournament):
        content = json.loads(o["content"])
        if o["kind"] == "interpret":
            hyps = [(o["lens"], h) for h in content.get("hypotheses") or []]
        elif o["kind"] == "all_cards":
            hyps = [(lens, h) for lens, part in content.get("lenses", {}).items() for h in part.get("hypotheses") or []]
        else:
            continue
        for lens, h in hyps:
            items.append((o["output_id"], lens, h))
    if not items:
        raise ValueError("no hypotheses yet: import the interpret and control tasks first")
    items.sort(key=lambda x: hashlib.sha256(f"{tournament}-{x[0]}-{x[2].get('id')}".encode()).hexdigest())
    hyps = []
    for n, (oid, lens, h) in enumerate(items, 1):
        key = f"h{n}"
        keys[key] = {"output_id": oid, "lens": lens, "hypothesis": h.get("id")}
        hyps.append({"key": key, **{k: h.get(k) for k in ("statement", "setting", "outcome", "condition", "sign",
                                                          "forbids", "derived_from")}})
    base = next((json.loads(o["content"]) for o in outputs(store, run_id, tournament, "no_card")), {})
    baseline = [{"key": f"b{n}", "statement": h.get("statement"), "condition": h.get("condition"),
                 "sign": h.get("sign")} for n, h in enumerate(base.get("hypotheses") or [], 1)]
    findings = [{"id": r["finding_id"], "finding": r["finding"]} for r in tables.finding_rows(store, run_id)]
    return {"hypotheses": hyps, "baseline": baseline, "findings": findings}, keys


# Checking and importing answers

def _check_hypotheses(hyps, budget: int, finding_ids: set[int], where: str) -> list[str]:
    errs = []
    if not isinstance(hyps, list):
        return [f"{where}: hypotheses must be a list"]
    if len(hyps) > budget:
        errs.append(f"{where}: {len(hyps)} hypotheses, at most {budget}")
    for h in hyps:
        if not (h.get("statement") or "").strip() or not (h.get("forbids") or "").strip():
            errs.append(f"{where} {h.get('id')}: needs a statement and what it forbids")
        bad = [f for f in h.get("derived_from") or [] if f not in finding_ids]
        if bad:
            errs.append(f"{where} {h.get('id')}: derived_from cites unknown findings {bad}")
    return errs


def _check_grid(entries, cells: list[str], where: str) -> list[str]:
    got = [g.get("cell") for g in entries or []]
    errs = [] if sorted(got) == sorted(cells) else [f"{where}: sign every grid cell exactly once"]
    errs += [f"{where} {g.get('cell')}: sign {g.get('sign')!r}" for g in entries or [] if g.get("sign") not in SIGNS]
    return errs


def check_answer(kind: str, meta: dict, data: dict, answer, cfg: dict) -> list[str]:
    if not isinstance(answer, dict):
        return ["the answer must be a JSON object"]
    fids = {f["id"] for f in data.get("findings") or []}
    budget = int(cfg["interpret"].get("budget", 8))
    if kind == "interpret":
        errs = []
        labels = answer.get("fit") or []
        if sorted(x.get("finding") for x in labels) != sorted(fids):
            errs.append("fit: label every finding exactly once")
        aux = {a.get("id") for a in answer.get("auxiliaries") or []}
        errs += [f"fit {x.get('finding')}: label {x.get('label')!r}" for x in labels if x.get("label") not in FIT]
        named = lambda x: [a for a in re.split(r"[,;\s]+", str(x.get("aux") or "")) if a]  # noqa: E731
        errs += [f"fit {x.get('finding')}: accommodates needs listed auxiliaries" for x in labels
                 if x.get("label") == "accommodates" and (not named(x) or any(a not in aux for a in named(x)))]
        errs += [f"auxiliary {a.get('id')}: needs a statement and a consequence" for a in answer.get("auxiliaries") or []
                 if not (a.get("statement") and a.get("consequence"))]
        errs += _check_hypotheses(answer.get("hypotheses"), budget, fids, "hypothesis")
        errs += _check_grid(answer.get("grid"), [c["cell"] for c in data["grid"]], "grid")
        want = sorted(t["insight"] for t in data.get("tensions") or [])
        if sorted(t.get("insight") for t in answer.get("tensions") or []) != want:
            errs.append("tensions: take a stance on every listed contradiction exactly once")
        errs += [f"tension {t.get('insight')}: stance {t.get('stance')!r}" for t in answer.get("tensions") or []
                 if t.get("stance") not in STANCES]
        if len((answer.get("reading") or "").split()) > 360:
            errs.append("reading: at most 300 words")
        return errs
    if kind == "lens_control":
        if meta.get("mode") == "no_card":
            return _check_hypotheses(answer.get("hypotheses"), int(cfg["interpret"].get("baseline_budget", 16)),
                                     fids, "hypothesis")
        names = meta["names"]
        parts = answer.get("lenses") or {}
        errs = [] if set(parts) == set(names) else [f"lenses: give exactly {sorted(names)}"]
        for name, part in parts.items():
            errs += _check_grid(part.get("grid"), [c["cell"] for c in data["grid"]], f"{name} grid")
            errs += _check_hypotheses(part.get("hypotheses"), budget, fids, f"{name} hypothesis")
        return errs
    if kind == "lens_derive":
        keys = {h["key"] for h in data["hypotheses"]}
        labels = answer.get("labels") or []
        errs = [] if sorted(x.get("key") for x in labels) == sorted(keys) else ["labels: every hypothesis key exactly once"]
        errs += [f"{x.get('key')}: label {x.get('label')!r}" for x in labels if x.get("label") not in DERIVATION]
        errs += [f"{x.get('key')}: needs_auxiliary must name the auxiliary" for x in labels
                 if x.get("label") == "needs_auxiliary" and not (x.get("auxiliary") or "").strip()]
        return errs
    if kind == "cartograph":
        cards = answer.get("cards")
        if not isinstance(cards, list) or not cards:
            return ["cards: at least one tension card"]
        lens_ids = {lens["id"] for lens in data["lenses"]}
        known = {f["id"] for f in data["findings"]}
        errs = [] if len(cards) <= 5 else ["cards: at most five"]
        for c in cards:
            where = f"card {c.get('id')}"
            if NO_VERDICT & set(c):
                errs.append(f"{where}: no verdict fields ({sorted(NO_VERDICT & set(c))}); the cartographer does not decide")
            for key in ("question", "positions", "test", "patterns"):
                if not c.get(key):
                    errs.append(f"{where}: needs {key}")
            for key in ("positions", "patterns", "cells", "existing_evidence", "artifact_threats", "facts_to_verify"):
                if c.get(key) is not None and not isinstance(c[key], list):
                    errs.append(f"{where}: {key} must be a list")
            if any(not isinstance(x, dict) for key in ("positions", "patterns", "existing_evidence")
                   for x in (c.get(key) if isinstance(c.get(key), list) else [])):
                errs.append(f"{where}: positions, patterns and existing_evidence hold objects")
                continue
            if any(not isinstance(pt.get("favours") or [], list) for pt in c.get("patterns") or []):
                errs.append(f"{where}: favours must be a list of lens ids")
                continue
            favoured = [set(pt.get("favours") or []) for pt in c.get("patterns") or []]
            if len(set().union(*favoured) if favoured else set()) < 2 or len({frozenset(f) for f in favoured}) < 2:
                errs.append(f"{where}: the patterns must favour at least two different lenses")
            bad = {x for f in favoured for x in f} | {p.get("lens") for p in c.get("positions") or []}
            if bad - lens_ids:
                errs.append(f"{where}: unknown lenses {sorted(str(b) for b in bad - lens_ids)}")
            ev = [e.get("finding") for e in c.get("existing_evidence") or []]
            if [e for e in ev if e not in known]:
                errs.append(f"{where}: existing_evidence cites findings not in the batch")
        return errs
    keys = {h["key"] for h in data["hypotheses"]}
    base = {b["key"] for b in data["baseline"]}
    labels = answer.get("labels") or []
    for x in labels:                      # "94" and 94 are the same finding
        if isinstance(x.get("restates"), str) and x["restates"].strip().isdigit():
            x["restates"] = int(x["restates"])
    errs = [] if sorted(x.get("key") for x in labels) == sorted(keys) else ["labels: every hypothesis key exactly once"]
    for x in labels:
        if not isinstance(x.get("testable"), bool):
            errs.append(f"{x.get('key')}: testable must be true or false")
        if x.get("restates") is not None and x.get("restates") not in fids:
            errs.append(f"{x.get('key')}: restates an unknown finding")
        if x.get("baseline") is not None and x.get("baseline") not in base:
            errs.append(f"{x.get('key')}: unknown baseline key")
    return errs


def _number_flags(answer: dict, findings: dict[int, str]) -> list[str]:
    """Numbers in readings and hypotheses must stand in the findings they cite (or, for the reading, any)."""
    out = []
    every = list(findings.values())
    if answer.get("reading"):
        out += [f"reading: {p}" for p in numgate.describe(numgate.gate(answer["reading"], every))]
    parts = [answer] if "hypotheses" in answer else list((answer.get("lenses") or {}).values())
    for part in parts:
        for h in part.get("hypotheses") or []:
            cited = [findings[f] for f in h.get("derived_from") or [] if f in findings]
            text = f"{h.get('statement', '')} {h.get('condition', '')}"
            out += [f"{h.get('id')}: {p}" for p in numgate.describe(numgate.gate(text, cited or every))]
    return out


def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions: str) -> dict:
    from .agents import _strip_fences
    ensure(store)
    kind = meta["kind"]
    result = folder / "batch_01_result.json"
    if not result.exists():
        return {"stored": 0, "problems": ["batch_01_result.json is missing"]}
    batch = folder / "batch_01.json"
    data = json.loads(batch.read_text(encoding="utf-8"))
    raw = result.read_text(encoding="utf-8")
    try:
        answer = json.loads(_strip_fences(raw))
    except ValueError as err:
        return {"stored": 0, "problems": [f"not valid JSON ({err})"]}
    root = folder.parents[4]                       # <root>/output/corpus_X/run_NNN/agents/<task>
    cfg = tomllib.loads((root / meta["lens_file"]).read_text(encoding="utf-8"))
    errs = check_answer(kind, meta, data, answer, cfg)
    if errs:
        return {"stored": 0, "problems": errs}
    call_id = store.log_call(meta["run_id"], "llm", meta["agent"], f"{instructions}\n\n[{kind} batch, sha256 "
                             f"{hashlib.sha256(batch.read_bytes()).hexdigest()}]", 200, 0, raw.encode("utf-8"),
                             {"agent": meta["agent"], "task": kind, **{k: meta[k] for k in ("lens", "replicate", "mode")
                                                                       if k in meta}})
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    if kind == "lens_judge":
        with store.db:
            for x in answer["labels"]:
                k = meta["keys"][x["key"]]
                store.db.execute("INSERT INTO lens_labels (tournament, output_id, lens, hypothesis, testable, restates, "
                                 "baseline, reason, judge, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (meta["tournament"], k["output_id"], k["lens"], k["hypothesis"], int(x["testable"]),
                                  x.get("restates"), x.get("baseline"), x.get("reason"), meta["agent"], call_id, now()))
        return {"stored": len(answer["labels"]), "problems": []}
    if kind == "lens_derive":
        with store.db:
            for x in answer["labels"]:
                k = meta["keys"][x["key"]]
                store.db.execute("INSERT INTO lens_derivations (tournament, output_id, lens, hypothesis, label, auxiliary, "
                                 "reason, judge, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 (meta["tournament"], k["output_id"], k["lens"], k["hypothesis"], x["label"],
                                  x.get("auxiliary"), x.get("reason"), meta["agent"], call_id, now()))
        return {"stored": len(answer["labels"]), "problems": []}
    if kind == "cartograph":
        with store.db:
            cur = store.db.execute(
                "INSERT INTO lens_outputs (corpus, run_id, tournament, kind, lens, replicate, agent, folder, content, "
                "sha256, numbers, call_id, created_at) VALUES (?, ?, ?, 'cartograph', ?, NULL, ?, ?, ?, ?, ?, ?, ?)",
                (meta["corpus"], meta["run_id"], meta["tournament"], "+".join(meta["lenses"]), meta["agent"],
                 folder.name, json.dumps(answer, ensure_ascii=False), sha, "[]", call_id, now()))
        return {"stored": len(answer["cards"]), "output": cur.lastrowid, "problems": []}
    if kind == "lens_control" and meta.get("mode") == "all_cards":       # store lenses under their ids
        answer["lenses"] = {meta["names"].get(name, name): part for name, part in answer["lenses"].items()}
        for d in answer.get("divergences") or []:
            d["lenses"] = [meta["names"].get(n, n) for n in d.get("lenses") or []]
    texts = {f["id"]: numgate.finding_text(f["finding"], f.get("effect"), f.get("for_whom"), f.get("passage"))
             for f in data.get("findings") or []}
    flags = _number_flags(answer, texts)
    with store.db:
        cur = store.db.execute(
            "INSERT INTO lens_outputs (corpus, run_id, tournament, kind, lens, replicate, agent, folder, content, sha256, "
            "numbers, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (meta["corpus"], meta["run_id"], meta["tournament"],
             "interpret" if kind == "interpret" else meta.get("mode"), meta.get("lens"), meta.get("replicate"),
             meta["agent"], folder.name, json.dumps(answer, ensure_ascii=False), sha, json.dumps(flags), call_id, now()))
    return {"stored": 1, "output": cur.lastrowid, "number_flags": len(flags), "problems": []}


# Evaluation: code counts, no agent scores

OPPOSITE_FIT = {("predicts", "strains"), ("strains", "predicts")}


def _kappa(a: list[str], b: list[str]) -> float | None:
    return agreement.cohen_kappa(list(zip(a, b))) if a else None


def _opposite_rate(x: dict, y: dict, opposite) -> float:
    shared = [k for k in x if k in y]
    return sum((x[k], y[k]) in opposite for k in shared) / len(shared) if shared else 0.0


def _pairings(items: list):
    """All ways to split an even list into unordered pairs (105 for eight replicate answers)."""
    if not items:
        yield []
        return
    first = items[0]
    for i in range(1, len(items)):
        rest = items[1:i] + items[i + 1:]
        for p in _pairings(rest):
            yield [(first, items[i])] + p


def _between_minus_within(groups: list[tuple], opposite) -> float:
    within = [_opposite_rate(a, b, opposite) for a, b in groups]
    flat = [x for g in groups for x in g]
    inside = {(id(a), id(b)) for a, b in groups} | {(id(b), id(a)) for a, b in groups}
    between = [_opposite_rate(a, b, opposite) for a, b in itertools.combinations(flat, 2)
               if (id(a), id(b)) not in inside]
    return (sum(between) / len(between) if between else 0) - (sum(within) / len(within) if within else 0)


def permutation_test(maps: dict[str, list[dict]], opposite) -> dict:
    """Is disagreement between lenses larger than between two runs of the same lens? Exact test over every
    re-pairing of the replicate answers (lens labels shuffled)."""
    groups = [tuple(v[:2]) for v in maps.values() if len(v) >= 2]
    if len(groups) < 2:
        return {"observed": None, "p": None, "pairings": 0}
    observed = _between_minus_within(groups, opposite)
    flat = [x for g in groups for x in g]
    stats = [_between_minus_within(p, opposite) for p in _pairings(flat)]
    return {"observed": round(observed, 4), "p": round(sum(s >= observed - 1e-12 for s in stats) / len(stats), 4),
            "pairings": len(stats)}


SIGN_GAP = {("+", "-"): 2, ("-", "+"): 2, ("+", "0"): 1, ("0", "+"): 1, ("-", "0"): 1, ("0", "-"): 1}
OPPOSITE_SIGN = {("+", "-"), ("-", "+")}


def _agreement(pairs: list[tuple]) -> dict:
    """Raw agreement and kappa between two judges; kappa is undefined (None) when every label is the same."""
    if not pairs:
        return {"n": 0, "agree": None, "kappa": None}
    varied = len({x for p in pairs for x in p}) > 1
    return {"n": len(pairs), "agree": round(sum(a == b for a, b in pairs) / len(pairs), 3),
            "kappa": agreement.cohen_kappa(pairs) if varied else None}


def evaluate(store: Store, run_id: int, root: Path = ROOT, tournament: str | None = None) -> dict:
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    _, _, cfg = load(corpus, root)
    tour = tournament or cfg["interpret"].get("tournament", "pilot")
    cells = {c["cell"]: c for c in grid(cfg)}
    roles = {lens["id"]: lens["role"] for lens in cfg["lenses"]}
    runs: dict[str, list[dict]] = {}
    for o in outputs(store, run_id, tour, "interpret"):
        runs.setdefault(o["lens"], []).append({"row": o, **json.loads(o["content"])})
    for v in runs.values():
        v.sort(key=lambda r: r["row"]["replicate"])
    fit = {lens: [{x["finding"]: x["label"] for x in r["fit"]} for r in v] for lens, v in runs.items()}
    signs = {lens: [{g["cell"]: g["sign"] for g in r["grid"]} for r in v] for lens, v in runs.items()}

    per_lens = {}
    for lens, v in runs.items():
        f, s = fit[lens], signs[lens]
        counts = Counter(lab for r in f for lab in r.values())
        shared = sorted(set(f[0]) & set(f[1])) if len(f) > 1 else []
        cells_ = sorted(set(s[0]) & set(s[1])) if len(s) > 1 else []
        aux = [a for r in v for a in r.get("auxiliaries") or []]
        per_lens[lens] = {
            "role": roles.get(lens), "replicates": len(v),
            "fit": dict(counts),
            "fit_kappa": _kappa([f[0][k] for k in shared], [f[1][k] for k in shared]) if shared else None,
            "grid_kappa": _kappa([s[0][k] for k in cells_], [s[1][k] for k in cells_]) if cells_ else None,
            "grid_unknown": round(sum(x == "?" for r in s for x in r.values()) / max(1, sum(len(r) for r in s)), 3),
            "auxiliaries": len(aux),
            "hypotheses": sum(len(r.get("hypotheses") or []) for r in v),
            "number_flags": sum(len(json.loads(r["row"]["numbers"] or "[]")) for r in v),
        }

    def stable(lens):
        s = signs.get(lens) or []
        if len(s) < 2:
            return {}
        return {k: s[0][k] for k in s[0] if s[0][k] == s[1].get(k) and s[0][k] != "?"}

    # Divergences between the real lenses, each shown with the placebo's sign: a placebo that sides with one
    # lens shows that lens's prediction is what the model says anyway. Divergences involving the placebo are
    # the noise floor: how much "divergence" a lens with no content earns.
    placebo = next((lens for lens in runs if roles.get(lens) == "placebo"), None)
    sp = stable(placebo) if placebo else {}
    divergences, floor = [], Counter()
    for a, b in itertools.combinations(sorted(runs), 2):
        sa, sb = stable(a), stable(b)
        for cell in sorted(set(sa) & set(sb), key=lambda c: int(c[1:])):
            d = SIGN_GAP.get((sa[cell], sb[cell]), 0)
            if not d:
                continue
            if placebo in (a, b):
                floor[b if a == placebo else a] += 1
                continue
            pl = sp.get(cell)
            divergences.append({"cell": cell, **{k: cells[cell][k] for k in ("setting", "variable")},
                                "lenses": [a, b], "signs": [sa[cell], sb[cell]], "d": d, "placebo": pl or "?",
                                "placebo_sides_with": a if pl == sa[cell] else b if pl == sb[cell] else None})
    pc = cfg["interpret"].get("positive_control") or {}
    control_hit = [d for d in divergences if sorted(d["lenses"]) == sorted(pc.get("lenses", []))
                   and d["variable"] == pc.get("variable")]

    # the all-cards control: one agent playing every lens
    control = next((json.loads(o["content"]) for o in outputs(store, run_id, tour, "all_cards")), None)
    control_div = []
    if control:
        cs = {lens: {g["cell"]: g["sign"] for g in part.get("grid") or []} for lens, part in control["lenses"].items()}
        for a, b in itertools.combinations(sorted(cs), 2):
            for cell in sorted(set(cs[a]) & set(cs[b]), key=lambda c: int(c[1:])):
                d = SIGN_GAP.get((cs[a][cell], cs[b][cell]), 0)
                if d and "?" not in (cs[a][cell], cs[b][cell]):
                    control_div.append({"cell": cell, "lenses": [a, b], "signs": [cs[a][cell], cs[b][cell]], "d": d})
    key = lambda d: (d["cell"], tuple(sorted(d["lenses"])))  # noqa: E731
    tour_keys, ctrl_keys = {key(d) for d in divergences}, {key(d) for d in control_div}

    # hypotheses after the judge's gates
    current = {o["output_id"] for o in outputs(store, run_id, tour)}   # this run, re-runs replaced
    labels = store.db.execute("SELECT * FROM lens_labels WHERE tournament = ? ORDER BY label_id", (tour,)).fetchall()
    by_judge: dict[tuple, dict] = {}
    for r in labels:                     # the latest label of each judge counts
        if r["output_id"] in current:
            by_judge.setdefault((r["output_id"], r["lens"], r["hypothesis"]), {})[r["judge"]] = r
    judges = sorted({j for v in by_judge.values() for j in v})
    # Several judges: an item counts as judged once every judge has labelled it; credit then needs every judge
    # to call it testable and none to find it restated or in the baseline.
    lab = {k: {"testable": all(x["testable"] for x in v.values()),
               "restates": next((x["restates"] for x in v.values() if x["restates"] is not None), None),
               "baseline": next((x["baseline"] for x in v.values() if x["baseline"] is not None), None)}
           for k, v in by_judge.items() if len(v) == len(judges)}
    judge_agreement = {}
    if len(judges) >= 2:
        both = [v for v in by_judge.values() if judges[0] in v and judges[1] in v]
        for name, get in (("testable", lambda x: bool(x["testable"])), ("restates", lambda x: x["restates"] is not None),
                          ("baseline", lambda x: x["baseline"] is not None)):
            pairs = [(str(get(v[judges[0]])), str(get(v[judges[1]]))) for v in both]
            judge_agreement[name] = _agreement(pairs)
    # The derivation gate: does a hypothesis follow from its lens's core or from common sense? With several
    # judges, credit needs every judge to say it follows (from the core, or with a named auxiliary). When they
    # split between the two, the weaker reading counts (needs_auxiliary); any other split is disputed.
    derived: dict[tuple, dict] = {}
    for r in store.db.execute("SELECT * FROM lens_derivations WHERE tournament = ? ORDER BY derivation_id", (tour,)):
        if r["output_id"] in current:
            derived.setdefault((r["output_id"], r["lens"], r["hypothesis"]), {})[r["judge"]] = r
    derive_judges = sorted({j for v in derived.values() for j in v})

    def merged(v):
        said = {x["label"] for x in v.values()}
        return said.pop() if len(said) == 1 else "needs_auxiliary" if said <= set(CREDIT_DERIVATION) else "disputed"
    derivation = {k: merged(v) for k, v in derived.items() if len(v) == len(derive_judges)}   # else pending
    if len(derive_judges) >= 2:
        both = [v for v in derived.values() if all(j in v for j in derive_judges[:2])]
        judge_agreement["derivation"] = _agreement([(v[derive_judges[0]]["label"], v[derive_judges[1]]["label"])
                                                    for v in both])
    novelty_verdicts = {}
    try:
        from . import novelty
        novelty_verdicts = novelty.verdicts(store, tour)
    except Exception:                      # the novelty check is optional
        novelty_verdicts = {}
    hyp, items_by_source, log, credited_keys = {}, {}, [], set()
    for o in outputs(store, run_id, tour):
        content = json.loads(o["content"])
        if o["kind"] == "cartograph":
            continue
        sources = [(o["lens"], content.get("hypotheses") or [])] if o["kind"] == "interpret" else \
            [(lens, part.get("hypotheses") or []) for lens, part in (content.get("lenses") or {}).items()] \
            if o["kind"] == "all_cards" else [("no_card", content.get("hypotheses") or [])]
        for lens, hs in sources:
            k = (o["kind"] if o["kind"] != "interpret" else "tournament", lens)
            h = hyp.setdefault(k, {"submitted": 0, "judged": 0, "testable": 0, "restates": 0, "in_baseline": 0,
                                   "credited": 0, "derivation": Counter(), "derivation_passed": Counter(),
                                   "credited_core": 0, "novel": 0, "near": 0, "prior": 0,
                                   "literature": Counter(), "items": []})
            for x in hs:
                h["submitted"] += 1
                r = lab.get((o["output_id"], lens, x.get("id")))
                fields = {f: x.get(f) for f in ("statement", "condition", "sign", "forbids", "derived_from")}
                if r is None:
                    if k[0] == "tournament":
                        log.append({"lens": lens, "output_id": o["output_id"], "replicate": o["replicate"],
                                    "id": x.get("id"), "gate1": "unjudged", "credited": False, **fields})
                    continue
                h["judged"] += 1
                h["testable"] += bool(r["testable"])
                h["restates"] += r["restates"] is not None
                h["in_baseline"] += r["baseline"] is not None
                ok = bool(r["testable"]) and r["restates"] is None and r["baseline"] is None
                h["credited"] += ok
                d = derivation.get((o["output_id"], lens, x.get("id")))
                if d:
                    h["derivation"][d] += 1
                if ok:                           # gate 2 as a funnel: only what passed gate 1
                    h["derivation_passed"][d or "unjudged"] += 1
                core_ok = ok and d in CREDIT_DERIVATION    # no credit before gate 2 has judged it
                if core_ok and k[0] == "tournament":
                    credited_keys.add((o["output_id"], lens, x.get("id")))
                h["credited_core"] += core_ok
                nv = novelty_verdicts.get((o["output_id"], lens, x.get("id")))
                if nv:
                    h["novel"] += core_ok and nv["verdict"] == "novel"
                    h["prior"] += core_ok and nv["verdict"] == "prior"
                    h["near"] += core_ok and nv["verdict"] == "near"
                if core_ok:                      # gate 3 over what was credited: every one has a verdict
                    h["literature"][nv["verdict"] if nv else "not_searched"] += 1
                if core_ok:
                    h["items"].append(x.get("statement"))
                    items_by_source.setdefault(f"{k[0]}:{k[1]}", []).append(
                        {"output_id": o["output_id"], "id": x.get("id"), "derivation": d,
                         "novelty": nv["verdict"] if nv else None, **fields})
                if k[0] == "tournament":         # every hypothesis with its fate at each gate, for the app
                    gate1 = ("untestable" if not r["testable"] else "restates" if r["restates"] is not None
                             else "in_baseline" if r["baseline"] is not None else "passed")
                    why = derived.get((o["output_id"], lens, x.get("id"))) or {}
                    log.append({"lens": lens, "output_id": o["output_id"], "replicate": o["replicate"],
                                "id": x.get("id"), "gate1": gate1, "restates": r["restates"],
                                "baseline": r["baseline"], "derivation": d, "credited": bool(core_ok),
                                "derivation_by_judge": [{"judge": j, "label": why[j]["label"],
                                                         "auxiliary": why[j]["auxiliary"], "reason": why[j]["reason"]}
                                                        for j in sorted(why)],
                                "novelty": (nv["verdict"] if nv else "not_searched") if core_ok else None,
                                "literature": {s: nv[s] for s in ("prior", "contested", "near", "counts", "hits",
                                                                  "readable", "unlabelled", "plain_searched")}
                                if nv and core_ok else None,
                                **fields})

    # copying between blind lens answers: share of 8-word shingles a reading shares with another lens's
    def shingles(t):
        w = re.findall(r"\w+", (t or "").lower())
        return {" ".join(w[i:i + 8]) for i in range(max(0, len(w) - 7))}
    contamination = []
    flat = [(lens, r) for lens, v in runs.items() for r in v]
    for (la, ra), (lb, rb) in itertools.combinations(flat, 2):
        if la == lb:
            continue
        sa, sb = shingles(ra.get("reading")), shingles(rb.get("reading"))
        if sa and sb:
            share = len(sa & sb) / min(len(sa), len(sb))
            if share > 0.10:
                contamination.append({"lenses": [la, lb], "share": round(share, 3)})

    stance = {}
    for lens, v in runs.items():
        for r in v:
            for t in r.get("tensions") or []:
                stance.setdefault(t["insight"], {}).setdefault(lens, []).append(t["stance"])
    contested = []
    theory = [lens for lens in runs if roles.get(lens) != "placebo"]
    for fid in sorted({k for f in fit.values() for r in f for k in r}):
        labels_ = {lens: {r.get(fid) for r in fit[lens]} for lens in theory}
        if any("predicts" in s for s in labels_.values()) and any("strains" in s for s in labels_.values()):
            contested.append({"finding": fid, "labels": {k: sorted(v) for k, v in labels_.items()}})
    return {
        "tournament": tour, "lenses": per_lens,
        "fit_test": permutation_test({lens: fit[lens] for lens in theory if len(fit[lens]) > 1}, OPPOSITE_FIT),
        "grid_test": permutation_test({lens: signs[lens] for lens in theory if len(signs[lens]) > 1}, set(SIGN_GAP)),
        "divergences": divergences, "placebo_floor": dict(floor),
        "positive_control": {"config": pc, "passed": bool(control_hit) if pc.get("lenses") else None},
        "control": {"divergences": control_div, "shared_with_tournament": len(tour_keys & ctrl_keys),
                    "only_tournament": len(tour_keys - ctrl_keys), "only_control": len(ctrl_keys - tour_keys)}
        if control else None,
        "hypotheses": {f"{k[0]}:{k[1]}": {**v, "derivation": dict(v["derivation"]),
                                          "derivation_passed": dict(v["derivation_passed"]),
                                          "literature": dict(v["literature"])} for k, v in hyp.items()},
        "hypothesis_items": items_by_source, "hypothesis_log": log, "derive_judges": derive_judges,
        "novelty": novelty.summary(store, tour, credited_keys, novelty_verdicts) if novelty_verdicts else None,
        "cards": [json.loads(o["content"]) | {"lenses": o["lens"]} for o in outputs(store, run_id, tour, "cartograph")],
        "judges": judges,
        "judge_agreement": judge_agreement,
        "contamination": contamination, "tension_stances": stance, "contested_findings": contested,
    }


def report(result: dict, store: Store, run_id: int) -> str:
    cites = {r["finding_id"]: tables.works(store, [r["work_id"]])[r["work_id"]]["cite"]
             for r in tables.finding_rows(store, run_id)}
    statements = {r["finding_id"]: r["finding"] for r in tables.finding_rows(store, run_id)}
    L = result["lenses"]
    out = [f"# Lens pilot: {result['tournament']}", "",
           "Interpretation scored for fruitfulness, not audited for faithfulness. Every count below was made by "
           "code from the agents' labels; no agent gave a score.", "",
           "## Each lens", "",
           "| Lens | Role | Runs | predicts / accommodates / silent / strains | Fit agreement between its runs (kappa) "
           "| Grid agreement (kappa) | Grid '?' | Auxiliaries | Hypotheses | Number flags |",
           "| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for lens, v in L.items():
        f = v["fit"]
        k = lambda x: "–" if x is None else f"{x:.2f}"  # noqa: E731
        out.append(f"| {lens} | {v['role']} | {v['replicates']} | {f.get('predicts', 0)} / {f.get('accommodates', 0)} / "
                   f"{f.get('silent', 0)} / {f.get('strains', 0)} | {k(v['fit_kappa'])} | {k(v['grid_kappa'])} | "
                   f"{v['grid_unknown']:.0%} | {v['auxiliaries']} | {v['hypotheses']} | {v['number_flags']} |")
    ft, gt = result["fit_test"], result["grid_test"]
    out += ["", "## Do the lenses differ more than two runs of the same lens?", "",
            f"- Fit labels (predicts vs strains): between-lens minus within-lens disagreement {ft['observed']}, exact "
            f"permutation p = {ft['p']} over {ft['pairings']} re-pairings of the replicate answers.",
            f"- Grid signs (any difference between signs): {gt['observed']}, p = {gt['p']}.",
            f"- With {ft['pairings']} possible re-pairings the smallest possible p is {1 / ft['pairings']:.3f}"
            if ft.get("pairings") else "- Not enough replicates for the test.",
            "- A small p means the lenses produce systematically different readings, not just noise. This is the "
            "pilot's decision point: if they do not, the cores need sharpening before more is built.", "",
            "## Divergences on the grid (both runs of each lens agree)", "",
            f"Positive control ({' vs '.join(result['positive_control']['config'].get('lenses', []))} on "
            f"{result['positive_control']['config'].get('variable')}): "
            f"{ {True: 'passed', False: 'NOT passed', None: 'not configured'}[result['positive_control']['passed']]}.",
            "",
            "Weight 2 = opposite signs, 1 = an effect against no difference. The placebo column shows the sign a "
            "lens with no content gives the same cell; where it sides with one lens, that lens's prediction is what "
            "the model says anyway, and the divergence is weaker evidence of a theory difference.", "",
            "| Cell | Setting | Variable | Lenses | Signs | Weight | Placebo | Placebo sides with |",
            "| --- | --- | --- | --- | --- | ---: | --- | --- |"]
    out += [f"| {d['cell']} | {d['setting']} | {d['variable']} | {' vs '.join(d['lenses'])} | "
            f"{' vs '.join(d['signs'])} | {d['d']} | {d['placebo']} | {d['placebo_sides_with'] or '–'} |"
            for d in result["divergences"]] or ["| – | | | | | | | |"]
    if result.get("placebo_floor"):
        out += ["", "Noise floor: divergences between the placebo and each lens: "
                + ", ".join(f"{k} {v}" for k, v in sorted(result["placebo_floor"].items())) + "."]
    c = result["control"]
    if c:
        out += ["", "## Many agents or one?", "",
                f"One agent playing every lens found {len(c['divergences'])} divergences; the separate lenses found "
                f"{len(result['divergences'])}. Shared: {c['shared_with_tournament']}; only the separate lenses: "
                f"{c['only_tournament']}; only the single agent: {c['only_control']}."]
    out += ["", "## Hypotheses after the judge's gates", "",
            "Three gates, all judged blind by two judges and counted by code:", "",
            "1. testable, not a restatement of a finding, not already in the no-lens baseline (what the model says "
            "anyway);",
            "2. follows from the lens's core (directly or with a named auxiliary), not from common sense; when one "
            "judge says directly and the other with an auxiliary, it counts as with an auxiliary;",
            "3. literature: every judge says a study already states or tests it (prior), only some say so or only "
            "some see a neighbouring test (contested), every judge says a study tests a close neighbour, the same "
            "mechanism with another outcome or population (near), or nothing was found in the logged searches "
            "(novel). Other: not searchable (no hit with an abstract), unjudged, or not searched yet.", "",
            "| Source | Submitted | Gate 1 | Common sense | From core | With auxiliary | Disputed | Gate 1+2 | Novel "
            "| Near | Contested | Prior | Other |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for k, v in sorted(result["hypotheses"].items()):
        if k.startswith("no_card"):
            continue
        d = v.get("derivation_passed") or {}             # gate 2 over what passed gate 1, so each row adds up
        lit = v.get("literature") or {}                  # gate 3 over what was credited
        main = ("novel", "near", "contested", "prior")
        gate3 = " | ".join(str(lit.get(x, 0)) for x in main) + f" | {sum(n for x, n in lit.items() if x not in main)}"             if k.startswith("tournament:") else " | ".join(["-"] * 5)
        out.append(f"| {k} | {v['submitted']} | {v['credited']} | {d.get('common_sense', 0)} | "
                   f"{d.get('follows_from_core', 0)} | {d.get('needs_auxiliary', 0)} | {d.get('disputed', 0)} | "
                   f"{v['credited_core']} | {gate3} |")
    alls = {k.split(":", 1)[1]: v["derivation"].get("common_sense", 0) for k, v in sorted(result["hypotheses"].items())
            if k.startswith("tournament:") and v.get("derivation")}
    if alls:
        out += ["", "The gate-2 columns count only the hypotheses that passed gate 1, the gate-3 columns only the "
                "credited ones; the one-agent control was not searched. The judges labelled every "
                "hypothesis; counted over all of them, common sense: "
                + ", ".join(f"{lens} {n}" for lens, n in alls.items()) + "."]
    base = result["hypotheses"].get("no_card:no_card")
    if base:
        out += ["", f"The no-lens baseline proposed {base['submitted']} hypotheses; they are the comparison set, "
                "not judged themselves."]
    if result.get("judge_agreement"):
        out += ["", f"Judges: {', '.join(result['judges'])}. An item counts once every judge has labelled it, and "
                "credit needs every judge on every gate. Agreement between them: "
                + "; ".join(f"{k} {v['agree']:.0%} (kappa {v['kappa']:.2f}, n = {v['n']})" if v["kappa"] is not None
                            else f"{k} {v['agree']:.0%} (kappa undefined: every label the same, n = {v['n']})"
                            for k, v in result["judge_agreement"].items() if v["agree"] is not None)
                + "."]
    nov = result.get("novelty")
    if nov:
        out += ["", "### The literature check", "",
                f"{nov['searched']} of the {nov['checked']} credited hypotheses with search strings were searched in "
                f"OpenAlex (strict and broad boolean strings) and Semantic Scholar (plain text, OpenAlex as fallback): {nov['hits']} hits, "
                f"{nov['readable']} with an abstract. Verdicts: "
                + ", ".join(f"{k} {v}" for k, v in sorted(nov["verdicts"].items())) + ".",
                f"Recall controls: {nov['controls']} findings of the review's own core studies were searched the same "
                f"way ({nov.get('control_hits', 0)} further hits). The search found the source study for "
                f"{nov['controls_found_by_search']}, and every judge labelled that study as prior for "
                f"{nov['controls_judged_prior']}. The controls describe a known study, which is easier to find than "
                "a prior test of a prediction. 'Novel' means no judge saw a prior or neighbouring test in these logged "
                "searches, not that none exists; only titles and abstracts were read."]
        if nov.get("unlabelled"):
            out.append(f"{nov['unlabelled']} hits with an abstract still lack a judge's label (searched after the "
                       "judges ran); their hypotheses are 'unjudged' until novelty_judge runs again.")
        if nov.get("plain_rewritten"):
            out.append(f"For {nov['plain_rewritten']} hypotheses (and {nov.get('controls_plain_rewritten', 0)} recall "
                       "controls) the plain search had found nothing, so a fresh agent rewrote only their plain query "
                       "as 3-5 key terms (novelty_plain; old and new query in novelty_rewrites), and it ran again on "
                       "OpenAlex. Their boolean strings are unchanged.")
        if nov.get("without_plain"):
            out.append(f"The plain-text search found nothing new for {nov['without_plain']} of the {nov['searched']} "
                       "searched hypotheses: rate limits refused some (the calls log has each refusal), a plain query "
                       "matches only titles and abstracts that contain all its words, and works the boolean searches "
                       "had found already are not counted twice. Their verdicts rest on the boolean searches alone.")
        if nov.get("judge_agreement_on_prior"):
            a = nov["judge_agreement_on_prior"]
            out.append(f"Judges agreed on whether a hit states or tests the hypothesis for {a['agree']:.0%} of "
                       f"{a['n']} hits; both said it does for {a.get('both_prior', 0)}, at least one for "
                       f"{a.get('either_prior', 0)}.")
    for k, items in sorted(result.get("hypothesis_items", {}).items()):
        if items and k.startswith("tournament:"):
            out += ["", f"Credited after gates 1 and 2, {k.split(':', 1)[1]}:"]
            out += [f"- {h['statement']}" + (f" *({h['novelty']})*" if h.get("novelty") else "")
                    + (" *(with auxiliary)*" if h.get("derivation") == "needs_auxiliary" else "") for h in items]
    for card in result.get("cards") or []:
        out += ["", f"## Test cards: {card.get('lenses', '').replace('+', ' vs ')}", "",
                "The cartographer turns disagreements into tests that would tell the lenses apart. It does not "
                "decide which lens is right."]
        for c in card.get("cards") or []:
            t = c.get("test") or {}
            m = t.get("measures") or {}
            out += ["", f"### {c.get('id')}: {c.get('question')}", ""]
            out += [f"- **{p.get('lens')}** predicts: {p.get('prediction')} *({p.get('from', '')})*"
                    for p in c.get("positions") or []]
            fields = [("Design", t.get("design")), ("Data", t.get("data")), ("Population", t.get("population")),
                      ("Predictor", m.get("predictor")), ("Moderator", m.get("moderator")),
                      ("Outcome", m.get("outcome")), ("Contrast", t.get("contrast")), ("Timing", t.get("timing")),
                      ("Analysis", t.get("analysis"))]
            out += ["", "**Test:**"] + [f"- {name}: {value}" for name, value in fields if value]
            out += ["", "**Possible results:**"]
            out += [f"- {pt.get('result')} → favours {', '.join(pt.get('favours') or [])}" for pt in c.get("patterns") or []]
            if c.get("existing_evidence"):
                out += ["", "**Existing evidence, not decisive:**"] + [
                    f"- Finding {e.get('finding')} ({cites.get(e.get('finding'), '?')}): {e.get('why_not_decisive')}"
                    for e in c["existing_evidence"]]
            if c.get("artifact_threats"):
                out += ["", "**Artifact threats:**"] + [f"- {x}" for x in c["artifact_threats"]]
            if c.get("facts_to_verify"):
                out += ["", "**Facts to verify (not checked by the pipeline):**"] + [f"- {x}" for x in c["facts_to_verify"]]
    out += ["", "## Contested findings (one lens predicts what another strains at)", ""]
    out += [f"- Finding {x['finding']} ({cites.get(x['finding'], '?')}): {statements.get(x['finding'], '')[:160]} — "
            + "; ".join(f"{k}: {'/'.join(v)}" for k, v in x["labels"].items()) for x in result["contested_findings"]] \
        or ["- none"]
    out += ["", "## Stances on the contradictions in the evidence", ""]
    for ins, by in sorted(result["tension_stances"].items()):
        out.append(f"- Insight {ins}: " + "; ".join(f"{k}: {'/'.join(v)}" for k, v in sorted(by.items())))
    out += ["", "## Copying between blind lenses", "",
            ("Readings sharing more than 10% of 8-word sequences: "
             + "; ".join(f"{' & '.join(x['lenses'])} {x['share']:.0%}" for x in result["contamination"]))
            if result["contamination"] else "No reading shares more than 10% of its 8-word sequences with another lens."]
    return "\n".join(out) + "\n"
