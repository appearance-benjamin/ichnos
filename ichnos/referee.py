"""The referee: a temporal backtest of how well lenses write propositions that later studies confirm.

A lens here is a way of reading the evidence (conservative, creative, cross-field ...), declared with the round's
other settings in protocol/referee_corpus_X.toml, which is pre-registered: its hash is fixed when the round begins.
Studies are split by their first publication date (Crossref, evolution.fetch_dates):
- training: first published before T. Every lens reads these findings, and only these;
- test: first published from T on and, by default, after the models' training cutoff (the leakage guard);
- excluded: from T on but inside the training window, or without the exact date the split needs.

Each lens writes falsifiable propositions: a claim with its population, contrast, outcome and direction, its
moderators, a stated confidence, and what would confirm or contradict it. The propositions are frozen, with a
timestamp and a sha256 in the evidence store (and a copy in the run's folder), before any agent sees a test study.
Then a judge who does not know which source wrote what rates every test finding against every proposition: supports,
contradicts, or does not address it. Two judges of different tiers where affordable; their agreement is reported.

Controls:
- placebo: Lens P, the vacuous perspective of lenses.py, shown to the generator as one perspective among others;
- no_lens: one agent with the evidence and no perspective;
- shuffled: propositions code makes by recombining the lenses' populations, contrasts, outcomes and directions at
  random, so that they commit as much as real ones but rest on nothing;
- the contamination probe: the generating model says what each test study found from its title, authors, year and
  venue alone, beside control studies from the year before the cutoff, and a second agent compares its answers with
  the real findings. Test studies it seems to remember are flagged and, by default, left out of the headline.

Scores, computed by code (no agent scores anything):
- each proposition's weight, from what it commits to: a direction counts more than "depends", each moderator with its
  own effect adds, and a proposition every rater calls obvious counts less;
- per test finding and source: the weighted net support of the source's propositions (supports +1, contradicts -1,
  averaged over the judges), divided by their number, so that every source has the same budget;
- the score: that summed over the test findings, the weighted net confirmations per proposition; beside it coverage,
  hit rate and contradiction rate over the propositions a test finding addressed, and the Brier score of the stated
  confidences;
- each lens against each control: the difference with a 95% interval from resampling whole studies, and a
  permutation test that reassigns propositions between the two sources.

Task kinds, in task folders like every other agent step:
- ref_generate: one source writes its propositions from the training evidence;
- ref_probe:    the contamination probe;
- ref_check:    does a probe answer reproduce the study's findings, and does the title give them away?
- ref_rate:     which propositions are obvious? Every proposition, masked, once per rater;
- ref_judge:    which propositions does each test finding support or contradict? Masked, a few findings per batch.

The round is a standing registry: when a later search update adds studies to the run, `referee RUN` puts them on the
split (one published before T is late and counts nowhere), probes and judges only the new test findings, and the
report counts every wave. Every judged pair is stored with its finding, study and judge (ref_labels), so that the same
scores can later calibrate an evidence-quality grid (study credibility, density and consistency, breadth).

A claims round (`mode = "claims"`) checks the review's own finished papers forward instead. Its sources are papers
([[sources]], each with the `paper` it states): an agent writes each paper's claims about the world as propositions,
restating them, and they are frozen like any others. The papers read every study the run has when the round begins,
so all of these are seen (training); only studies the run gains later, first published after the models' cutoff, test
the claims. No probe (a later study cannot be in the papers or in the models' training data) and no obviousness
rating (no lenses are compared); the shuffled control stays, as the judges' check. Home shows each claim with the
later studies that support or contradict it (claims_summary).
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
import re
import tomllib
from collections import Counter
from pathlib import Path

from . import evolution, lenses, tables, writer
from .config import ROOT
from .store import Store, now

KINDS = ("ref_generate", "ref_probe", "ref_check", "ref_rate", "ref_judge")
BASELINES = ("placebo", "no_lens", "shuffled")
DIRECTIONS = ("higher", "lower", "no_difference", "depends")
EFFECTS = ("stronger", "weaker", "reversed", "absent")
VERDICTS = {"supports": 1, "contradicts": -1}
REPRODUCED = ("yes", "partly", "no")
SHOWN = ("claim", "population", "contrast", "outcome", "direction", "moderators", "confirms", "contradicts")
WORDS = {"claim": 40, "population": 25, "contrast": 25, "outcome": 20, "confirms": 40, "contradicts": 40}
WEIGHTS = {"higher": 1.0, "lower": 1.0, "no_difference": 0.8, "depends": 0.3}
PLACEBO = {"name": "Lens P", "stance": "What mattered to these people, and in which context? People differ, and context "
                                       "matters for how people respond to events."}
PROP_ID = re.compile(r"R\d+")
UNDATED = "no exact date"
SEEN = "seen by the papers"
MIN_CLAIMS = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_rounds (
    round       TEXT PRIMARY KEY,
    corpus      TEXT NOT NULL,
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    config_path TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    config      TEXT NOT NULL,                   -- the pre-registered file as it was when the round began
    created_at  TEXT NOT NULL,
    frozen_at   TEXT,                            -- when every proposition was in, before any agent saw a test study
    frozen_sha256 TEXT                           -- sha256 of every proposition, as digest() computes it
);
CREATE TABLE IF NOT EXISTS ref_studies (
    round       TEXT NOT NULL,
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    split       TEXT NOT NULL,                   -- training, test, excluded
    published   TEXT,                            -- the date the split used (Crossref's earliest), or the year
    reason      TEXT,                            -- dated, by year, or why the study is excluded
    wave        INTEGER NOT NULL,                -- 0: fixed when the round began; 1, 2, ...: later search updates
    added_at    TEXT NOT NULL,
    PRIMARY KEY (round, work_id)
);
CREATE TABLE IF NOT EXISTS ref_tasks (
    round       TEXT NOT NULL,
    need        TEXT NOT NULL,                   -- e.g. generate:creative:1, judge:claude-opus-5-5:<hash>
    kind        TEXT NOT NULL,
    folder      TEXT NOT NULL,
    status      TEXT NOT NULL,                   -- open, imported
    created_at  TEXT NOT NULL,
    PRIMARY KEY (round, need)
);
CREATE TABLE IF NOT EXISTS ref_propositions (
    round       TEXT NOT NULL,
    key         TEXT NOT NULL,                   -- <source>.r<replicate>.<id>, e.g. creative.r1.R3, shuffled.r2.S5
    source      TEXT NOT NULL,
    replicate   INTEGER NOT NULL,
    content     TEXT NOT NULL,                   -- JSON: claim, population, contrast, outcome, direction, moderators,
                                                 -- confidence, confirms, contradicts, basis
    sha256      TEXT NOT NULL,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (round, key)
);
CREATE TABLE IF NOT EXISTS ref_labels (
    label_id    INTEGER PRIMARY KEY,
    round       TEXT NOT NULL,
    kind        TEXT NOT NULL,                   -- probe, check, rate, judge
    agent       TEXT NOT NULL,
    work_id     INTEGER,
    finding_id  INTEGER,
    key         TEXT,                            -- a proposition (rate, judge)
    label       TEXT NOT NULL,                   -- known/unknown; yes/partly/no; obvious/not_obvious;
                                                 -- judged (one per finding and judge), supports, contradicts
    detail      TEXT,                            -- JSON: recalled statements, title_states, a reason
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_events (
    event_id    INTEGER PRIMARY KEY,
    round       TEXT NOT NULL,
    event       TEXT NOT NULL,                   -- started, frozen, wave
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


# The pre-registered file

def load(corpus: str, root: Path = ROOT) -> tuple[Path, str, dict]:
    path = root / "protocol" / f"referee_corpus_{corpus.lower()}.toml"
    if not path.exists():
        raise ValueError(f"no referee file: {path.relative_to(root).as_posix()}")
    text = path.read_text(encoding="utf-8")
    return path, text, tomllib.loads(text)


def claims(cfg: dict) -> bool:
    """A claims round: the sources are the review's finished papers, checked against the studies the run gains later."""
    return cfg.get("mode") == "claims"


def check_config(cfg: dict) -> None:
    group = "sources" if claims(cfg) else "lenses"
    for key in ("round", "corpus", "run", "seed", "domain", "split", "propositions", group, "agents"):
        if key not in cfg:
            raise ValueError(f"the referee file needs `{key}`")
    for key in ("T", "model_cutoff"):
        if not re.fullmatch(r"\d{4}-\d\d-\d\d", str(cfg["split"].get(key, ""))):
            raise ValueError(f"[split] {key} = \"YYYY-MM-DD\"")
    if per_source(cfg) < (MIN_CLAIMS if claims(cfg) else 1) or reps(cfg) < 1:
        raise ValueError("[propositions] per_source and replicates are 1 or more"
                         + (f" (per_source, the most claims a paper states, at least {MIN_CLAIMS})" if claims(cfg) else ""))
    ids = [x.get("id") for x in cfg[group]]
    if not ids or len(set(ids)) != len(ids) or set(ids) & set(BASELINES):
        raise ValueError(f"[[{group}]] need distinct ids, none of {', '.join(BASELINES)}")
    for x in cfg[group]:
        for key in ("id", "name") + (("paper",) if claims(cfg) else ("stance",)):
            if not x.get(key):
                raise ValueError(f"{group[:-1]} {x.get('id', '?')} needs `{key}`")
    a = cfg["agents"]
    roles = ("generate",) if claims(cfg) else ("generate", "probe", "check")
    if not all(a.get(k) for k in roles) or not isinstance(a.get("judges"), list) \
            or not a["judges"] or len(set(a["judges"])) != len(a["judges"]):
        raise ValueError(f"[agents] needs {', '.join(roles)} and a list of distinct judges")
    if set(((cfg.get("score") or {}).get("direction") or {})) - set(DIRECTIONS):
        raise ValueError(f"[score] direction weighs only {', '.join(DIRECTIONS)}")


def per_source(cfg: dict) -> int:
    return int(cfg["propositions"].get("per_source", 0))


def reps(cfg: dict) -> int:
    return 1 if claims(cfg) else int(cfg["propositions"].get("replicates", 1))      # a paper states its claims once


def sources(cfg: dict) -> list[str]:
    """The sources an agent writes: every lens, then the placebo and the no-lens control unless switched off; in a
    claims round, the papers."""
    if claims(cfg):
        return lens_ids(cfg)
    b = cfg.get("baselines") or {}
    return [lens["id"] for lens in cfg["lenses"]] + [x for x in ("placebo", "no_lens") if b.get(x, True) is not False]


def shuffled_on(cfg: dict) -> bool:
    return (cfg.get("baselines") or {}).get("shuffled", True) is not False


def all_sources(cfg: dict) -> list[str]:
    return sources(cfg) + (["shuffled"] if shuffled_on(cfg) else [])


def lens_ids(cfg: dict) -> list[str]:
    return [x["id"] for x in cfg["sources" if claims(cfg) else "lenses"]]


def perspective(cfg: dict, source: str) -> dict | None:
    """What the generator is told of its source: a lens's stance, the placebo's under its own name, or nothing."""
    if source == "no_lens":
        return None
    if source == "placebo":
        p = (cfg.get("baselines") or {}).get("placebo")
        return {**PLACEBO, **p} if isinstance(p, dict) else dict(PLACEBO)
    lens = next(x for x in cfg["lenses"] if x["id"] == source)
    return {"name": lens["name"], "stance": " ".join(lens["stance"].split())}


def short(agent: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", agent.split(":")[-1].lower()).strip("-")


def _cfg_of(store: Store, run_id: int, root: Path) -> tuple[str, Path, str, dict]:
    row = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    path, text, cfg = load(row["corpus"], root)
    check_config(cfg)
    if int(cfg["run"]) != run_id or str(cfg["corpus"]).upper() != row["corpus"].upper():
        raise ValueError(f"round {cfg['round']} is pre-registered for corpus {cfg['corpus']}, run {cfg['run']}")
    return row["corpus"], path, text, cfg


# The split

def test_from(cfg: dict) -> str:
    """The first day of the test window: T, or the day after the models' cutoff where the leakage guard is on."""
    sp = cfg["split"]
    start = sp["T"]
    if sp.get("after_cutoff", True):
        start = max(start, (dt.date.fromisoformat(sp["model_cutoff"]) + dt.timedelta(days=1)).isoformat())
    return start


def place(cfg: dict, year, published: str | None) -> tuple[str, str | None, str]:
    """A study's split, the date it rests on and why. A study is out by the end of its year at the latest; without an
    exact date it is placed by its year only where the whole year lies on one side."""
    T, start = cfg["split"]["T"], test_from(cfg)
    if published:
        if year:
            published = min(published, f"{int(year):04d}-12-31")
        if published < T:
            return "training", published, "dated"
        if published < start:
            return "excluded", published, "from T on, but inside the models' training window"
        return "test", published, "dated"
    if not year:
        return "excluded", None, "no year"
    if f"{int(year):04d}-12-31" < T:
        return "training", str(year), "by year"
    if f"{int(year):04d}-01-01" >= start:
        return "test", str(year), "by year"
    return "excluded", str(year), UNDATED


def _dates_cfg(cfg: dict) -> dict:
    """evolution's date functions want studies from T's year on: they read the year of `cutoff`."""
    return {"split": {"cutoff": cfg["split"]["T"]}}


def missing_dates(store: Store, cfg: dict, meta: dict) -> list[int]:
    cand = evolution.date_candidates(_dates_cfg(cfg), meta)
    have = evolution.dates(store, cand)
    return [w for w in cand if w not in have or have[w]["status"] == "error"]


def fetch_dates(store: Store, run_id: int, root: Path = ROOT, http=None) -> Counter:
    """Crossref's earliest date for every study of the run from T's year on (or without a year)."""
    _, _, _, cfg = _cfg_of(store, run_id, root)
    return evolution.fetch_dates(store, run_id, _dates_cfg(cfg), http)


def placements(store: Store, run_id: int, cfg: dict) -> tuple[list, dict, dict]:
    """The run's findings, their studies, and each study's place as the split stands today."""
    rows = tables.finding_rows(store, run_id)
    meta = tables.works(store, {r["work_id"] for r in rows})
    dated = evolution.dates(store, meta)
    return rows, meta, {w: place(cfg, m["year"], (dated.get(w) or {}).get("published")) for w, m in meta.items()}


def opening(store: Store, run_id: int, cfg: dict) -> tuple[list, dict, dict]:
    """The split as a round begins: by date, or in a claims round every study the papers read, which is all of them."""
    rows, meta, split = placements(store, run_id, cfg)
    if claims(cfg):
        split = {w: ("training", d, SEEN) for w, (_, d, _) in split.items()}
    return rows, meta, split


# The round

def round_(store: Store, run_id: int, root: Path = ROOT, start: bool = False) -> dict:
    """The run's round: on --start the file's hash and the split are fixed; afterwards the file must be unchanged."""
    ensure(store)
    corpus, path, text, cfg = _cfg_of(store, run_id, root)
    sha = evolution._sha(text)
    rel = path.relative_to(root).as_posix()
    R = {"round": cfg["round"], "cfg": cfg, "sha": sha, "corpus": corpus, "run_id": run_id, "root": root, "path": rel}
    row = store.db.execute("SELECT * FROM ref_rounds WHERE round = ?", (cfg["round"],)).fetchone()
    if row:
        if row["config_sha256"] != sha:
            raise ValueError(f"{rel} changed after round {cfg['round']} began; restore it, or give it a new `round` id "
                             "to start a new round")
        return R | {"row": dict(row)}
    if not start:
        raise ValueError(f"round {cfg['round']} has not begun. See its split and cost with `referee {run_id} --plan`; "
                         "begin it with --start")
    _start(store, R, text)
    return round_(store, run_id, root)


def _start(store: Store, R: dict, text: str) -> None:
    cfg = R["cfg"]
    rows, meta, split = opening(store, R["run_id"], cfg)
    if not rows:
        raise ValueError("the run has no findings to test propositions on")
    missing = [] if claims(cfg) else missing_dates(store, cfg, meta)     # a claims round dates only later studies
    if missing:
        raise ValueError(f"no publication date yet for {len(missing)} studies; fetch them first with "
                         f"`referee {R['run_id']} --dates`")
    train = sorted(r["finding_id"] for r in rows if split[r["work_id"]][0] == "training")
    if not train or not (claims(cfg) or any(s == "test" for s, _, _ in split.values())):
        raise ValueError("the split leaves no training or no test studies")
    at = now()
    with store.db:
        store.db.execute("INSERT INTO ref_rounds (round, corpus, run_id, config_path, config_sha256, config, created_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (R["round"], R["corpus"], R["run_id"], R["path"], R["sha"],
                                                           text, at))
        for w, (s, d, why) in sorted(split.items()):
            store.db.execute("INSERT INTO ref_studies VALUES (?, ?, ?, ?, ?, 0, ?)", (R["round"], w, s, d, why, at))
        _event(store, R["round"], "started", {
            "config": R["path"], "config_sha256": R["sha"], "T": cfg["split"]["T"], "test_from": test_from(cfg),
            "studies": dict(Counter(s for s, _, _ in split.values())), "training_findings": len(train),
            "training_sha256": evolution._sha(json.dumps(train))})


def _event(store: Store, round_id: str, event: str, detail: dict) -> None:
    store.db.execute("INSERT INTO ref_events (round, event, detail, created_at) VALUES (?, ?, ?, ?)",
                     (round_id, event, json.dumps(detail, ensure_ascii=False), now()))


def events(store: Store, round_id: str, event: str) -> list[dict]:
    return [json.loads(r["detail"]) for r in store.db.execute(
        "SELECT detail FROM ref_events WHERE round = ? AND event = ? ORDER BY event_id", (round_id, event))]


def studies(store: Store, round_id: str) -> dict[int, dict]:
    return {r["work_id"]: dict(r) for r in store.db.execute("SELECT * FROM ref_studies WHERE round = ?", (round_id,))}


def tasks(store: Store, round_id: str) -> dict[str, dict]:
    return {r["need"]: dict(r) for r in store.db.execute("SELECT * FROM ref_tasks WHERE round = ?", (round_id,))}


def propositions(store: Store, round_id: str) -> dict[str, dict]:
    return {r["key"]: {"source": r["source"], "replicate": r["replicate"], "content": json.loads(r["content"])}
            for r in store.db.execute("SELECT * FROM ref_propositions WHERE round = ? ORDER BY key", (round_id,))}


def digest(props: dict[str, dict]) -> str:
    """The sha256 of every proposition's key and content, independent of the order they were stored in."""
    return evolution._sha(json.dumps({k: v["content"] for k, v in sorted(props.items())}, ensure_ascii=False,
                                     sort_keys=True))


def split_findings(store: Store, R: dict, split: str) -> dict[int, int]:
    """The run's current findings on one side of the split: {finding_id: work_id}."""
    st = studies(store, R["round"])
    return {r["finding_id"]: r["work_id"] for r in tables.finding_rows(store, R["run_id"])
            if (st.get(r["work_id"]) or {}).get("split") == split}


def shown(cfg: dict, round_id: str, ids: list[int]) -> list[int]:
    cap = int(cfg["split"].get("evidence_max") or 0)
    if not cap or len(ids) <= cap:
        return sorted(ids)
    return sorted(evolution._order(ids, round_id, cfg["seed"], "evidence")[:cap])


def update_waves(store: Store, R: dict) -> int:
    """Studies the run gained since the round began, and undated ones that have a date now, go on the split as a new
    wave. None can join training: the training set was fixed when the round began. Returns the wave, or 0."""
    rid = R["round"]
    have = studies(store, rid)
    _, meta, split = placements(store, R["run_id"], R["cfg"])
    new = {}
    for w, (s, d, why) in split.items():
        old = have.get(w)
        if old and not (old["split"] == "excluded" and old["reason"] == UNDATED):
            continue
        if s == "training":
            s, why = "excluded", "late: published before T, added after the round began"
        if old and (s, why) == (old["split"], old["reason"]):
            continue
        new[w] = (s, d, why)
    if not new:
        return 0
    wave = max((x["wave"] for x in have.values()), default=0) + 1
    at = now()
    with store.db:
        for w, (s, d, why) in sorted(new.items()):
            store.db.execute("INSERT OR REPLACE INTO ref_studies VALUES (?, ?, ?, ?, ?, ?, ?)", (rid, w, s, d, why, wave, at))
        _event(store, rid, "wave", {"wave": wave, "studies": {str(w): s for w, (s, _, _) in sorted(new.items())}})
    return wave


# Writing tasks

def _write(store: Store, R: dict, kind: str, need: str, label: str, instructions: str, batches: list, meta: dict,
           agent: str, files: dict[str, str] | None = None) -> Path:
    from .agents import _new_folder
    from .config import load_protocol
    root = R["root"]
    ppath, ptext, _ = load_protocol(R["corpus"], root)
    psha = store.add_protocol(R["corpus"], ppath.relative_to(root).as_posix(), ptext)
    out = _new_folder(root, R["corpus"], R["run_id"], kind, f"{R['round']}-{label}")
    (out / "instructions.md").write_text(instructions, encoding="utf-8")
    for i, b in enumerate(batches, 1):
        (out / f"batch_{i:02d}.json").write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    for name, text in (files or {}).items():
        (out / name).write_text(text, encoding="utf-8")
    meta = {"kind": kind, "agent": agent, "run_id": R["run_id"], "corpus": R["corpus"], "protocol_sha256": psha,
            "round": R["round"], "config_sha256": R["sha"], "need": need, "batches": len(batches), **meta}
    (out / "task.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO ref_tasks VALUES (?, ?, ?, ?, 'open', ?)",
                         (R["round"], need, kind, out.relative_to(root).as_posix(), now()))
    return out


def _design(chart: dict, work_id: int) -> str:
    return ", ".join((chart.get(work_id, {}).get("design") or {}).get("values", [])) or "not charted"


def _evidence(store: Store, R: dict, ids: list[int], *order) -> list[dict]:
    """Training findings as a lens sees them: the study, its year and design, the finding and its coded fields."""
    rows = {r["finding_id"]: r for r in tables.finding_rows(store, R["run_id"])}
    meta = tables.works(store, {rows[f]["work_id"] for f in ids}, R["run_id"])
    chart = tables.charting(store, R["run_id"])
    out = []
    for f in evolution._order(ids, *order):
        r, m = rows[f], meta[rows[f]["work_id"]]
        item = {"id": f, "study": m["cite"], "year": m["year"], "design": _design(chart, r["work_id"]),
                "finding": r["finding"]}
        item.update({k: r[k] for k in ("effect", "for_whom") if r[k]})
        out.append({**item, **json.loads(r["fields"] or "{}")})
    return out


def _test_items(store: Store, R: dict, ids: list[int]) -> list[dict]:
    """Test findings as a judge sees them: the finding, its passage and coded fields, without the study's name."""
    rows = {r["finding_id"]: r for r in tables.finding_rows(store, R["run_id"])}
    chart = tables.charting(store, R["run_id"])
    out = []
    for f in ids:
        r = rows[f]
        item = {"id": f, "design": _design(chart, r["work_id"]), "finding": r["finding"]}
        item.update({k: r[k] for k in ("effect", "for_whom") if r[k]})
        out.append({**item, "passage": r["evidence"], **json.loads(r["fields"] or "{}")})
    return out


def generate_task(store: Store, R: dict, source: str, rep: int) -> Path:
    cfg = R["cfg"]
    ids = shown(cfg, R["round"], sorted(split_findings(store, R, "training")))
    items = _evidence(store, R, ids, R["round"], source, rep, "evidence")
    ev = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items)
    persp = perspective(cfg, source)
    data = {"domain": cfg["domain"], "T": cfg["split"]["T"], "propositions": per_source(cfg), "perspective": persp,
            "evidence_file": "evidence.jsonl", "evidence_findings": len(items)}
    return _write(store, R, "ref_generate", f"generate:{source}:{rep}", f"{source}-r{rep}",
                  generate_instructions(cfg, persp, len(items)), [data],
                  {"source": source, "replicate": rep, "evidence_ids": sorted(ids), "evidence_sha256": evolution._sha(ev),
                   "records": per_source(cfg)}, cfg["agents"]["generate"], {"evidence.jsonl": ev})


def claims_task(store: Store, R: dict, source: str, rep: int) -> Path:
    """A paper's claims as propositions: the manuscript as finished, and the findings it rests on (for basis)."""
    from . import paper as paper_mod
    cfg = R["cfg"]
    src = next(s for s in cfg["sources"] if s["id"] == source)
    paper_mod.ensure(store)
    row = store.db.execute("SELECT paper_id, version, title, created_at FROM papers WHERE paper_id = ?",
                           (int(src["paper"]),)).fetchone()
    if row is None:
        raise ValueError(f"source {source}: there is no paper {src['paper']}")
    text = paper_mod.render(store, row["paper_id"], full=False)
    ids = sorted(split_findings(store, R, "training"))
    items = _evidence(store, R, ids, R["round"], source, rep, "evidence")
    ev = "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in items)
    data = {"domain": cfg["domain"], "paper": {"id": row["paper_id"], "version": row["version"], "title": row["title"],
                                               "finished": row["created_at"][:10]},
            "manuscript_file": "manuscript.md", "evidence_file": "evidence.jsonl", "evidence_findings": len(items),
            "propositions": per_source(cfg)}
    return _write(store, R, "ref_generate", f"generate:{source}:{rep}", f"{source}-r{rep}",
                  claims_instructions(cfg, src, len(items)), [data],
                  {"source": source, "replicate": rep, "paper": row["paper_id"], "evidence_ids": ids,
                   "evidence_sha256": evolution._sha(ev), "manuscript_sha256": evolution._sha(text),
                   "records": per_source(cfg)}, cfg["agents"]["generate"], {"evidence.jsonl": ev, "manuscript.md": text})


def mask(R: dict, props: dict) -> dict[str, str]:
    """One mask for every rater and judge: P001, P002, ... in a seeded order, no source or replicate in sight."""
    keys = evolution._order(list(props), R["round"], R["cfg"]["seed"], "mask")
    return {f"P{n:03d}": k for n, k in enumerate(keys, 1)}


def public(content: dict) -> dict:
    return {k: content[k] for k in SHOWN}


def rate_task(store: Store, R: dict, rater: str) -> Path:
    props = propositions(store, R["round"])
    m = mask(R, props)
    items = [{"key": k, **public(props[v]["content"])} for k, v in m.items()]
    return _write(store, R, "ref_rate", f"rate:{short(rater)}", f"rate-{short(rater)}", rate_instructions(R["cfg"]),
                  [{"propositions": items}], {"mask": m, "rater": rater, "records": len(items)}, rater)


def judge_task(store: Store, R: dict, judge: str, ids: list[int], need: str) -> Path:
    cfg = R["cfg"]
    props = propositions(store, R["round"])
    m = mask(R, props)
    plist = [{"key": k, **public(props[v]["content"])} for k, v in m.items()]
    items = _test_items(store, R, evolution._order(ids, R["round"], "judge", judge))
    size = int(cfg["agents"].get("findings_per_batch", 10))
    batches = [{"findings": items[i:i + size]} for i in range(0, len(items), size)]
    return _write(store, R, "ref_judge", need, need.replace(":", "-"), judge_instructions(cfg, len(plist)), batches,
                  {"mask": m, "judge": judge, "findings": sorted(ids), "records": len(ids)}, judge,
                  {"propositions.json": json.dumps({"propositions": plist}, ensure_ascii=False, indent=1)})


def _end(published: str | None) -> str:
    """The last day a study can have appeared: its date, or the end of its year."""
    p = published or ""
    return p if len(p) == 10 else f"{p}-12-31" if p else ""


def control_works(store: Store, R: dict, n: int) -> list[int]:
    """Training studies from the year before the models' cutoff: what the probe finds for studies the model may have
    read, and what their titles give away."""
    cfg, rid = R["cfg"], R["round"]
    cut = cfg["split"]["model_cutoff"]
    since = f"{int(cut[:4]) - 1:04d}{cut[4:]}"
    with_f = {r["work_id"] for r in tables.finding_rows(store, R["run_id"])}
    train = {w: s for w, s in studies(store, rid).items() if s["split"] == "training" and w in with_f}
    pool = [w for w, s in train.items() if _end(s["published"]) >= since]
    if len(pool) < n:
        pool = sorted(train, key=lambda w: _end(train[w]["published"]), reverse=True)[:max(n, len(pool))]
    return sorted(evolution._order(pool, rid, cfg["seed"], "control")[:n])


def probe_task(store: Store, R: dict, wave: int) -> Path:
    cfg, rid = R["cfg"], R["round"]
    with_f = {r["work_id"] for r in tables.finding_rows(store, R["run_id"])}
    test = sorted(w for w, s in studies(store, rid).items() if s["split"] == "test" and s["wave"] == wave and w in with_f)
    control = control_works(store, R, int((cfg.get("probe") or {}).get("control", 20))) if wave == 0 else []
    every = evolution._order(test + control, rid, cfg["seed"], "probe", wave)
    meta = tables.works(store, every)
    keys = {f"S{n}": w for n, w in enumerate(every, 1)}
    items = []
    for k, w in keys.items():
        m, names = meta[w], writer._names(meta[w]["authors"])
        items.append({"key": k, "title": m["title"], "authors": ", ".join(names[:3]) + (" et al." if len(names) > 3 else ""),
                      "year": m["year"], "venue": m["venue"] or "not recorded"})
    return _write(store, R, "ref_probe", f"probe:w{wave}", f"probe-w{wave}", probe_instructions(cfg), [{"studies": items}],
                  {"keys": keys, "test": test, "control": control, "wave": wave, "records": len(items)},
                  cfg["agents"]["probe"])


def check_task(store: Store, R: dict, wave: int) -> Path:
    cfg, rid = R["cfg"], R["round"]
    folder = R["root"] / tasks(store, rid)[f"probe:w{wave}"]["folder"]
    keys = json.loads((folder / "task.json").read_text(encoding="utf-8"))["keys"]
    recalled = {w: c["recalled"] for w, c in _latest_probe(store, rid).items()}
    rows: dict[int, list] = {}
    for r in tables.finding_rows(store, R["run_id"]):
        rows.setdefault(r["work_id"], []).append(r)
    meta = tables.works(store, keys.values())
    items = [{"key": k, "title": meta[w]["title"], "recalled": recalled.get(w, []),
              "findings": [{"id": r["finding_id"], "finding": r["finding"], **({"effect": r["effect"]} if r["effect"] else {})}
                           for r in sorted(rows.get(w, []), key=lambda r: r["finding_id"])]}
             for k, w in keys.items()]
    return _write(store, R, "ref_check", f"check:w{wave}", f"check-w{wave}", check_instructions(), [{"studies": items}],
                  {"keys": keys, "wave": wave, "records": len(items)}, cfg["agents"]["check"])


# Freezing

def shuffled(cfg: dict, round_id: str, props: dict) -> dict[str, dict]:
    """Per replicate, as many propositions as a source writes, each with the population, contrast, outcome,
    moderators and confidence of a different real proposition (the lenses' and the no-lens control's) and a random
    direction, phrased by a template."""
    donors = set(lens_ids(cfg)) | {"no_lens"}
    out = {}
    for rep in range(1, reps(cfg) + 1):
        mine = [p for _, p in sorted(props.items()) if p["replicate"] == rep and p["source"] in donors]
        pool = [p["content"] for p in mine]
        if len(pool) < 2:
            continue
        rng = evolution._rng(round_id, cfg["seed"], "shuffled", rep)
        # as many as a source writes; papers state different numbers of claims, so as many as one states on average
        n = round(len(pool) / len({p["source"] for p in mine})) if claims(cfg) else per_source(cfg)
        for i in range(1, n + 1):
            pop, con, outcome, mods, conf = (rng.choice(pool)[k] for k in ("population", "contrast", "outcome",
                                                                          "moderators", "confidence"))
            out[f"shuffled.r{rep}.S{i}"] = {**_template(pop, con, outcome, rng.choice(DIRECTIONS[:3])),
                                            "moderators": mods, "confidence": conf, "basis": []}
    return out


def _template(population: str, contrast: str, outcome: str, direction: str) -> dict:
    parts = re.split(r"\s+(?:versus|vs\.?)\s+", contrast, maxsplit=1)
    x, y = parts if len(parts) == 2 else (contrast, "the alternative")
    phrase = {"higher": f"is higher under {x} than under {y}", "lower": f"is lower under {x} than under {y}",
              "no_difference": f"does not differ between {x} and {y}"}[direction]
    return {"claim": f"Among {population}, {outcome} {phrase}.", "population": population, "contrast": contrast,
            "outcome": outcome, "direction": direction, "confirms": f"A study of {population} finds that {outcome} {phrase}.",
            "contradicts": (f"A study of {population} finds a reliable difference in {outcome} between {x} and {y}."
                            if direction == "no_difference" else
                            f"A study of {population} finds {outcome} no different, or the other way round.")}


def freeze(store: Store, R: dict) -> str:
    """Adds the shuffled control, then fixes every proposition: its sha256 and the time go into the store and the
    event log, and a copy of the set into the run's folder. After this no proposition can be added or changed."""
    cfg, rid = R["cfg"], R["round"]
    at = now()
    if shuffled_on(cfg):
        with store.db:
            for k, c in shuffled(cfg, rid, propositions(store, rid)).items():
                text = json.dumps(c, ensure_ascii=False, sort_keys=True)
                store.db.execute("INSERT OR REPLACE INTO ref_propositions VALUES (?, ?, 'shuffled', ?, ?, ?, NULL, ?)",
                                 (rid, k, int(k.split(".")[1][1:]), text, evolution._sha(text), at))
    props = propositions(store, rid)
    sha = digest(props)
    with store.db:
        store.db.execute("UPDATE ref_rounds SET frozen_at = ?, frozen_sha256 = ? WHERE round = ?", (at, sha, rid))
        _event(store, rid, "frozen", {"sha256": sha, "at": at,
                                      "propositions": dict(Counter(p["source"] for p in props.values()))})
    out = R["root"] / "output" / f"corpus_{R['corpus']}" / f"run_{R['run_id']:03d}" / f"referee_{rid}_frozen.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"round": rid, "frozen_at": at, "sha256": sha, "config_sha256": R["sha"],
                               "propositions": {k: v["content"] for k, v in sorted(props.items())}},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    return sha


# The schedule

def judged(store: Store, round_id: str, judge: str) -> set[int]:
    return {r[0] for r in store.db.execute("SELECT DISTINCT finding_id FROM ref_labels WHERE round = ? AND kind = 'judge' "
                                           "AND label = 'judged' AND agent = ?", (round_id, judge))}


def _open_findings(store: Store, R: dict, judge: str) -> set[int]:
    out = set()
    for t in tasks(store, R["round"]).values():
        folder = R["root"] / t["folder"]
        if t["kind"] == "ref_judge" and t["status"] == "open" and (folder / "task.json").exists():
            meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
            if meta["judge"] == judge:
                out |= set(meta["findings"])
    return out


def step(store: Store, run_id: int, root: Path = ROOT, start: bool = False) -> dict:
    """Write every task the state allows that is not written yet, and say what is open. The order is fixed: the
    generators first; the freeze once every proposition is in; then the probe, the raters and the judges."""
    R = round_(store, run_id, root, start)
    cfg, rid = R["cfg"], R["round"]
    written: list[Path] = []

    def need(key: str, make) -> None:
        t = tasks(store, rid).get(key)
        if t and (t["status"] == "imported" or (root / t["folder"]).exists()):
            return
        written.append(make())

    if not R["row"]["frozen_at"]:
        gens = [(s, r) for s in sources(cfg) for r in range(1, reps(cfg) + 1)]
        make = claims_task if claims(cfg) else generate_task
        for s, r in gens:
            need(f"generate:{s}:{r}", lambda s=s, r=r: make(store, R, s, r))
        have = tasks(store, rid)
        if not all((have.get(f"generate:{s}:{r}") or {}).get("status") == "imported" for s, r in gens):
            return _status(store, R, written)
        freeze(store, R)
        R = round_(store, run_id, root)
    update_waves(store, R)
    if not claims(cfg):         # a claims round compares no lenses and its later studies cannot have leaked
        for rater in cfg["agents"]["judges"]:
            need(f"rate:{short(rater)}", lambda rater=rater: rate_task(store, R, rater))
        with_f = {r["work_id"] for r in tables.finding_rows(store, run_id)}
        waves = sorted({s["wave"] for w, s in studies(store, rid).items() if s["split"] == "test" and w in with_f} | {0})
        for wave in waves:
            need(f"probe:w{wave}", lambda wave=wave: probe_task(store, R, wave))
            if (tasks(store, rid).get(f"probe:w{wave}") or {}).get("status") == "imported":
                need(f"check:w{wave}", lambda wave=wave: check_task(store, R, wave))
    group = int(cfg["agents"].get("findings_per_batch", 10)) * int(cfg["agents"].get("batches_per_task", 3))
    test = split_findings(store, R, "test")
    for judge in cfg["agents"]["judges"]:
        pending = sorted(set(test) - judged(store, rid, judge) - _open_findings(store, R, judge))
        order = evolution._order(pending, rid, "judge", judge)
        for i in range(0, len(order), group):
            chunk = sorted(order[i:i + group])
            key = f"judge:{short(judge)}:{evolution._sha(json.dumps(chunk))[:8]}"
            need(key, lambda judge=judge, chunk=chunk, key=key: judge_task(store, R, judge, chunk, key))
    return _status(store, R, written)


def _status(store: Store, R: dict, written: list[Path]) -> dict:
    rid = R["round"]
    t = tasks(store, rid)
    row = store.db.execute("SELECT frozen_at, frozen_sha256 FROM ref_rounds WHERE round = ?", (rid,)).fetchone()
    test = split_findings(store, R, "test")
    pending = {j: len(set(test) - judged(store, rid, j)) for j in R["cfg"]["agents"]["judges"]}
    is_open = sorted(v["folder"] for v in t.values() if v["status"] == "open")
    return {"round": rid, "written": written, "open": is_open, "imported": sum(v["status"] == "imported" for v in t.values()),
            "frozen_at": row["frozen_at"], "frozen_sha256": row["frozen_sha256"], "unjudged": pending,
            "done": bool(row["frozen_at"]) and not is_open and not any(pending.values())}


# Instructions

OUTPUT_GENERATE = {"propositions": [{
    "id": "R1", "claim": "one sentence, at most 40 words", "population": "who, in which setting",
    "contrast": "X versus Y", "outcome": "what is measured", "direction": "higher | lower | no_difference | depends",
    "moderators": [{"condition": "a condition that changes the effect", "effect": "stronger | weaker | reversed | absent"}],
    "confidence": 0.6, "confirms": "the result that would confirm it", "contradicts": "the result that would contradict it",
    "basis": [12, 40]}]}


def _field_lines(whose: str) -> list[str]:
    return [
        "- id: R1, R2, ...",
        "- claim: one sentence, at most 40 words.",
        "- population: who, in which setting (for example \"medical students in their pre-clinical years\").",
        "- contrast: what is compared, as \"X versus Y\" (for example \"pass/fail grading versus tiered grades\").",
        "- outcome: what is measured.",
        "- direction: higher (the outcome is higher under X than under Y), lower, no_difference, or depends (then the "
        "moderators say on what).",
        "- moderators: up to 3 conditions that change the effect, each with `condition` and `effect` (stronger, weaker, "
        "reversed or absent). The list may be empty, except for depends.",
        f"- confidence: {whose}, from 0.01 to 0.99, that the new studies which test the proposition will confirm it.",
        "- confirms and contradicts: the results that would confirm it and contradict it.",
        "- basis: ids of findings in evidence.jsonl it builds on (it may be empty)."]


def claims_instructions(cfg: dict, src: dict, n: int) -> str:
    out = dict(OUTPUT_GENERATE["propositions"][0])
    out["where"] = "the section (and table row) it comes from, and its gist, at most 25 words"
    return "\n".join([
        "# A paper's claims, as propositions later studies will confirm or contradict", "",
        f"`manuscript.md` in this folder is a finished paper on {cfg['domain']} ({src['name']}). It was written from the "
        f"studies a review had found; `evidence.jsonl` holds their {n} findings, one per line. Studies published after "
        "the paper will be used to check what it claims. This task writes its claims down before any such study is "
        "seen, so that the check is fair.", "",
        "## The task", "",
        f"State the paper's claims about the world as {MIN_CLAIMS} to {per_source(cfg)} propositions, the most important "
        "first: its headline conclusions, the main findings later studies could test, and, where the paper proposes a "
        "design, the effect it expects each component to have on performance, learning and well-being. Restate: add no "
        "claim the paper does not make, and do not drop its boldest ones.", "",
        "- Translate faithfully. Where the paper says an effect has not been shown, write what it expects the next "
        "studies to find, with the confidence its wording implies; do not harden \"not shown\" into \"no effect\" "
        "with high confidence, and do not soften a firm claim.",
        "- Only claims a later study could confirm or contradict. Statements about the review's own evidence base (how "
        "many studies were randomised, what was measured) are not propositions.",
        "- Each proposition must be falsifiable: a single new study could confirm it or contradict it.", "",
        "For each proposition:", *_field_lines("the probability the paper's wording and evidence imply"),
        "- where: the section (and table row) of the paper the claim comes from, and its gist in at most 25 words.", "",
        "How they are scored: a judge who does not know which paper wrote what reads each later finding against every "
        "proposition and says whether it supports it, contradicts it or does not address it. Each claim is then shown "
        "with the later studies for and against it.", "",
        "Rules:",
        _rules("manuscript.md, batch_01.json and evidence.jsonl") + " Read all of manuscript.md, in parts if it is long.",
        "- Propositions are general: no study names, authors, years or finding numbers in their text (basis is where "
        "findings go; `where` may name a section or table).",
        "- No two propositions with the same population, contrast, outcome and direction.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"propositions": [out]}, ensure_ascii=False, indent=1), "```"]) + "\n"


def _rules(files: str) -> str:
    return (f"- Use only {files} in this folder. Open no other file of the project (not task.json, no other folder, "
            "report, manuscript or database) and do not search the web.")


def generate_instructions(cfg: dict, persp: dict | None, n: int) -> str:
    T = cfg["split"]["T"]
    head = [f"## Your perspective: {persp['name']}", "", persp["stance"], "",
            "Read the evidence through this perspective, as a committed proponent of it would.", ""] if persp else []
    return "\n".join([
        "# Propositions that later studies will confirm or contradict", "",
        f"A review collects studies of {cfg['domain']}. `evidence.jsonl` in this folder holds the {n} findings of the "
        f"studies first published before {T}, one per line: the study, its year and design, the finding and its coded "
        f"fields. The studies first published from {T} on are hidden; they will be used to check what you write here.", "",
        *head,
        "## The task", "",
        f"Write exactly {per_source(cfg)} propositions about what studies first published from {T} on will find. Each "
        "must be falsifiable: a single new study could confirm it or contradict it.", "",
        "For each proposition:", *_field_lines("your probability"), "",
        "How the propositions are scored: a judge who does not know who wrote what reads each new finding against every "
        "proposition and says whether it supports it, contradicts it or does not address it. Each support counts +1 "
        "and each contradiction -1, weighted by how much the proposition commits to: a direction counts more than "
        "depends, each moderator with its own effect adds, and a proposition that any researcher in the field would "
        "predict anyway counts less. Every source writes the same number of propositions. The confidences are scored "
        "against what the new studies show (Brier score), so be calibrated.", "",
        "Rules:",
        _rules("batch_01.json and evidence.jsonl") + " The hidden studies are elsewhere in the project, and reading "
        "them would void the test. Read all of evidence.jsonl, in parts if it is long.",
        "- Propositions are general: no study names, authors, years or finding numbers in their text (basis is where "
        "findings go).",
        "- No two propositions with the same population, contrast, outcome and direction.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps(OUTPUT_GENERATE, ensure_ascii=False, indent=1), "```"]) + "\n"


def probe_instructions(cfg: dict) -> str:
    return "\n".join([
        "# What did these studies find?", "",
        f"Each item in `studies` (batch_01.json) is a study of {cfg['domain']}: its title, authors, year and venue. For "
        "each, say what it found, as specifically as you can: the direction of each main result, and for whom.", "",
        "- known: true only if you remember this specific study and its results; false if you are inferring from the "
        "title or from the field.",
        "- recalled: 1 to 3 short statements of what it found, at most 30 words each: from memory where you have it, "
        "otherwise your best guess from the title and the field. Always give at least one.", "",
        "Rules:", _rules("batch_01.json"), "- Every study exactly once.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"studies": [{"key": "S1", "known": False, "recalled": ["..."]}]}, indent=1), "```"]) + "\n"


def check_instructions() -> str:
    return "\n".join([
        "# Did the answer reproduce the study's findings?", "",
        "Another agent saw only the title, authors, year and venue of each study in `studies` (batch_01.json) and said "
        "what it found (`recalled`). `findings` are what the study really reports. For each study:", "",
        "- title_states: true if the title alone states the result of at least one of its findings (its direction), "
        "false otherwise;",
        "- findings: for every finding, reproduced: yes (a recalled statement gives this finding's result: the same "
        "outcome and direction, for the same group), partly (the right outcome, but vaguer, or only part of the "
        "result), or no.", "",
        "Judge the content, not the wording.", "",
        "Rules:", _rules("batch_01.json"), "- Every study and every finding exactly once.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"studies": [{"key": "S1", "title_states": False,
                                 "findings": [{"finding": 12, "reproduced": "yes | partly | no"}]}]}, indent=1),
        "```"]) + "\n"


def rate_instructions(cfg: dict) -> str:
    return "\n".join([
        "# Which propositions are obvious?", "",
        f"`propositions` (batch_01.json) are predictions about what new studies of {cfg['domain']} will find, written by "
        "several sources whose names are hidden, in random order. For each, decide:", "",
        "- obvious: true if a knowledgeable researcher in this field would predict it without reading any particular "
        "evidence (textbook knowledge or common sense, such as 'support helps', 'people differ', 'stress harms "
        "performance'), or if it only restates the field's best-known result; false if it says something such a "
        "researcher would not have predicted as such (a specific condition, threshold, reversal or outcome);",
        "- reason: at most 20 words.", "",
        "Judge the content, not the wording or how confident it sounds.", "",
        "Rules:", _rules("batch_01.json"), "- Every proposition exactly once.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"labels": [{"key": "P001", "obvious": False, "reason": "..."}]}, indent=1), "```"]) + "\n"


def judge_instructions(cfg: dict, n: int) -> str:
    return "\n".join([
        "# Which propositions does each new finding support or contradict?", "",
        f"Predictions about {cfg['domain']} were written down before the studies in the batch files were published: "
        f"`propositions.json` holds {n} of them, from several sources whose names are hidden, in random order. Each "
        "item of `findings` in batch_NN.json is one finding of such a study, with its verbatim passage and coded fields. "
        "For every finding, say which propositions it supports and which it contradicts, and leave out every "
        "proposition it does not address.", "",
        "- A finding addresses a proposition when it reports a result on the proposition's outcome (or a close measure "
        "of it), for its contrast (or a clear instance of it), in a population the proposition covers. Sharing the "
        "topic is not enough.",
        "- supports: the result goes the way the proposition says (its direction, and its moderators where the finding "
        "reports on them); it meets the proposition's `confirms`.",
        "- contradicts: the result goes against it: the opposite direction, no difference where it predicts one, a "
        "difference where it predicts none, a moderator working the other way, or what its `contradicts` describes.",
        "- When a finding speaks to only part of a proposition (one moderator, one group), judge that part.",
        "- why: at most 25 words, naming what in the finding decides it.", "",
        "Be strict and consistent, with the same reading for every proposition. Judge the content, not the wording, and "
        "not how confident or bold a proposition sounds.", "",
        "Rules:", _rules("propositions.json and the batch files"),
        "- Every finding exactly once, in the result file of its batch; each proposition at most once per finding.", "",
        "Output: for each batch_NN.json, one JSON object in batch_NN_result.json:", "", "```json",
        json.dumps({"findings": [{"finding": 12, "addressed": [{"key": "P017", "verdict": "supports | contradicts",
                                                                "why": "..."}]},
                                 {"finding": 13, "addressed": []}]}, indent=1), "```"]) + "\n"


# Checking and storing answers

def _norm(s) -> str:
    return " ".join(str(s or "").split()).casefold()


def _words(s) -> int:
    return len(str(s or "").split())


def check_generate(meta: dict, answer, cfg: dict) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("propositions"), list):
        return ["the answer needs a `propositions` list"]
    props, errs = answer["propositions"], []
    if claims(cfg):
        if not MIN_CLAIMS <= len(props) <= per_source(cfg):
            errs.append(f"{MIN_CLAIMS} to {per_source(cfg)} propositions, not {len(props)}")
    elif len(props) != per_source(cfg):
        errs.append(f"exactly {per_source(cfg)} propositions, not {len(props)}")
    ids = [p.get("id") if isinstance(p, dict) else None for p in props]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and PROP_ID.fullmatch(i) for i in ids):
        errs.append("ids: R1, R2, ..., each once")
    ev, seen = set(meta["evidence_ids"]), set()
    for p in props:
        if not isinstance(p, dict):
            errs.append("each proposition is an object")
            continue
        where = f"proposition {p.get('id')}"
        for k, limit in WORDS.items():
            v = p.get(k)
            if not isinstance(v, str) or not v.strip():
                errs.append(f"{where}: `{k}` is empty")
                continue
            if _words(v) > limit + 5:
                errs.append(f"{where}: `{k}` has more than {limit} words")
            if m := evolution.CITE.search(v):
                errs.append(f"{where}: `{k}` names a study, year or finding ({m.group(0)!r}); findings go in basis")
        if p.get("direction") not in DIRECTIONS:
            errs.append(f"{where}: direction is one of {', '.join(DIRECTIONS)}")
        mods = p.get("moderators", [])
        if not isinstance(mods, list) or len(mods) > 3 or any(
                not isinstance(x, dict) or not str(x.get("condition") or "").strip() or x.get("effect") not in EFFECTS
                for x in mods):
            errs.append(f"{where}: moderators: up to 3, each with a condition and an effect ({', '.join(EFFECTS)})")
            mods = []
        for x in mods:
            if m := evolution.CITE.search(str(x["condition"])):
                errs.append(f"{where}: a moderator names a study, year or finding ({m.group(0)!r})")
        if p.get("direction") == "depends" and not mods:
            errs.append(f"{where}: depends needs at least one moderator")
        c = p.get("confidence")
        if not isinstance(c, (int, float)) or isinstance(c, bool) or not 0.01 <= c <= 0.99:
            errs.append(f"{where}: confidence is a number from 0.01 to 0.99")
        basis = p.get("basis", [])
        if not isinstance(basis, list) or any(b not in ev for b in basis):
            errs.append(f"{where}: basis holds ids of findings in evidence.jsonl")
        if claims(cfg) and (not str(p.get("where") or "").strip() or _words(p.get("where")) > 35):
            errs.append(f"{where}: `where` gives the section the claim comes from and its gist, at most 25 words")
        key = tuple(_norm(p.get(k)) for k in ("population", "contrast", "outcome", "direction"))
        if key in seen:
            errs.append(f"{where}: the same population, contrast, outcome and direction as another proposition")
        seen.add(key)
    return errs


def _clean(p: dict) -> dict:
    ws = lambda s: " ".join(str(s).split())  # noqa: E731
    return {**{k: ws(p[k]) for k in WORDS}, "direction": p["direction"],
            "moderators": [{"condition": ws(x["condition"]), "effect": x["effect"]} for x in p.get("moderators") or []],
            "confidence": float(p["confidence"]), "basis": sorted(set(p.get("basis") or [])),
            **({"where": ws(p["where"])} if p.get("where") else {})}          # a paper's claim: where it says so


def _every_once(got: list, want: set, what: str) -> list[str]:
    c = Counter(got)
    if set(c) == want and all(v == 1 for v in c.values()):
        return []
    return [f"every {what} exactly once: missing {sorted(want - set(c), key=str)}, unknown "
            f"{sorted((x for x in c if x not in want), key=str)}, twice {sorted((x for x, v in c.items() if v > 1), key=str)}"]


def check_probe(data: dict, answer) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("studies"), list):
        return ["the answer needs a `studies` list"]
    xs = [x for x in answer["studies"] if isinstance(x, dict)]
    errs = _every_once([x.get("key") for x in xs], {s["key"] for s in data["studies"]}, "study")
    for x in xs:
        rec = x.get("recalled")
        if not isinstance(x.get("known"), bool):
            errs.append(f"{x.get('key')}: known is true or false")
        if not isinstance(rec, list) or not 1 <= len(rec) <= 3 or any(not isinstance(s, str) or not s.strip()
                                                                     or _words(s) > 40 for s in rec):
            errs.append(f"{x.get('key')}: recalled holds 1 to 3 statements of at most 30 words")
    return errs


def check_check(data: dict, answer) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("studies"), list):
        return ["the answer needs a `studies` list"]
    want = {s["key"]: {f["id"] for f in s["findings"]} for s in data["studies"]}
    xs = [x for x in answer["studies"] if isinstance(x, dict)]
    errs = _every_once([x.get("key") for x in xs], set(want), "study")
    for x in xs:
        if x.get("key") not in want:
            continue
        if not isinstance(x.get("title_states"), bool):
            errs.append(f"{x['key']}: title_states is true or false")
        fs = [f for f in x.get("findings") or [] if isinstance(f, dict)]
        errs += [f"{x['key']}: {e}" for e in _every_once([f.get("finding") for f in fs], want[x["key"]], "finding")]
        errs += [f"{x['key']}, finding {f.get('finding')}: reproduced is yes, partly or no" for f in fs
                 if f.get("reproduced") not in REPRODUCED]
    return errs


def check_rate(meta: dict, answer) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("labels"), list):
        return ["the answer needs a `labels` list"]
    xs = [x for x in answer["labels"] if isinstance(x, dict)]
    errs = _every_once([x.get("key") for x in xs], set(meta["mask"]), "proposition")
    return errs + [f"{x.get('key')}: obvious is true or false" for x in xs if not isinstance(x.get("obvious"), bool)]


def check_judge(meta: dict, data: dict, answer) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("findings"), list):
        return ["the answer needs a `findings` list"]
    xs = [x for x in answer["findings"] if isinstance(x, dict)]
    errs = _every_once([x.get("finding") for x in xs], {f["id"] for f in data["findings"]}, "finding")
    for x in xs:
        add = x.get("addressed")
        if not isinstance(add, list):
            errs.append(f"finding {x.get('finding')}: addressed is a list (it may be empty)")
            continue
        keys = [a.get("key") for a in add if isinstance(a, dict)]
        if len(keys) != len(add) or len(set(keys)) != len(keys) or any(k not in meta["mask"] for k in keys):
            errs.append(f"finding {x.get('finding')}: addressed holds propositions of propositions.json, each once")
        errs += [f"finding {x.get('finding')}, {a.get('key')}: verdict is supports or contradicts" for a in add
                 if isinstance(a, dict) and a.get("verdict") not in VERDICTS]
        errs += [f"finding {x.get('finding')}, {a.get('key')}: why has more than 25 words" for a in add
                 if isinstance(a, dict) and _words(a.get("why")) > 40]
    return errs


def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions: str) -> dict:
    from .agents import _strip_fences, batch_files
    ensure(store)
    row = store.db.execute("SELECT * FROM ref_rounds WHERE round = ?", (meta["round"],)).fetchone()
    if row is None or row["config_sha256"] != meta["config_sha256"]:
        return {"stored": 0, "problems": ["this task belongs to a round whose pre-registered file is not this one"]}
    cfg, kind, rid = tomllib.loads(row["config"]), meta["kind"], meta["round"]
    if kind == "ref_generate" and row["frozen_at"]:
        return {"stored": 0, "problems": [f"the propositions were frozen at {row['frozen_at']}: none can be added now"]}
    if kind != "ref_generate" and not row["frozen_at"]:
        return {"stored": 0, "problems": ["the propositions are not frozen yet"]}
    if kind == "ref_generate":
        ev = folder / "evidence.jsonl"
        if not ev.exists() or evolution._sha(ev.read_text(encoding="utf-8")) != meta["evidence_sha256"]:
            return {"stored": 0, "problems": ["evidence.jsonl is missing or changed after the task was written"]}
        ms = folder / "manuscript.md"
        if meta.get("manuscript_sha256") and (not ms.exists() or
                                              evolution._sha(ms.read_text(encoding="utf-8")) != meta["manuscript_sha256"]):
            return {"stored": 0, "problems": ["manuscript.md is missing or changed after the task was written"]}
    answers, problems, raws = [], [], []
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
        errs = (check_generate(meta, answer, cfg) if kind == "ref_generate" else
                check_probe(data, answer) if kind == "ref_probe" else
                check_check(data, answer) if kind == "ref_check" else
                check_rate(meta, answer) if kind == "ref_rate" else check_judge(meta, data, answer))
        problems += [f"{result.name}: {e}" for e in errs]
        answers.append((batch, data, answer))
        raws.append(raw)
    if problems:
        return {"stored": 0, "problems": problems}
    raw_all = "\n".join(raws)
    shas = ", ".join(evolution._sha(b.read_text(encoding="utf-8")) for b, _, _ in answers)
    extra = f", evidence sha256 {meta['evidence_sha256']}" if kind == "ref_generate" else ""
    call_id = store.log_call(meta["run_id"], "llm", meta["agent"], f"{instructions}\n\n[{kind} {meta['need']}, batches "
                             f"sha256 {shas}{extra}]", 200, 0, raw_all.encode("utf-8"),
                             {"agent": meta["agent"], "task": kind, "round": rid, "need": meta["need"]})
    at, stored = now(), 0

    def label(k, work=None, finding=None, key=None, value="", detail=None):
        store.db.execute("INSERT INTO ref_labels (round, kind, agent, work_id, finding_id, key, label, detail, call_id, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (rid, k, meta["agent"], work, finding, key, value,
                          json.dumps(detail, ensure_ascii=False) if detail is not None else None, call_id, at))

    with store.db:
        if kind == "ref_generate":
            for p in answers[0][2]["propositions"]:
                c = _clean(p)
                text = json.dumps(c, ensure_ascii=False, sort_keys=True)
                store.db.execute("INSERT OR REPLACE INTO ref_propositions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                 (rid, f"{meta['source']}.r{meta['replicate']}.{p['id']}", meta["source"],
                                  meta["replicate"], text, evolution._sha(text), call_id, at))
                stored += 1
        elif kind == "ref_probe":
            test = set(meta["test"])
            for x in answers[0][2]["studies"]:
                w = meta["keys"][x["key"]]
                label("probe", work=w, value="known" if x["known"] else "unknown",
                      detail={"recalled": [" ".join(s.split()) for s in x["recalled"]],
                              "group": "test" if w in test else "control", "wave": meta["wave"]})
                stored += 1
        elif kind == "ref_check":
            for x in answers[0][2]["studies"]:
                w = meta["keys"][x["key"]]
                for f in x["findings"]:
                    label("check", work=w, finding=f["finding"], value=f["reproduced"],
                          detail={"title_states": x["title_states"]})
                    stored += 1
        elif kind == "ref_rate":
            for x in answers[0][2]["labels"]:
                label("rate", key=meta["mask"][x["key"]], value="obvious" if x["obvious"] else "not_obvious",
                      detail={"reason": x.get("reason")})
                stored += 1
        else:
            works = {r["finding_id"]: r["work_id"] for r in tables.finding_rows(store, meta["run_id"])}
            for _, _, answer in answers:
                for x in answer["findings"]:
                    f = x["finding"]
                    label("judge", work=works.get(f), finding=f, value="judged", detail={"addressed": len(x["addressed"])})
                    for a in x["addressed"]:
                        label("judge", work=works.get(f), finding=f, key=meta["mask"][a["key"]], value=a["verdict"],
                              detail={"why": a.get("why")})
                    stored += 1
        store.db.execute("UPDATE ref_tasks SET status = 'imported' WHERE round = ? AND need = ?", (rid, meta["need"]))
    return {"stored": stored, "problems": []}


# Evaluation: code counts, no agent scores

def spec_weight(content: dict, cfg: dict) -> float:
    """What a proposition commits to, from 0 to 1: its direction's weight, raised by each moderator with its own effect
    (up to max_moderators) and scaled so that a direction with the most moderators counts 1."""
    s = cfg.get("score") or {}
    w = {**WEIGHTS, **(s.get("direction") or {})}.get(content["direction"], 0.0)
    bonus, most = float(s.get("moderator_bonus", 0.25)), int(s.get("max_moderators", 2))
    m = sum(1 for x in content.get("moderators") or [] if x.get("effect") in EFFECTS)
    return w * (1 + bonus * min(m, most)) / (1 + bonus * most)


def _latest(rows, key) -> dict:
    """The rows of the latest call for each key: a task answered again replaces the earlier answer."""
    last: dict = {}
    for r in rows:
        k = key(r)
        if k not in last or r["call_id"] > last[k][0]:
            last[k] = (r["call_id"], [])
        if r["call_id"] == last[k][0]:
            last[k][1].append(r)
    return {k: v[1] for k, v in last.items()}


def _labels(store: Store, round_id: str, kind: str):
    return store.db.execute("SELECT * FROM ref_labels WHERE round = ? AND kind = ? ORDER BY label_id",
                            (round_id, kind)).fetchall()


def _latest_probe(store: Store, round_id: str) -> dict[int, dict]:
    out = {}
    for w, rs in _latest(_labels(store, round_id, "probe"), lambda r: r["work_id"]).items():
        d = json.loads(rs[0]["detail"])
        out[w] = {"known": rs[0]["label"] == "known", "recalled": d["recalled"], "group": d["group"], "wave": d["wave"]}
    return out


def leaked(known: bool, title_states: bool, reproduced: dict) -> bool:
    """Likely remembered: the model says it knows the study and gets a finding at least partly right, or it gets a
    finding right that the title does not give away."""
    vals = set(reproduced.values())
    return bool((known and vals & {"yes", "partly"}) or ("yes" in vals and not title_states))


def contamination(store: Store, round_id: str) -> dict[int, dict]:
    probe = _latest_probe(store, round_id)
    check = _latest(_labels(store, round_id, "check"), lambda r: r["work_id"])
    out = {}
    for w, p in probe.items():
        rs = check.get(w) or []
        rep = {r["finding_id"]: r["label"] for r in rs}
        title = any(json.loads(r["detail"])["title_states"] for r in rs)
        out[w] = {**p, "checked": bool(rs), "title_states": title, "reproduced": rep,
                  "flagged": bool(rs) and leaked(p["known"], title, rep)}
    return out


def _verdicts(store: Store, round_id: str) -> tuple[dict, dict]:
    """Per judge: the findings it judged and its verdict on every proposition a finding addressed (+1 or -1), from its
    latest answer for each finding; and each verdict's reason."""
    out: dict[str, dict] = {}
    why = {}
    for (judge, f), rs in _latest(_labels(store, round_id, "judge"), lambda r: (r["agent"], r["finding_id"])).items():
        j = out.setdefault(judge, {"judged": set(), "pairs": {}})
        j["judged"].add(f)
        for r in rs:
            if r["label"] in VERDICTS:
                j["pairs"][(r["key"], f)] = VERDICTS[r["label"]]
                why[(judge, r["key"], f)] = (json.loads(r["detail"] or "{}")).get("why")
    return out, why


def _obvious(store: Store, round_id: str, raters: list[str]) -> tuple[dict, dict]:
    """Per proposition: obvious only if every rater rated it and called it obvious; and each rater's labels."""
    by = {}
    for (agent, key), rs in _latest(_labels(store, round_id, "rate"), lambda r: (r["agent"], r["key"])).items():
        by.setdefault(agent, {})[key] = rs[0]["label"] == "obvious"
    keys = set().union(*(set(v) for v in by.values())) if by else set()
    return {k: all(by.get(a, {}).get(k) is True for a in raters) for k in keys}, by


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _perm_p(u: dict, a: list[list[str]], b: list[list[str]], n: int, seed: str) -> float | None:
    """Two-sided permutation p for score(a) - score(b): within each replicate the two sources' propositions are pooled
    and dealt out again at random, n times. A source's score is the mean over replicates of its propositions' mean u."""
    pools = [(x + y, len(x)) for x, y in zip(a, b) if x and y]
    if not pools:
        return None
    stat = lambda parts: _mean(_mean(u[k] for k in ks[:m]) - _mean(u[k] for k in ks[m:]) for ks, m in parts)  # noqa: E731
    obs = stat(pools)
    rng = random.Random(seed)
    hits = 0
    for _ in range(n):
        drawn = []
        for ks, m in pools:
            ks = list(ks)
            rng.shuffle(ks)
            drawn.append((ks, m))
        hits += abs(stat(drawn)) >= abs(obs) - 1e-12
    return (hits + 1) / (n + 1)


def _score_table(cfg: dict, round_id: str, props: dict, weight: dict, obvious: dict, verdict: dict, judges: list[str],
                 F: list[int], study: dict[int, int]) -> dict:
    n_boot, n_perm = int((cfg.get("score") or {}).get("bootstrap", 2000)), int((cfg.get("score") or {}).get("permutations", 5000))
    by: dict[tuple, list[str]] = {}
    for k, p in props.items():
        by.setdefault((p["source"], p["replicate"]), []).append(k)
    v = {(k, f): sum(verdict[j]["pairs"].get((k, f), 0) for j in judges) / len(judges) for k in props for f in F}
    net = {k: sum(v[k, f] for f in F) for k in props}
    u = {k: weight[k] * net[k] for k in props}
    srcs = [s for s in all_sources(cfg) if any(src == s for src, _ in by)]
    rep_keys = {s: [sorted(by.get((s, r), [])) for r in range(1, reps(cfg) + 1)] for s in srcs}
    x = {s: {f: _mean(sum(weight[k] * v[k, f] for k in ks) / len(ks) for ks in rep_keys[s] if ks) for f in F}
         for s in srcs}
    rows = {}
    for s in srcs:
        keys = [k for ks in rep_keys[s] for k in ks]
        rep_scores = [_mean(u[k] for k in ks) for ks in rep_keys[s] if ks]
        m, lo, hi = evolution.cluster_ci(x[s], study, n_boot, f"{round_id}-{s}") if F else (None, None, None)
        per_judge = []
        for j in judges:
            counts = {k: (sum(verdict[j]["pairs"].get((k, f)) == 1 for f in F),
                          sum(verdict[j]["pairs"].get((k, f)) == -1 for f in F)) for k in keys}
            tested = {k: sc for k, sc in counts.items() if sum(sc)}
            per_judge.append({
                "coverage": len(tested) / len(keys) if keys else None,
                "hit": _mean(1.0 if s_ and not c_ else 0.0 for s_, c_ in tested.values()),
                "contra": _mean(1.0 if c_ and not s_ else 0.0 for s_, c_ in tested.values()),
                "brier": _mean((props[k]["content"]["confidence"] - s_ / (s_ + c_)) ** 2 for k, (s_, c_) in tested.items()),
                "tested": len(tested)})
        pairs = [(a, b) for a, b in zip(rep_scores, rep_scores[1:])]
        rows[s] = {"propositions": len(keys), "specificity": _mean(spec_weight(props[k]["content"], cfg) for k in keys),
                   "obvious": _mean(1.0 if obvious.get(k) else 0.0 for k in keys),
                   "confidence": _mean(props[k]["content"]["confidence"] for k in keys),
                   "score": _mean(rep_scores), "replicates": rep_scores,
                   "ci": (None if m is None else m * len(F), None if lo is None else lo * len(F),
                          None if hi is None else hi * len(F)),
                   "noise": _mean(abs(a - b) for a, b in pairs) if pairs else None,
                   **{k: _mean(pj[k] for pj in per_judge) for k in ("coverage", "hit", "contra", "brier", "tested")}}
    comparisons = {}
    for a in [s for s in srcs if s not in ("placebo", "shuffled")]:
        for b in [s for s in ("placebo", "no_lens", "shuffled") if s in srcs and s != a]:
            diffs = {f: x[a][f] - x[b][f] for f in F}
            m, lo, hi = evolution.cluster_ci(diffs, study, n_boot, f"{round_id}-{a}-{b}") if F else (None, None, None)
            comparisons[f"{a}|{b}"] = {"diff": None if m is None else m * len(F), "lo": None if lo is None else lo * len(F),
                                   "hi": None if hi is None else hi * len(F),
                                   "p": _perm_p(u, rep_keys[a], rep_keys[b], n_perm, f"{round_id}-perm-{a}-{b}")}
    return {"findings": len(F), "studies": len({study[f] for f in F}), "sources": rows, "comparisons": comparisons,
            "u": u, "net": net}


def _power(n: int, studies_: int) -> dict:
    if not n or not studies_:
        return {}
    deff = 1 + (n / studies_ - 1) * evolution.PILOT["icc_study"]
    neff = n / deff
    return {str(d): evolution._phi(d * math.sqrt(neff) - 1.959964) for d in (0.2, 0.3, 0.5)} | {"deff": deff}


def evaluate(store: Store, run_id: int, root: Path = ROOT) -> dict:
    R = round_(store, run_id, root)
    cfg, rid, row = R["cfg"], R["round"], R["row"]
    if not row["frozen_at"]:
        raise ValueError("the propositions are not frozen yet: there is nothing to score")
    props = propositions(store, rid)
    judges = list(cfg["agents"]["judges"])
    st = studies(store, rid)
    rows = {r["finding_id"]: r for r in tables.finding_rows(store, run_id)}
    test = {f: r["work_id"] for f, r in rows.items() if (st.get(r["work_id"]) or {}).get("split") == "test"}
    cont = contamination(store, rid)
    flagged = {w for w, c in cont.items() if c["group"] == "test" and c["flagged"]}
    verdict, why = _verdicts(store, rid)
    for j in judges:
        verdict.setdefault(j, {"judged": set(), "pairs": {}})
    complete = sorted(f for f in test if all(f in verdict[j]["judged"] for j in judges))
    exclude = (cfg.get("probe") or {}).get("exclude_flagged", True) is not False
    F = [f for f in complete if not (exclude and test[f] in flagged)]
    obvious, by_rater = _obvious(store, rid, judges)
    mult = float((cfg.get("score") or {}).get("obvious", 0.5))
    weight = {k: spec_weight(p["content"], cfg) * (mult if obvious.get(k) else 1.0) for k, p in props.items()}
    main = _score_table(cfg, rid, props, weight, obvious, verdict, judges, F, test)
    with_flagged = (_score_table(cfg, rid, props, weight, obvious, verdict, judges, complete, test)
                    if exclude and len(complete) != len(F) else None)

    agree = {}
    if len(judges) >= 2:
        a, b = judges[:2]
        lab = lambda j, k, f: {1: "supports", -1: "contradicts"}.get(verdict[j]["pairs"].get((k, f)), "none")  # noqa: E731
        pairs = [(lab(a, k, f), lab(b, k, f)) for f in complete for k in props]
        agree = {"judges": [a, b], "all": lenses._agreement(pairs),
                 "either": lenses._agreement([p for p in pairs if "none" != p[0] or "none" != p[1]]),
                 "both": lenses._agreement([p for p in pairs if "none" not in p])}
        both = [k for k in props if k in by_rater.get(a, {}) and k in by_rater.get(b, {})]
        agree["obvious"] = lenses._agreement([(str(by_rater[a][k]), str(by_rater[b][k])) for k in both])

    meta = tables.works(store, {w for w in st if st[w]["split"] in ("test",)} | set(cont), run_id)
    groups = {}
    for g in ("test", "control"):
        cs = [c for c in cont.values() if c["group"] == g]
        groups[g] = {"studies": len(cs), "checked": sum(c["checked"] for c in cs), "known": sum(c["known"] for c in cs),
                     "title_states": sum(c["title_states"] for c in cs),
                     "reproduced": sum("yes" in c["reproduced"].values() for c in cs),
                     "flagged": sum(c["flagged"] for c in cs)}
    leaks = [{"work_id": w, "cite": meta[w]["cite"], "title": meta[w]["title"], "known": cont[w]["known"],
              "title_states": cont[w]["title_states"], "reproduced": dict(Counter(cont[w]["reproduced"].values()))}
             for w in sorted(flagged)]

    by_split: dict[str, dict] = {}
    for w, s in st.items():
        d = by_split.setdefault(s["split"], {"studies": 0, "findings": 0})
        d["studies"] += 1
        d["findings"] += sum(1 for r in rows.values() if r["work_id"] == w)
    waves = Counter(s["wave"] for w, s in st.items() if s["split"] == "test")
    reasons = Counter(s["reason"] for s in st.values() if s["split"] == "excluded")

    u = main["u"]
    picks = []
    for s in lens_ids(cfg):
        mine = [k for k, p in props.items() if p["source"] == s]
        if mine:
            picks.append(max(mine, key=lambda k: (u[k], k)))
    rest = [k for k in props if k not in picks]
    if rest:
        picks.append(min(rest, key=lambda k: (u[k], k)))
        picks.append(max((k for k in rest if props[k]["source"] in BASELINES), key=lambda k: (u[k], k), default=None))
    examples = []
    for k in [p for p in picks if p][:5]:
        counts = {j: (sum(verdict[j]["pairs"].get((k, f)) == 1 for f in F), sum(verdict[j]["pairs"].get((k, f)) == -1
                                                                                 for f in F)) for j in judges}
        reasons_ = [why.get((j, k, f)) for f in F for j in judges if (k, f) in verdict[j]["pairs"]]
        examples.append({"key": k, "source": props[k]["source"], "content": props[k]["content"], "weight": weight[k],
                         "obvious": bool(obvious.get(k)), "net": main["net"][k], "u": u[k], "counts": counts,
                         "why": next((r for r in reasons_ if r), None)})
    calls = Counter(t["kind"] for t in tasks(store, rid).values() if t["status"] == "imported")
    return {"round": rid, "corpus": R["corpus"], "run": run_id, "cfg": cfg, "config_path": R["path"],
            "config_sha256": R["sha"], "frozen_at": row["frozen_at"], "frozen_sha256": row["frozen_sha256"],
            "intact": digest(props) == row["frozen_sha256"], "propositions": len(props),
            "split": by_split, "waves": dict(sorted(waves.items())), "excluded": dict(reasons), "test_from": test_from(cfg),
            "unjudged": len(test) - len(complete), "flagged_findings": len(complete) - len(F) if exclude else 0,
            "main": main, "with_flagged": with_flagged, "agreement": agree, "contamination": groups, "leaks": leaks,
            "examples": examples, "power": _power(main["findings"], main["studies"]), "judges": judges,
            "calls": dict(calls), "claims": claims_summary(store, run_id, root) if claims(cfg) else None}


def claims_summary(store: Store, run_id: int, root: Path = ROOT) -> dict | None:
    """A claims round as Home shows it: each paper's frozen claims and what the later studies say of each. A finding
    counts for a claim, or against it, where the judges' verdicts averaged lean that way; a finding counts only once
    every judge has read it. None where the run has no claims round."""
    try:
        R = round_(store, run_id, root)
    except (ValueError, OSError):
        return None
    cfg, rid, row = R["cfg"], R["round"], R["row"]
    if not claims(cfg):
        return None
    judges = list(cfg["agents"]["judges"])
    st = studies(store, rid)
    test = {r["finding_id"]: r["work_id"] for r in tables.finding_rows(store, run_id)
            if (st.get(r["work_id"]) or {}).get("split") == "test"}
    verdict, why = _verdicts(store, rid)
    complete = sorted(f for f in test if all(f in (verdict.get(j) or {}).get("judged", ()) for j in judges))
    props = propositions(store, rid)
    papers = []
    for src in cfg["sources"]:
        items = []
        for k, p in sorted(props.items(), key=lambda kv: int(re.sub(r"\D", "", kv[0].rsplit(".", 1)[-1]) or 0)):
            if p["source"] != src["id"]:        # in the paper's own order, most important first (R1, R2, ..., R10)
                continue
            lean ={f: _mean((verdict.get(j) or {}).get("pairs", {}).get((k, f), 0) for j in judges) for f in complete}
            pro, con = [f for f, v in lean.items() if v > 0], [f for f, v in lean.items() if v < 0]
            c = p["content"]
            items.append({"key": k, "claim": c["claim"], "where": c.get("where"), "confidence": c["confidence"],
                          "direction": c["direction"], "for": len(pro), "against": len(con),
                          "studies_for": len({test[f] for f in pro}), "studies_against": len({test[f] for f in con}),
                          "status": ("not tested yet" if not pro and not con else "supported" if not con else
                                     "contradicted" if not pro else "mixed"),
                          "why": next((why.get((j, k, f)) for f in con + pro for j in judges
                                       if why.get((j, k, f))), None)})
        papers.append({"source": src["id"], "name": src["name"], "paper": src["paper"], "claims": items})
    return {"round": rid, "frozen_at": row["frozen_at"], "frozen_sha256": row["frozen_sha256"],
            "intact": (digest(props) == row["frozen_sha256"]) if row["frozen_at"] else None,
            "test_from": test_from(cfg), "test_studies": len(set(test.values())), "test_findings": len(test),
            "judged": len(complete), "unjudged": len(test) - len(complete),
            "late": sum(1 for s in st.values() if s["split"] == "excluded"), "papers": papers}


def claims_report(result: dict) -> str:
    c, cfg = result["claims"], result["cfg"]
    L = [f"# The forward check of the papers: corpus {result['corpus']}, round {result['round']}", "",
         f"Generated by `python -m ichnos referee {result['run']} --report` from the evidence store; no agent "
         f"scored anything. Pre-registered settings: {result['config_path']}, sha256 `{result['config_sha256'][:16]}…`.",
         "", f"The papers' {result['propositions']} propositions (their claims, and the shuffled control) were frozen at "
         f"{result['frozen_at']}, sha256 `{(result['frozen_sha256'] or '')[:16]}…`, before any later study was seen. "
         + ("The stored propositions still match that hash." if result["intact"] else
            "**The stored propositions no longer match that hash: they were changed after the freeze.**"), "",
         f"Every study the papers read is seen. Later studies test the claims: those the review gains after the freeze "
         f"and first published from {c['test_from']} on (after the models' training cutoff, "
         f"{cfg['split']['model_cutoff']}). So far {c['test_studies']} studies with {c['test_findings']} findings, "
         f"{c['judged']} judged by every judge; {c['late']} later-found studies were published too early to count.", ""]
    for p in c["papers"]:
        L += [f"## {p['name']}", "", "| Claim | Where | Later findings for / against (studies) | "
              "Status |", "| --- | --- | --- | --- |"]
        for x in p["claims"]:
            L.append(f"| {x['claim']} | {x['where'] or ''} | {x['for']} / {x['against']} ({x['studies_for']} / "
                     f"{x['studies_against']}) | {x['status']} |")
        L.append("")
    sh = result["main"]["sources"].get("shuffled")
    if sh and result["main"]["findings"]:
        L += ["## The judges' check", "", f"The shuffled control (propositions recombined at random by code) scored "
              f"{_f(sh['score'], 2, True)}, with {_pct(sh['contra'])} of those tested only contradicted."]
    ag = result["agreement"]
    if ag and ag.get("either", {}).get("kappa") is not None:
        L += ["", f"The two judges agree on supports / contradicts / not addressed over the pairs either called addressed "
              f"with kappa {ag['either']['kappa']:.2f} (n = {ag['either']['n']})."]
    return "\n".join(L) + "\n"


# Planning: what a round would cost and could show, before it begins

def plan(store: Store, run_id: int, root: Path = ROOT) -> dict:
    """The round as it would be played today: the split, the test set, the power and the agent tasks. Reads only."""
    corpus, path, text, cfg = _cfg_of(store, run_id, root)
    rows, meta, split = opening(store, run_id, cfg)
    counts: dict[str, dict] = {}
    for s in ("training", "test", "excluded"):
        ws = [w for w, x in split.items() if x[0] == s]
        counts[s] = {"studies": len(ws), "findings": sum(1 for r in rows if split[r["work_id"]][0] == s)}
    n_test, s_test = counts["test"]["findings"], counts["test"]["studies"]
    group = int(cfg["agents"].get("findings_per_batch", 10)) * int(cfg["agents"].get("batches_per_task", 3))
    n_props = per_source(cfg) * reps(cfg) * len(all_sources(cfg))
    train = sorted(r["finding_id"] for r in rows if split[r["work_id"]][0] == "training")
    R = {"round": cfg["round"], "run_id": run_id, "cfg": cfg}
    ev = len("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in _evidence(store, R, shown(cfg, cfg["round"], train),
                                                                                     "plan")))
    begun = (store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ref_rounds'").fetchone()
             and store.db.execute("SELECT 1 FROM ref_rounds WHERE round = ?", (cfg["round"],)).fetchone())
    return {"round": cfg["round"], "corpus": corpus, "run": run_id, "config": path.relative_to(root).as_posix(),
            "config_sha256": evolution._sha(text), "begun": bool(begun), "T": cfg["split"]["T"], "claims": claims(cfg),
            "test_from": test_from(cfg), "split": counts,
            "excluded": dict(Counter(x[2] for x in split.values() if x[0] == "excluded")),
            "missing_dates": 0 if claims(cfg) else len(missing_dates(store, cfg, meta)), "propositions": n_props,
            "tasks": {"ref_generate": len(sources(cfg)) * reps(cfg),
                      **({} if claims(cfg) else {"ref_probe": 1, "ref_check": 1, "ref_rate": len(cfg["agents"]["judges"])}),
                      "ref_judge": len(cfg["agents"]["judges"]) * math.ceil(n_test / group)},
            "tokens_in": {"generate": (ev + 6000) // 4, "judge": (n_props * 450 + group * 1200 + 4000) // 4},
            "power": _power(n_test, s_test)}


def plan_text(p: dict) -> str:
    L = [f"Round {p['round']}: corpus {p['corpus']}, run {p['run']}, {p['config']} sha256 {p['config_sha256'][:16]}… "
         + ("ALREADY BEGUN" if p["begun"] else "not begun"), "",
         (f"  Seen (training): every study the papers read; test: studies found later, first published from "
          f"{p['test_from']} on." if p.get("claims") else
          f"  Training: first published before {p['T']}; test: from {p['test_from']} on.")]
    for s, c in p["split"].items():
        L.append(f"  {s:<9} {c['studies']:>4} studies {c['findings']:>5} findings")
    if p["excluded"]:
        L.append("  Excluded: " + ", ".join(f"{n} {why}" for why, n in sorted(p["excluded"].items())))
    if p["missing_dates"]:
        L.append(f"  {p['missing_dates']} studies still need a Crossref date: fetch them with --dates before --start")
    pw = p["power"]
    if pw:
        L.append(f"  Power of a lens against a control (design effect {pw['deff']:.2f}): "
                 + "; ".join(f"d = {d}: {pw[d]:.0%}" for d in ("0.2", "0.3", "0.5")))
    L.append(f"  Propositions: {p['propositions']}. Tasks: " + ", ".join(f"{k} {v}" for k, v in p["tasks"].items())
             + f" = {sum(p['tasks'].values())}")
    L.append("  Input per task, about: " + ", ".join(f"{k} {v:,} tokens" for k, v in p["tokens_in"].items()))
    return "\n".join(L)


# The report

def _f(x, d=2, sign=False):
    if x is None:
        return "–"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def _pct(x):
    return "–" if x is None else f"{x:.0%}"


def report(result: dict) -> str:
    if result.get("claims"):
        return claims_report(result)
    cfg, m = result["cfg"], result["main"]
    names = {lens["id"]: lens["name"] for lens in cfg["lenses"]} | {"placebo": "Placebo (Lens P)", "no_lens": "No lens",
                                                                    "shuffled": "Shuffled (code)"}
    sp, cut = cfg["split"], cfg["split"]["model_cutoff"]
    L = [f"# The referee: corpus {result['corpus']}, round {result['round']}", "",
         f"Generated by `python -m ichnos referee {result['run']} --report` from the evidence store. Every number "
         f"is computed by code from the stored labels; no agent scored anything. Pre-registered settings: "
         f"{result['config_path']}, sha256 `{result['config_sha256'][:16]}…`.", "",
         f"The {result['propositions']} propositions were frozen at {result['frozen_at']}, sha256 "
         f"`{result['frozen_sha256'][:16]}…`, before any agent saw a test study. "
         + ("The stored propositions still match that hash." if result["intact"] else
            "**The stored propositions no longer match that hash: they were changed after the freeze.**"), "",
         "## The test", ""]
    s = result["split"]
    L += [f"- Training: {s.get('training', {}).get('studies', 0)} studies, {s.get('training', {}).get('findings', 0)} "
          f"findings, first published before {sp['T']}. Every source read these and nothing else.",
          f"- Test: {s.get('test', {}).get('studies', 0)} studies, {s.get('test', {}).get('findings', 0)} findings, first "
          f"published from {result['test_from']} on, after the models' training cutoff ({cut}), by Crossref's earliest "
          "date. By wave: " + ", ".join(f"{'start' if w == 0 else f'update {w}'} {n}" for w, n in result["waves"].items())
          + ".",
          f"- Excluded: {s.get('excluded', {}).get('studies', 0)} studies ("
          + ", ".join(f"{n} {why}" for why, n in sorted(result["excluded"].items())) + ").",
          f"- Scored: {m['findings']} test findings from {m['studies']} studies, judged by every judge"
          + (f"; {result['flagged_findings']} findings of studies the probe flagged are left out" if
             result["flagged_findings"] else "")
          + (f"; {result['unjudged']} still unjudged" if result["unjudged"] else "") + ".", "",
          "## Leaderboard", "",
          "Score: the weighted net confirmations per proposition. Each test finding that supports a proposition counts "
          "+1 and each that contradicts it -1 (averaged over the judges), times the proposition's weight (what it "
          "commits to, from 0 to 1, halved if every rater called it obvious), summed over the test findings and averaged "
          "over the source's propositions. Coverage: share of its propositions any test finding addressed; hit and "
          "contradiction rate: of those, the share only supported and only contradicted; all three averaged over the "
          "judges. Commitment: the mean weight before halving. Brier: stated confidence against the share of supporting "
          "verdicts, over the propositions tested (n). The 95% interval resamples whole studies. Noise: the gap between "
          "the scores of the source's two runs; a gap between two sources smaller than their noise is not evidence.", "",
          "| Source | Propositions | Commitment | Obvious | Coverage | Hit rate | Contradiction rate | Score [95% CI] | "
          "Brier (n) | Noise |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: |"]
    for src, r in sorted(m["sources"].items(), key=lambda x: -(x[1]["score"] or 0)):
        ci = r["ci"]
        L.append(f"| {names.get(src, src)} | {r['propositions']} | {_f(r['specificity'])} | {_pct(r['obvious'])} | "
                 f"{_pct(r['coverage'])} | {_pct(r['hit'])} | {_pct(r['contra'])} | {_f(r['score'], 2, True)} "
                 f"[{_f(ci[1], 2, True)}, {_f(ci[2], 2, True)}] | {_f(r['brier'])} ({_f(r['tested'], 1)}) | "
                 f"{_f(r['noise'])} |")
    L += ["", "## Each source against the controls", "",
          "Difference in score (positive = the first source scores higher), 95% interval from resampling whole studies, "
          f"and a two-sided permutation p that reassigns propositions between the two sources "
          f"({cfg.get('score', {}).get('permutations', 5000)} draws). The two answer different questions: the interval "
          "holds the propositions fixed and asks how much the evidence could vary; the permutation asks whether the "
          "source matters at all, given how much one proposition differs from the next. A difference counts only where "
          "both agree.", "",
          "| Source | vs placebo | vs no lens | vs shuffled |", "| --- | --- | --- | --- |"]
    for a in [x for x in m["sources"] if x not in ("placebo", "shuffled")]:
        cells = []
        for b in ("placebo", "no_lens", "shuffled"):
            c = m["comparisons"].get(f"{a}|{b}")
            cells.append("–" if not c or c["diff"] is None else
                         f"{_f(c['diff'], 2, True)} [{_f(c['lo'], 2, True)}, {_f(c['hi'], 2, True)}], p = {_f(c['p'], 3)}")
        L.append(f"| {names.get(a, a)} | " + " | ".join(cells) + " |")
    ag = result["agreement"]
    if ag:
        def k(x):
            return (f"{x['agree']:.1%}, kappa {x['kappa']:.2f} (n = {x['n']})" if x.get("kappa") is not None else
                    f"{x['agree']:.1%} (n = {x['n']})" if x.get("agree") is not None else "–")
        L += ["", "## The judges", "",
              f"{ag['judges'][0]} and {ag['judges'][1]} judged blind, with one mask for every proposition. Agreement on "
              f"supports / contradicts / not addressed over every finding and proposition: {k(ag['all'])}; over the "
              f"pairs either judge called addressed: {k(ag['either'])}; supports against contradicts where both did: "
              f"{k(ag['both'])}. On which propositions are obvious: {k(ag['obvious'])}."]
    c = result["contamination"]
    L += ["", "## Leakage: the contamination probe", "",
          f"The generating model ({cfg['agents']['probe']}) was shown only the title, authors, year and venue of each "
          f"test study and of {c['control']['studies']} control studies from the year before its cutoff, and said what "
          f"each found; {cfg['agents']['check']} compared the answers with the real findings. A study is flagged when "
          "the model says it knows it and gets a finding at least partly right, or gets a finding right that its title "
          "does not give away.", "",
          "| Studies | Probed | Says it knows | Title gives a result | A finding reproduced | Flagged |",
          "| --- | ---: | ---: | ---: | ---: | ---: |"]
    for g in ("test", "control"):
        x = c[g]
        L.append(f"| {g} | {x['studies']} | {x['known']} | {x['title_states']} | {x['reproduced']} | {x['flagged']} |")
    if result["leaks"]:
        L += ["", "Flagged test studies:", *[f"- {x['cite']}: {x['title']}" for x in result["leaks"]]]
    wf = result["with_flagged"]
    if wf:
        L += ["", f"With the flagged studies included ({wf['findings']} findings): "
              + ", ".join(f"{names.get(s_, s_)} {_f(r['score'], 2, True)}" for s_, r in
                          sorted(wf["sources"].items(), key=lambda x: -(x[1]["score"] or 0))) + "."]
    pw = result["power"]
    L += ["", "## What this sample can and cannot show", "",
          f"- Size. {m['findings']} findings from {m['studies']} studies. A difference between two sources of d = 0.5 "
          f"would be detected {_pct(pw.get('0.5'))} of the time, d = 0.3 {_pct(pw.get('0.3'))}, d = 0.2 "
          f"{_pct(pw.get('0.2'))} (design effect {_f(pw.get('deff'))}). The pilot of lens evolution found gaps between "
          "theory lenses and a placebo of about d = 0.1, which this test cannot see. A leaderboard with intervals this "
          "wide is a first reading, not a verdict.",
          "- The registry grows. The propositions stay frozen; each search update adds test studies, and the intervals "
          "narrow as they are judged.",
          "- One model family. The generator, the probe, the raters and one judge are Claude models with the same "
          "training cutoff; their shared knowledge is a confound the second judge does not remove.",
          "- Judging is reading. A verdict is an agent's reading of a finding against a proposition; the kappa above "
          "says how far two readers agree. Code only counts.",
          "- The shuffled control is phrased by a template, so a judge could tell its style apart, though not what it "
          "is.",
          "- Leakage. The test studies postdate the models' training data by their first publication date; earlier "
          "versions with another DOI (preprints, conference abstracts) can escape that, and the probe catches only what "
          "the model shows it remembers."]
    L += ["", "## Example propositions", ""]
    for e in result["examples"]:
        p = e["content"]
        mods = "; ".join(f"{x['effect']} when {x['condition']}" for x in p["moderators"]) or "none"
        counts = "; ".join(f"{short(j)} {s_} supporting, {c_} contradicting" for j, (s_, c_) in e["counts"].items())
        L += [f"**{names.get(e['source'], e['source'])}** ({e['key']}): {p['claim']}", "",
              f"- Population: {p['population']}. Contrast: {p['contrast']}. Outcome: {p['outcome']}. Direction: "
              f"{p['direction']}. Moderators: {mods}. Confidence {p['confidence']:.2f}.",
              f"- Weight {e['weight']:.2f}{' (obvious)' if e['obvious'] else ''}; test findings: {counts}; net "
              f"{_f(e['net'], 1, True)}, weighted {_f(e['u'], 2, True)}."
              + (f" A judge: \"{e['why']}\"" if e["why"] else ""), ""]
    L += ["## Calls", "", ", ".join(f"{k} {n}" for k, n in sorted(result["calls"].items())) + ".", "",
          "Every proposition, with its key, is in "
          f"output/corpus_{result['corpus']}/run_{result['run']:03d}/referee_{result['round']}_frozen.json."]
    return "\n".join(L) + "\n"
