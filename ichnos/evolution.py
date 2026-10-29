"""Lens evolution: research programmes bred against held-out evidence.

A lens (lenses.py) is a research programme: a hard core, a heuristic and a protective belt of auxiliary
assumptions. Here lenses are varied by an agent and selected by a game that code scores, the held-out evidence
game. A lens reads the development findings, whose results it sees, and forecasts the sign of findings it has not
seen. The forecasts are probabilities over positive, negative, null and mixed, and code scores them against the
coded results (Brier score). Fitness is the gain over the placebo lens and over a forecaster with no lens, minus
a penalty for every auxiliary assumption. Lakatos's test is built in: a progressive programme predicts what it
has not yet seen.

The game is set in protocol/evolution_corpus_X.toml, which is pre-registered: its hash is fixed when the game
begins. Findings are split by study:
- development: visible to the forecasters, the mutator and the judge;
- validation: forecast by every lens, and the selection signal;
- test: the newest studies, sealed until evolution ends, then forecast once by the final lenses.

Arms. Generation 1 is shared: every island mutates its seed once. From generation 2 the arms differ only in how
they choose parents. The mutation step and the forecaster stay the same in all three arms.
- selected: the member of the island with the highest validation fitness (seeds are never deleted, so a seed
  that beats its children is chosen again);
- no_selection: a random member, the same compute without selection;
- judge: the member an LLM judge thinks most promising, without scores (LLM taste as fitness, the Goodhart arm).
From `crossover_from` on, each arm adds one recombination of two islands.

Controls: the placebo lens, a shuffled-core lens (one proposition from each seed, which is incoherent on
purpose), a forecaster with no lens, a base-rate forecaster (code), and a recall probe. In the recall probe the
same model forecasts once with the study named and once with neither the study nor the evidence. Generation 0 runs
each seed and control twice; the gap between the two runs is the noise floor.

Task kinds, in task folders like every other agent step:
- evo_probe:   an agent turns each validation and test finding into a neutral question. It also reads off the
               result, and the probe counts only if that reading agrees with the extraction's coding.
- evo_predict: one lens (or none) forecasts the probes.
- evo_mutate:  one child from one parent (belt or core change) or from two (recombination).
- evo_judge:   the judge arm's parent choices, and a final blind ranking of every lens.

Lineage: every lens with its parents, operator and change; every forecast; every choice (evo_events).
step() writes the tasks the state allows; evaluate() and report() only count.

Since 0.2 (game evo-ab1, sized from the v0 pilot):
- Pooled games. A file with [[parts]] pre-registers several games under one hash, one per corpus, each with its
  own lens file and islands; their arm comparisons are pooled at the end (evaluate_pool). Studies stay whole within
  a split, and a corpus's lenses only forecast that corpus's findings.
- Further probes. [probes] per_finding lets the probe writer ask a finding's other outcomes or subgroups as well
  ("37.2"). They count only if the finding's own probe agrees with the extraction and they fit its coding.
- [coding] maps a corpus's own result field (corpus B: `direction`) to the four classes.
- [replicates] sets the runs of every forecast: seeds and controls, children, and the test split.
- [split] test = "after_cutoff": the test split is the studies first published after the forecaster's training
  cutoff (Crossref dates, fetched with fetch_dates). [probes] identity = "anonymous": no probe names its sample.
- [agents] probes_per_task splits a long forecast into several tasks.
- plan() gives the expected probes, the power and the agent calls before anything is written, and a new game
  begins only when asked (start=True; `evolve RUN --game ID --start`).
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
import tomllib
from collections import Counter
from pathlib import Path

from . import lenses, tables, writer
from .config import ROOT
from .http import ApiError, Http
from .store import Store, now

KINDS = ("evo_probe", "evo_predict", "evo_mutate", "evo_judge")
ARMS = ("selected", "no_selection", "judge")
ABBREV = {"selected": "S", "no_selection": "N", "judge": "J"}
MODES = ("lens", "no_card", "prior", "recall")
PROBE_BATCH = 35
PROBE_TASK = 3                         # batches per probe-writer task: 105 findings
PARTS = ("population", "first", "second", "outcome")
DIRECTIONS = ("positive", "negative", "null")
# What the v0 pilot measured (game evo-a1, validation, 23 probes, 6 lenses run twice; measured() gives the first
# three); plan() sizes a game with it until the game's own generation 0 is in.
PILOT = {"sigma_probe": 0.096,         # SD of one probe's Brier score between two runs of the same lens
         "sigma_true": 0.039,          # SD across probes of a seed's true gain over a reference (runs averaged out)
         "sigma_run": 0.010,           # SD of a whole task's mean Brier that more probes do not shrink: 0.020 over the
                                       # six lenses, all of it from the shuffled lens, 0 for the five coherent ones;
                                       # the plan takes the middle
         "icc_study": 0.15,            # correlation of gains between probes of one study (0.17-0.18, from 9 studies)
         "icc_finding": 0.5,           # assumed between the probes of one finding
         "yield": 0.70}                # findings with a coded result that became probes (46 of 66)
BASIS = re.compile(r"^(core|aux):[A-Za-z]+\d+$|^evidence:\d+$|^(base_rate|background|recall)$")
# Words that would give a probe's answer away. Agents are told the list; the import refuses a probe with one.
LEAK = re.compile(r"\b(increas|decreas|improv|worsen|benefit|harm|buffer|protect|reduc|boost|declin|predict|"
                  r"associat|linked|impact|signific|unrelated|no differen|did not differ|effects? (of|on))\w*", re.I)
CITE = re.compile(r"\b(1[89]|20)\d\d\b|\bet al\b|#\d|\bfindings? \d", re.I)
PROP_ID = re.compile(r"^\s*([A-Za-z]+\d+)\s*:")

SCHEMA = """
CREATE TABLE IF NOT EXISTS evo_games (
    game        TEXT PRIMARY KEY,
    corpus      TEXT NOT NULL,
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    config_path TEXT NOT NULL,
    config_sha256 TEXT NOT NULL,
    config      TEXT NOT NULL,                   -- the pre-registered file as it was when the game began
    lens_sha256 TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evo_probes (
    game        TEXT NOT NULL,
    finding_id  INTEGER NOT NULL REFERENCES findings(finding_id),
    work_id     INTEGER NOT NULL,
    split       TEXT NOT NULL,                   -- development, validation, test
    coded       TEXT,                            -- the extraction's sign, mapped to a class
    status      TEXT NOT NULL,                   -- evidence (development), pending, ok, excluded
    question    TEXT,
    parts       TEXT,                            -- JSON: population, first, second, outcome
    read_as     TEXT,                            -- the probe writer's reading of the result
    note        TEXT,
    call_id     INTEGER REFERENCES calls(call_id),
    PRIMARY KEY (game, finding_id)
);
CREATE TABLE IF NOT EXISTS evo_lenses (
    game        TEXT NOT NULL,
    lens_id     TEXT NOT NULL,
    mode        TEXT NOT NULL,                   -- lens, no_card, prior, recall
    arm         TEXT NOT NULL,                   -- seed, control, shared, selected, no_selection, judge
    island      TEXT,
    generation  INTEGER NOT NULL,
    operator    TEXT NOT NULL,                   -- seed, shuffle, mutate, crossover
    parents     TEXT NOT NULL,                   -- JSON list of lens ids
    card        TEXT,                            -- JSON: name, core, heuristic, auxiliaries
    level       TEXT,
    mechanism   TEXT,
    change      TEXT,                            -- belt, core, recombination
    rationale   TEXT,                            -- JSON: the mutator's rationale and the anomalies it addressed
    agent       TEXT,
    call_id     INTEGER REFERENCES calls(call_id),
    sha256      TEXT,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (game, lens_id)
);
CREATE TABLE IF NOT EXISTS evo_tasks (
    game        TEXT NOT NULL,
    need        TEXT NOT NULL,                   -- e.g. predict:jahoda:validation:1, mutate:fryer.g2.S
    kind        TEXT NOT NULL,
    folder      TEXT NOT NULL,
    status      TEXT NOT NULL,                   -- open, imported
    created_at  TEXT NOT NULL,
    PRIMARY KEY (game, need)
);
CREATE TABLE IF NOT EXISTS evo_predictions (
    prediction_id INTEGER PRIMARY KEY,
    game        TEXT NOT NULL,
    lens_id     TEXT NOT NULL,
    split       TEXT NOT NULL,                   -- validation, test, contamination
    replicate   INTEGER NOT NULL,
    agent       TEXT NOT NULL,
    folder      TEXT,
    content     TEXT NOT NULL,                   -- JSON: by_finding {id: {p, basis, why, known}}, strains
    sha256      TEXT NOT NULL,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evo_events (
    event_id    INTEGER PRIMARY KEY,
    game        TEXT NOT NULL,
    event       TEXT NOT NULL,                   -- game_started, parent_chosen, judge_parents, judge_rank, test_opened
    detail      TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evo_extra (
    game        TEXT NOT NULL,                   -- a finding's further probes: another outcome or subgroup
    finding_id  INTEGER NOT NULL,
    k           INTEGER NOT NULL,                -- 2, 3, ...: the probe's key is "<finding>.<k>"
    status      TEXT NOT NULL,                   -- ok, excluded
    question    TEXT,
    parts       TEXT,
    read_as     TEXT,                            -- the probe writer's reading, the answer when it counts
    note        TEXT,
    call_id     INTEGER REFERENCES calls(call_id),
    PRIMARY KEY (game, finding_id, k)
);
CREATE TABLE IF NOT EXISTS evo_dates (
    work_id     INTEGER PRIMARY KEY REFERENCES works(work_id),
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,                   -- found, no_date, no_doi, not_found, error
    published   TEXT,                            -- the earliest date Crossref gives (created, online, print, issued,
                                                 -- posted), its first possible day when only a month or year is known
    fetched_at  TEXT NOT NULL
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


# The pre-registered file

def _files(root: Path) -> list[Path]:
    return sorted((root / "protocol").glob("evolution_corpus_*.toml"))


def part(cfg: dict, game_id: str) -> dict:
    """One game of a pooled file: the shared sections with the part's own keys (game, corpus, run, islands, placebo,
    domain, archive, coding) on top. A part's table changes only the settings it names ([parts.split] test = "year"
    keeps the shared validation_share). `pool` names the file's game."""
    for p in cfg.get("parts") or []:
        if p.get("game") == game_id:
            out = {k: v for k, v in cfg.items() if k != "parts"}
            for k, v in p.items():
                out[k] = {**out[k], **v} if isinstance(v, dict) and isinstance(out.get(k), dict) else v
            out["pool"] = cfg["game"]
            return out
    raise ValueError(f"no part {game_id!r} in pooled game {cfg.get('game')!r}")


def game_config(text: str, game_id: str) -> dict:
    """A game's settings from the pre-registered text stored with it."""
    cfg = tomllib.loads(text)
    return part(cfg, game_id) if cfg.get("parts") else cfg


def load(corpus: str, root: Path = ROOT, game: str | None = None, run_id: int | None = None) -> tuple[Path, str, dict]:
    """The pre-registered file of a corpus's game. protocol/evolution_corpus_<x>.toml is the corpus's own game; a
    pooled file (evolution_corpus_*.toml with [[parts]]) adds one game per part. With several, `game` chooses."""
    default = root / "protocol" / f"evolution_corpus_{corpus.lower()}.toml"
    found = []
    for path in _files(root):
        text = path.read_text(encoding="utf-8")
        cfg = tomllib.loads(text)
        if cfg.get("parts"):
            for p in cfg["parts"]:
                if str(p.get("corpus", "")).upper() == corpus.upper() and (run_id is None or p.get("run") in (None, run_id)):
                    found.append((path, text, part(cfg, p["game"])))
        elif path == default:
            found.append((path, text, cfg))
    if game:
        hit = [f for f in found if game in (f[2].get("game"), f[2].get("pool"))]
        if len(hit) != 1:
            raise ValueError(f"no evolution game {game!r} for corpus {corpus}"
                             + (f"{' run ' + str(run_id) if run_id else ''}; there are: "
                                + ", ".join(f[2]["game"] for f in found) if found else ""))
        return hit[0]
    own = [f for f in found if f[0] == default]
    if own:
        return own[0]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError(f"no evolution file: {default.relative_to(root)}")
    raise ValueError(f"several evolution games for corpus {corpus}: {', '.join(f[2]['game'] for f in found)}; "
                     "choose one with --game")


def check_config(cfg: dict, lens_cfg: dict) -> None:
    for key in ("game", "seed", "islands", "placebo", "domain"):
        if key not in cfg:
            raise ValueError(f"the evolution file needs `{key}`")
    sp = cfg.get("split") or {}
    if sp.get("test", "year") not in ("year", "after_cutoff"):
        raise ValueError("[split] test is \"year\" or \"after_cutoff\"")
    if sp.get("test") == "after_cutoff" and not re.fullmatch(r"\d{4}-\d\d-\d\d", str(sp.get("cutoff", ""))):
        raise ValueError("[split] test = \"after_cutoff\" needs cutoff = \"YYYY-MM-DD\", the forecaster's training cutoff")
    if sp.get("test", "year") == "year" and "test_from_year" not in sp:
        raise ValueError("[split] needs test_from_year")
    if int(sp.get("evidence_max", 1) or 1) < 1:
        raise ValueError("[split] evidence_max is a number of findings, 1 or more")
    pb = cfg.get("probes") or {}
    if int(pb.get("per_finding", 1)) < 1 or pb.get("identity", "full") not in ("full", "anonymous"):
        raise ValueError("[probes] per_finding is 1 or more; identity is \"full\" or \"anonymous\"")
    r = cfg.get("replicates")
    if r is not None and (not isinstance(r, dict) or any(int(r.get(k, 1)) < 1 for k in ("seeds", "children", "test"))):
        raise ValueError("[replicates] sets seeds, children and test, each 1 or more")
    c = cfg.get("coding")
    if c is not None and (not c.get("field") or not isinstance(c.get("map"), dict)
                          or any(v not in classes(cfg) for v in c["map"].values())):
        raise ValueError(f"[coding] needs `field` and a `map` from its values to {classes(cfg)}")
    by_id = {lens["id"]: lens for lens in lens_cfg.get("lenses") or []}
    for lid in cfg["islands"] + [cfg["placebo"]]:
        if lid not in by_id:
            raise ValueError(f"lens {lid!r} is not in the lens file")
    if len(cfg["islands"]) < 2:
        raise ValueError("at least two islands")
    s = cfg.get("schedule") or {}
    if int(s.get("crossover_from", 2)) < 2:
        raise ValueError("crossover_from must be 2 or later: generation 1 has only the seeds")
    a = cfg.get("archive") or {}
    for lid in cfg["islands"]:
        d = (a.get("seeds") or {}).get(lid) or {}
        if d.get("level") not in a.get("levels", []) or d.get("mechanism") not in a.get("mechanisms", []):
            raise ValueError(f"[archive.seeds] {lid} needs a level and a mechanism from the lists")


def classes(cfg: dict) -> list[str]:
    return list((cfg.get("score") or {}).get("classes") or ["positive", "negative", "null", "mixed"])


def coded_class(sign: str | None) -> str | None:
    """The extraction's sign as a forecast class: compound signs ("negative; null") count as mixed."""
    parts = [p.strip() for p in (sign or "").split(";") if p.strip()]
    if not parts or "not_applicable" in parts:
        return None
    if len(set(parts)) > 1:
        return "mixed"
    return parts[0] if parts[0] in ("positive", "negative", "null", "mixed") else None


def coded_of(fields: dict, cfg: dict) -> str | None:
    """A finding's coded result as a class: the extraction's `sign`, or the corpus's own field through [coding]."""
    c = cfg.get("coding")
    if not c:
        return coded_class(fields.get("sign"))
    parts = [p.strip() for p in str(fields.get(c["field"]) or "").split(";") if p.strip()]
    mapped = [c["map"].get(p) for p in parts]
    if not mapped or None in mapped:
        return None
    return "mixed" if len(set(mapped)) > 1 else mapped[0]


def result_text(fields: dict, cfg: dict) -> str:
    """What the evidence shows of a finding's result: its class, or the coded value when it has no class."""
    coded = coded_of(fields, cfg)
    if coded or not cfg.get("coding"):
        return coded or "not applicable"
    return str(fields.get(cfg["coding"]["field"]) or "not coded").replace("_", " ")


def context_of(fields: dict, cfg: dict) -> dict:
    """A finding's coded context without its result: setting, predictor and outcome, or a corpus's own fields."""
    if not cfg.get("coding"):
        return {k: fields.get(k) for k in ("setting", "predictor", "outcome")}
    return {k: v for k, v in fields.items() if k not in (cfg["coding"]["field"], "sign", "direction")}


def reps(cfg: dict, stage: str) -> int:
    """Runs of every forecast at a stage (seeds, children, test). Before [replicates] only generation 0 repeated."""
    r = cfg.get("replicates")
    if isinstance(r, dict):
        return int(r.get(stage, 1))
    return int(cfg["schedule"].get("replicates", 2)) if stage == "seeds" else 1


def per_finding(cfg: dict) -> int:
    return int((cfg.get("probes") or {}).get("per_finding", 1))


def anonymous(cfg: dict) -> bool:
    return (cfg.get("probes") or {}).get("identity", "full") == "anonymous"


def chunks(cfg: dict, n: int) -> int:
    """How many tasks one forecast of n probes takes."""
    size = int((cfg.get("agents") or {}).get("probes_per_task") or 0)
    return max(1, math.ceil(n / size)) if size and n > size else 1


# Publication dates, for a test split after the forecaster's training cutoff

def earliest(msg: dict) -> str | None:
    """The earliest date a Crossref record gives, at the first possible day of a month or year."""
    out = []
    for key in ("created", "published-online", "published-print", "issued", "posted", "published"):
        dp = ((msg.get(key) or {}).get("date-parts") or [[None]])[0]
        if dp and dp[0]:
            y, mo, d = (list(dp) + [1, 1])[:3]
            out.append(f"{int(y):04d}-{int(mo or 1):02d}-{int(d or 1):02d}")
    return min(out) if out else None


def date_candidates(cfg: dict, meta: dict) -> list[int]:
    """The studies that could have appeared after the cutoff: those from its year on, or without a year."""
    year = int(cfg["split"]["cutoff"][:4])
    return sorted(w for w, m in meta.items() if not m.get("year") or int(m["year"]) >= year)


def dates(store: Store, ids) -> dict[int, dict]:
    ids = list(ids)
    if not ids or not store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'evo_dates'").fetchone():
        return {}
    return {r["work_id"]: dict(r) for r in store.db.execute(
        f"SELECT * FROM evo_dates WHERE work_id IN ({','.join('?' * len(ids))})", tuple(ids))}


def fetch_dates(store: Store, run_id: int, cfg: dict, http: Http | None = None, refresh: bool = False) -> Counter:
    """Crossref's dates for the run's studies that may postdate the cutoff. Every lookup is logged as a call."""
    ensure(store)
    rows = tables.finding_rows(store, run_id)
    meta = tables.works(store, {r["work_id"] for r in rows})
    have = dates(store, meta)
    http = http or Http(store, run_id, max_retries=3)
    counts = Counter()
    for w in date_candidates(cfg, meta):
        if not refresh and w in have and have[w]["status"] != "error":
            counts["cached"] += 1
            continue
        doi, published = meta[w].get("doi"), None
        if not doi:
            status = "no_doi"
        else:
            try:
                published = earliest(http.get_json("crossref", f"https://api.crossref.org/works/{doi}", {})["message"])
                status = "found" if published else "no_date"
            except ApiError as err:
                status = "not_found" if err.status == 404 else "error"
        with store.db:
            store.db.execute("INSERT OR REPLACE INTO evo_dates VALUES (?, 'crossref', ?, ?, ?)",
                             (w, status, published, now()))
        counts[status] += 1
    return counts


# The game

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rng(*parts) -> random.Random:
    return random.Random(_sha("-".join(str(p) for p in parts)))


def _order(items, *parts):
    return sorted(items, key=lambda x: _sha("-".join(str(p) for p in parts) + f"-{x}"))


def game(store: Store, run_id: int, root: Path = ROOT, game_id: str | None = None, start: bool = True) -> dict:
    """The run's game: on first use the split, the seeds and the controls are fixed; afterwards the
    pre-registered file must be unchanged. With start=False a game that has not begun is refused."""
    ensure(store)
    run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"no run {run_id}")
    corpus = run["corpus"]
    path, text, cfg = load(corpus, root, game_id, run_id)
    if cfg.get("run") not in (None, run_id):
        raise ValueError(f"game {cfg['game']} is pre-registered for run {cfg['run']}")
    _, lens_text, lens_cfg = lenses.load(corpus, root)
    check_config(cfg, lens_cfg)
    sha = _sha(text)
    row = store.db.execute("SELECT * FROM evo_games WHERE game = ?", (cfg["game"],)).fetchone()
    rel = path.relative_to(root).as_posix()
    G = {"game": cfg["game"], "cfg": cfg, "sha": sha, "corpus": corpus, "run_id": run_id, "root": root, "path": rel}
    if row:
        if row["config_sha256"] != sha:
            raise ValueError(f"{rel} changed after game {cfg['game']} began; restore "
                             "it, or give it a new `game` id to start a new game")
        if row["run_id"] != run_id:
            raise ValueError(f"game {cfg['game']} belongs to run {row['run_id']}")
        return G
    if not start:
        raise ValueError(f"game {cfg['game']} has not begun. See what it would cost with `evolve {run_id} --game "
                         f"{cfg['game']} --plan`; begin it with --start")
    _start(store, G, rel, text, lens_cfg, _sha(lens_text))
    return G


def assign_splits(cfg: dict, game_id: str, per_study: Counter, meta: dict, dated: dict) -> dict[int, str]:
    """Whole studies to development, validation and test. Test: the studies from test_from_year on, or those first
    published after the cutoff; a study without a date is never test. The rest alternate, in a seeded order, so that
    validation gets its share of the findings."""
    sp = cfg["split"]
    split = {}
    share = float(sp.get("validation_share", 0.5))
    rest = []
    for w in per_study:
        if sp.get("test", "year") == "after_cutoff":
            published = (dated.get(w) or {}).get("published")
            test = bool(published) and published > sp["cutoff"]
        else:
            test = int(meta[w]["year"] or 0) >= int(sp["test_from_year"])
        if test:
            split[w] = "test"
        else:
            rest.append(w)
    v = d = 0
    for w in _order(rest, game_id, cfg["seed"], "split"):   # whole studies, so that no study is on both sides
        if v + d == 0 or v / (v + d) < share:
            split[w], v = "validation", v + per_study[w]
        else:
            split[w], d = "development", d + per_study[w]
    return split


def _start(store: Store, G: dict, rel: str, text: str, lens_cfg: dict, lens_sha: str) -> None:
    cfg, game_id, run_id = G["cfg"], G["game"], G["run_id"]
    rows = tables.finding_rows(store, run_id)
    if not rows:
        raise ValueError("the run has no findings to play the game on")
    meta = tables.works(store, {r["work_id"] for r in rows})
    per_study = Counter(r["work_id"] for r in rows)
    dated = {}
    if cfg["split"].get("test") == "after_cutoff":
        dated = dates(store, meta)
        missing = [w for w in date_candidates(cfg, meta) if w not in dated or dated[w]["status"] == "error"]
        if missing:
            raise ValueError(f"no publication date yet for {len(missing)} studies; fetch them first with "
                             f"`evolve {run_id} --game {game_id} --dates`")
    split = assign_splits(cfg, game_id, per_study, meta, dated)
    by_id = {lens["id"]: lens for lens in lens_cfg["lenses"]}
    seeds = cfg["archive"]["seeds"]
    at = now()
    with store.db:
        store.db.execute("INSERT INTO evo_games VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                         (game_id, G["corpus"], run_id, rel, G["sha"], text, lens_sha, at))
        for r in rows:
            s = split[r["work_id"]]
            fields = json.loads(r["fields"] or "{}")
            coded = coded_of(fields, cfg)
            status = "evidence" if s == "development" else "pending" if coded else "excluded"
            raw = fields.get(cfg["coding"]["field"]) if cfg.get("coding") else None
            store.db.execute("INSERT INTO evo_probes (game, finding_id, work_id, split, coded, status, note) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (game_id, r["finding_id"], r["work_id"], s, coded, status,
                              None if coded or s == "development" else
                              f"no sign to forecast ({raw or 'not applicable'})"))

        def add(lens_id, mode, arm, island=None, operator="seed", card=None, level=None, mechanism=None):
            store.db.execute("INSERT INTO evo_lenses (game, lens_id, mode, arm, island, generation, operator, parents, "
                             "card, level, mechanism, created_at) VALUES (?, ?, ?, ?, ?, 0, ?, '[]', ?, ?, ?, ?)",
                             (game_id, lens_id, mode, arm, island, operator,
                              json.dumps(card, ensure_ascii=False) if card else None, level, mechanism, at))

        for lid in cfg["islands"]:
            lens = by_id[lid]
            add(lid, "lens", "seed", lid, card=_seed_card(lens), level=seeds[lid]["level"],
                mechanism=seeds[lid]["mechanism"])
        add(cfg["placebo"], "lens", "control", card=_seed_card(by_id[cfg["placebo"]]))
        add("shuffled", "lens", "control", operator="shuffle", card=shuffled_card(cfg, by_id))
        for mode in ("no_card", "prior", "recall"):
            add(mode, mode, "control")
        detail = {"config": rel, "config_sha256": G["sha"], "lens_sha256": lens_sha,
                  "split": {s: sorted(w for w, x in split.items() if x == s) for s in ("development", "validation", "test")}}
        if dated:
            detail["undated"] = sorted(w for w in date_candidates(cfg, meta) if not dated[w]["published"])
            detail["published"] = {str(w): dated[w]["published"] for w in sorted(dated) if w in split}
        _event(store, game_id, "game_started", detail)


def _seed_card(lens: dict) -> dict:
    return {"name": lens["name"], "core": list(lens["core"]), "heuristic": lens["heuristic"], "auxiliaries": []}


def shuffled_card(cfg: dict, by_id: dict) -> dict:
    """One proposition from each seed and the heuristic of another: the same sentences, without a programme."""
    rng = _rng(cfg["game"], cfg["seed"], "shuffle")
    core = []
    for i, lid in enumerate(cfg["islands"], 1):
        prop = rng.choice(by_id[lid]["core"])
        core.append(f"S{i}: " + PROP_ID.sub("", prop, count=1).strip())
    return {"name": "Lens S", "core": core, "heuristic": by_id[rng.choice(cfg["islands"])]["heuristic"],
            "auxiliaries": []}


def _event(store: Store, game_id: str, event: str, detail: dict) -> None:
    store.db.execute("INSERT INTO evo_events (game, event, detail, created_at) VALUES (?, ?, ?, ?)",
                     (game_id, event, json.dumps(detail, ensure_ascii=False), now()))


def events(store: Store, game_id: str, event: str) -> list[dict]:
    return [json.loads(r["detail"]) for r in store.db.execute(
        "SELECT detail FROM evo_events WHERE game = ? AND event = ? ORDER BY event_id", (game_id, event))]


# State

def lens_rows(store: Store, game_id: str) -> dict[str, dict]:
    out = {}
    for r in store.db.execute("SELECT * FROM evo_lenses WHERE game = ? ORDER BY generation, rowid", (game_id,)):
        d = dict(r)
        d["card"] = json.loads(d["card"]) if d["card"] else None
        d["parents"] = json.loads(d["parents"])
        d["rationale"] = json.loads(d["rationale"]) if d["rationale"] else None
        out[d["lens_id"]] = d
    return out


def probes(store: Store, game_id: str) -> dict[int, dict]:
    return {r["finding_id"]: dict(r) for r in store.db.execute(
        "SELECT * FROM evo_probes WHERE game = ? ORDER BY finding_id", (game_id,))}


def targets(store: Store, game_id: str, split: str | None = None) -> dict[str, dict]:
    """Every probe that counts, by key: a finding's own probe ("37") and its further probes ("37.2"), each with its
    study, split, question and answer."""
    sql = " AND p.split = ?" if split else ""
    args = (game_id,) + ((split,) if split else ())
    out = {}
    for r in store.db.execute("SELECT * FROM evo_probes p WHERE game = ? AND status = 'ok'" + sql
                              + " ORDER BY finding_id", args):
        out[str(r["finding_id"])] = {"finding": r["finding_id"], "work_id": r["work_id"], "split": r["split"],
                                     "question": r["question"], "answer": r["coded"]}
    for r in store.db.execute("SELECT e.finding_id, e.k, e.question, e.read_as, p.work_id, p.split FROM evo_extra e "
                              "JOIN evo_probes p ON p.game = e.game AND p.finding_id = e.finding_id "
                              "WHERE e.game = ? AND e.status = 'ok'" + sql + " ORDER BY e.finding_id, e.k", args):
        out[f"{r['finding_id']}.{r['k']}"] = {"finding": r["finding_id"], "work_id": r["work_id"], "split": r["split"],
                                              "question": r["question"], "answer": r["read_as"]}
    return out


def forecasts(store: Store, game_id: str) -> dict[tuple, list[dict]]:
    """{(lens_id, split): [content of each replicate]}; a re-run of a replicate replaces the earlier one. A forecast
    split into several tasks counts once all of its parts are in."""
    latest = {}
    for r in store.db.execute("SELECT * FROM evo_predictions WHERE game = ? ORDER BY prediction_id", (game_id,)):
        c = json.loads(r["content"])
        latest[(r["lens_id"], r["split"], r["replicate"], c.get("chunk", 1))] = c
    merged: dict[tuple, dict] = {}
    for (lid, split, rep, chunk), c in sorted(latest.items()):
        m = merged.setdefault((lid, split, rep), {"by_finding": {}, "strains": set(), "in": set(),
                                                  "chunks": c.get("chunks", 1)})
        m["by_finding"].update(c["by_finding"])
        m["strains"] |= set(c.get("strains") or [])
        m["in"].add(chunk)
    out: dict[tuple, list] = {}
    for (lid, split, _), m in sorted(merged.items()):
        if len(m["in"]) >= m["chunks"]:
            out.setdefault((lid, split), []).append({"by_finding": m["by_finding"], "strains": sorted(m["strains"])})
    return out


def tasks(store: Store, game_id: str) -> dict[str, dict]:
    return {r["need"]: dict(r) for r in store.db.execute("SELECT * FROM evo_tasks WHERE game = ?", (game_id,))}


def child_id(island: str, gen: int, arm: str) -> str:
    if arm == "shared":
        return f"{island}.g{gen}"
    return f"{'x' if island == 'x' else island}.g{gen}.{ABBREV[arm]}"


def expected(cfg: dict, arm: str, gen: int) -> list[str]:
    """The children an arm makes in a generation: one mutant per island, and a recombination from crossover_from."""
    if gen == 1:
        return [child_id(i, 1, "shared") for i in cfg["islands"]]
    out = [child_id(i, gen, arm) for i in cfg["islands"]]
    if gen >= int(cfg["schedule"].get("crossover_from", 2)):
        out.append(child_id("x", gen, arm))
    return out


def population(cfg: dict, rows: dict, arm: str, island: str, gen: int) -> list[str]:
    """An island's members an arm may choose a parent from in a generation: the seed (never deleted) and the
    shared and own children of earlier generations."""
    return [lid for lid, r in rows.items() if r["island"] == island and r["generation"] < gen
            and (r["arm"] in ("seed", "shared") or r["arm"] == arm)]


def generations(cfg: dict) -> int:
    return int(cfg["schedule"].get("generations", 2))


def final_children(cfg: dict) -> dict[str, list[str]]:
    g = generations(cfg)
    return {arm: expected(cfg, arm if g > 1 else "shared", g) for arm in ARMS}


def test_set(cfg: dict) -> list[str]:
    out = list(cfg["islands"]) + [cfg["placebo"], "shuffled", "no_card"]
    for kids in final_children(cfg).values():
        out += [k for k in kids if k not in out]
    return out


# Scoring: code only

def brier(p: list[float], y: int) -> float:
    return sum((q - (1.0 if k == y else 0.0)) ** 2 for k, q in enumerate(p))


def log_loss(p: list[float], y: int, floor: float) -> float:
    k = len(p)
    return -math.log((1 - k * floor) * p[y] + floor)


def _mean(xs):
    xs = list(xs)
    return sum(xs) / len(xs) if xs else None


def per_probe(content: list[dict], answers: dict[int, int], cls: list[str], floor: float) -> dict[int, dict]:
    """Each probe's Brier score, log loss and hit, averaged over replicates."""
    out = {}
    for f, y in answers.items():
        reps = [c["by_finding"][str(f)]["p"] for c in content if str(f) in c["by_finding"]]
        if not reps:
            continue
        out[f] = {"brier": _mean(brier(p, y) for p in reps), "log": _mean(log_loss(p, y, floor) for p in reps),
                  "hit": _mean(1.0 if max(range(len(p)), key=lambda k: (p[k], -k)) == y else 0.0 for p in reps)}
    return out


def base_rate(store: Store, game_id: str, cls: list[str], split: str) -> list[float]:
    """The forecaster with no lens and no agent: how often each result occurs among the findings it may see
    (development; for the test split, development and validation), with one added to every count."""
    seen = ("development",) if split != "test" else ("development", "validation")
    n = Counter(r["coded"] for r in store.db.execute(
        f"SELECT coded FROM evo_probes WHERE game = ? AND split IN ({','.join('?' * len(seen))}) AND coded IS NOT NULL",
        (game_id, *seen)))
    total = sum(n[c] for c in cls) + len(cls)
    return [(n[c] + 1) / total for c in cls]


def cluster_ci(diffs: dict[int, float], study: dict[int, int], n: int, seed: str, runs: list | None = None) -> tuple:
    """The mean of per-probe differences and a 95% interval from resampling whole studies. With `runs` (a game with
    [replicates]: for each lens with 2 or more runs, its weight in the difference and each run's per-probe Brier
    score), every draw then also resamples each lens's runs with replacement, so that a shift of a whole forecast
    task widens the interval. The runs are drawn from a stream of their own: the studies drawn stay those of `seed`."""
    if not diffs:
        return None, None, None
    by: dict[int, list[float]] = {}
    for f, d in diffs.items():
        by.setdefault(study[f], []).append(d)
    groups = list(by.values())
    mean = _mean(diffs.values())
    if len(groups) < 2:
        return mean, None, None
    rng = random.Random(seed)
    terms = _run_terms(diffs, study, list(by), runs or [])
    stats = []
    if not terms:
        for _ in range(n):
            pick = [groups[rng.randrange(len(groups))] for _ in groups]
            stats.append(sum(map(sum, pick)) / sum(map(len, pick)))
    else:
        rr = random.Random(f"{seed}:runs")
        sums, sizes = [sum(g) for g in groups], [len(g) for g in groups]
        for _ in range(n):
            idx = [rng.randrange(len(groups)) for _ in groups]
            total = sum(map(sums.__getitem__, idx))
            for w, dev in terms:
                at = [sum(map(d.__getitem__, idx)) for d in dev]
                total += w * _mean(at[rr.randrange(len(dev))] for _ in dev)
            stats.append(total / sum(map(sizes.__getitem__, idx)))
    stats.sort()
    return mean, stats[int(0.025 * n)], stats[min(n - 1, int(0.975 * n))]


def _run_terms(diffs: dict, study: dict, studies: list, runs: list) -> list[tuple]:
    """For each lens with 2 or more runs: its weight, and for each run, per study, how far the run's Brier scores lie
    from the lens's per-probe mean over its runs (summed over the study's probes in `diffs`). A run without a probe
    counts as that mean."""
    pos = {s: i for i, s in enumerate(studies)}
    out = []
    for w, rs in runs:
        if not w or len(rs) < 2:
            continue
        dev = [[0.0] * len(studies) for _ in rs]
        for f in diffs:
            xs = [r[f] for r in rs if f in r]
            if not xs:
                continue
            m = _mean(xs)
            for k, r in enumerate(rs):
                if f in r:
                    dev[k][pos[study[f]]] += r[f] - m
        out.append((w, dev))
    return out


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None

    def ranks(v):
        order = sorted(range(len(v)), key=lambda i: v[i])
        r = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = _mean(rx), _mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return num / den if den else None


class Scores:
    """Per-probe scores of every lens on a split, the two references, gains and fitness."""

    def __init__(self, store: Store, G: dict, split: str):
        cfg = G["cfg"]
        self.cfg, self.split = cfg, split
        self.cls = classes(cfg)
        self.floor = float(cfg["score"].get("log_floor", 0.005))
        tg = targets(store, G["game"])
        self.study = {k: t["work_id"] for k, t in tg.items()}
        self.answers = {k: self.cls.index(t["answer"]) for k, t in tg.items() if t["split"] == split}
        self.fc = forecasts(store, G["game"])
        self.rows = lens_rows(store, G["game"])
        self.n = int(cfg["score"].get("bootstrap", 2000))
        self.base = base_rate(store, G["game"], self.cls, split)
        self._cache, self._gains = {}, {}

    def required(self, lens_id: str) -> int:
        """The runs a lens's score waits for: a lens is not scored on part of its replicates."""
        if self.split == "test":
            return reps(self.cfg, "test")
        r = self.rows.get(lens_id) or {}
        return reps(self.cfg, "seeds" if r.get("generation", 0) == 0 else "children")

    def probe_scores(self, lens_id: str) -> dict[str, dict] | None:
        if lens_id == "base_rate":
            return {f: {"brier": brier(self.base, y), "log": log_loss(self.base, y, self.floor),
                        "hit": 1.0 if max(range(len(self.base)), key=lambda k: self.base[k]) == y else 0.0}
                    for f, y in self.answers.items()}
        if lens_id not in self._cache:
            content = self.fc.get((lens_id, self.split))
            ok = content and len(content) >= self.required(lens_id)
            self._cache[lens_id] = per_probe(content, self.answers, self.cls, self.floor) if ok else None
        return self._cache[lens_id]

    def summary(self, lens_id: str) -> dict | None:
        s = self.probe_scores(lens_id)
        if not s:
            return None
        return {k: _mean(v[k] for v in s.values()) for k in ("brier", "log", "hit")} | {"n": len(s)}

    def runs(self, lens_id: str) -> list[dict[str, float]] | None:
        """Each run's per-probe Brier score, for the bootstrap to resample: only in a game with [replicates] and for
        a scored lens with 2 or more runs. A lens with one run, and the base rate, stay fixed."""
        if not isinstance(self.cfg.get("replicates"), dict) or not self.probe_scores(lens_id):
            return None
        content = self.fc.get((lens_id, self.split)) or []
        if len(content) < 2:
            return None
        return [{f: brier(c["by_finding"][f]["p"], y) for f, y in self.answers.items() if f in c["by_finding"]}
                for c in content]

    def run_terms(self, weights: dict[str, float]) -> list[tuple]:
        """For cluster_ci: each lens's weight in a difference of scores, with its runs where it has runs to resample."""
        return [(w, r) for lid, w in weights.items() if (r := self.runs(lid))]

    def gain(self, lens_id: str, ref: str) -> tuple:
        a, b = self.probe_scores(lens_id), self.probe_scores(ref)
        if not a or not b:
            return None, None, None
        key = (lens_id, ref)
        if key not in self._gains:
            diffs = {f: b[f]["brier"] - a[f]["brier"] for f in a if f in b}
            self._gains[key] = cluster_ci(diffs, self.study, self.n, f"{lens_id}-{ref}-{self.split}",
                                          self.run_terms({lens_id: -1.0, ref: 1.0}))
        return self._gains[key]

    def aux(self, lens_id: str) -> int:
        card = (self.rows.get(lens_id) or {}).get("card") or {}
        return len(card.get("auxiliaries") or [])

    def fitness(self, lens_id: str) -> float | None:
        """Gain over the weaker-scoring of the two references is not enough: a lens must beat both, so fitness is
        the smaller of the two gains, minus the belt's penalty."""
        g1, g2 = self.gain(lens_id, self.cfg["placebo"])[0], self.gain(lens_id, "no_card")[0]
        if g1 is None or g2 is None:
            return None
        return min(g1, g2) - float(self.cfg["score"].get("aux_penalty", 0.005)) * self.aux(lens_id)

    def strains(self, lens_id: str) -> float | None:
        content = self.fc.get((lens_id, self.split))
        return _mean(len(c.get("strains") or []) for c in content) if content else None

    def signature(self, lens_id: str) -> list[int] | None:
        """The most probable result per probe (first replicate): what the lens says, for diversity."""
        content = self.fc.get((lens_id, self.split))
        if not content:
            return None
        bf = content[0]["by_finding"]
        return [max(range(len(self.cls)), key=lambda k: (bf[str(f)]["p"][k], -k)) for f in sorted(self.answers)
                if str(f) in bf]

    def noise_floor(self, ids: list[str]) -> float | None:
        """The mean gap in mean Brier score between two runs of the same lens. With [replicates], every run counts:
        the expected gap, 2s/sqrt(pi), from the pooled variance s^2 of a lens's run means."""
        gaps, variances = [], []
        for lid in ids:
            content = self.fc.get((lid, self.split)) or []
            if len(content) >= 2:
                m = [_mean(brier(c["by_finding"][str(f)]["p"], y) for f, y in self.answers.items()
                           if str(f) in c["by_finding"]) for c in content]
                gaps.append(abs(m[0] - m[1]))
                mu = _mean(m)
                variances.append(sum((x - mu) ** 2 for x in m) / (len(m) - 1))
        if isinstance(self.cfg.get("replicates"), dict):
            return 2 * math.sqrt(_mean(variances)) / math.sqrt(math.pi) if variances else None
        return _mean(gaps)


# Choosing parents

def choose_parent(store: Store, G: dict, rows: dict, sc: Scores | None, arm: str, island: str, gen: int):
    """The parent an arm develops on an island in a generation, or None while the state does not allow it yet."""
    cfg = G["cfg"]
    pop = population(cfg, rows, arm, island, gen)
    if gen == 1 or len(pop) == 1:
        return pop[0] if pop else None
    if arm == "selected":
        fit = {lid: sc.fitness(lid) for lid in pop}
        if any(v is None for v in fit.values()):
            return None
        return max(pop, key=lambda lid: (fit[lid], -rows[lid]["generation"]))
    if arm == "no_selection":
        return _rng(G["game"], cfg["seed"], arm, gen, island).choice(pop)
    for e in events(store, G["game"], "judge_parents"):
        if e["arm"] == arm and e["generation"] == gen:
            return e["choices"][island]
    return None


def choose_pair(store: Store, G: dict, rows: dict, sc: Scores | None, arm: str, gen: int, parents: dict):
    """The two programmes an arm recombines: from two different islands."""
    cfg = G["cfg"]
    if any(p is None for p in parents.values()):
        return None
    if arm == "selected":
        fit = {i: sc.fitness(p) for i, p in parents.items()}
        if any(v is None for v in fit.values()):
            return None
        ranked = sorted(parents, key=lambda i: -fit[i])
        cell = lambda i: (rows[parents[i]]["level"], rows[parents[i]]["mechanism"])
        for a in ranked:                       # the best pair whose archive cells differ, else the best two
            for b in ranked:
                if a != b and ranked.index(b) > ranked.index(a) and cell(a) != cell(b):
                    return [parents[a], parents[b]]
        return [parents[ranked[0]], parents[ranked[1]]]
    if arm == "no_selection":
        rng = _rng(G["game"], cfg["seed"], arm, gen, "pair")
        a, b = rng.sample(list(cfg["islands"]), 2)
        return [rng.choice(population(cfg, rows, arm, a, gen)), rng.choice(population(cfg, rows, arm, b, gen))]
    for e in events(store, G["game"], "judge_parents"):
        if e["arm"] == arm and e["generation"] == gen:
            return e["crossover"]
    return None


# Writing tasks

def _evidence(store: Store, G: dict, ids: list[int]) -> list[dict]:
    run_id = G["run_id"]
    rows = {r["finding_id"]: r for r in tables.finding_rows(store, run_id)}
    meta = tables.works(store, {rows[f]["work_id"] for f in ids}, run_id)
    chart = tables.charting(store, run_id)
    out = []
    for f in sorted(ids):
        r = rows[f]
        fields = json.loads(r["fields"] or "{}")
        design = ", ".join((chart.get(r["work_id"], {}).get("design") or {}).get("values", [])) or "not charted"
        out.append({"id": f, "study": meta[r["work_id"]]["cite"], "design": design, "finding": r["finding"],
                    "for_whom": r["for_whom"] or "", **context_of(fields, G["cfg"]),
                    "result": result_text(fields, G["cfg"])})
    return out


def shown(cfg: dict, game_id: str, ids: list[int]) -> list[int]:
    """The evidence an agent sees: every finding, or with [split] evidence_max a seeded sample of that many, the
    same sample in every task that shows the same findings, so that the lenses are compared on the same evidence."""
    cap = int((cfg.get("split") or {}).get("evidence_max") or 0)
    if not cap or len(ids) <= cap:
        return list(ids)
    return sorted(_order(ids, game_id, cfg["seed"], "evidence")[:cap])


def _dev_evidence(store: Store, G: dict) -> list[dict]:
    return _evidence(store, G, shown(G["cfg"], G["game"], _split_ids(store, G["game"], "development")))


def _split_ids(store: Store, game_id: str, split: str, status: str | None = None) -> list[int]:
    sql = "SELECT finding_id FROM evo_probes WHERE game = ? AND split = ?" + (" AND status = ?" if status else "")
    return [r[0] for r in store.db.execute(sql + " ORDER BY finding_id", (game_id, split) + ((status,) if status else ()))]


def _write(store: Store, G: dict, kind: str, need: str, label: str, instructions: str, batches: list,
           meta: dict, agent_role: str) -> Path:
    from .agents import _new_folder
    from .config import load_protocol
    root = G["root"]
    ppath, ptext, _ = load_protocol(G["corpus"], root)
    psha = store.add_protocol(G["corpus"], ppath.relative_to(root).as_posix(), ptext)
    agent = G["cfg"]["agents"][agent_role]
    out = _new_folder(root, G["corpus"], G["run_id"], kind, f"{G['game']}-{label}")
    (out / "instructions.md").write_text(instructions, encoding="utf-8")
    for i, b in enumerate(batches, 1):
        (out / f"batch_{i:02d}.json").write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    meta = {"kind": kind, "agent": agent, "run_id": G["run_id"], "corpus": G["corpus"], "protocol_sha256": psha,
            "game": G["game"], "config_sha256": G["sha"], "need": need, "batches": len(batches), **meta}
    (out / "task.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO evo_tasks VALUES (?, ?, ?, ?, 'open', ?)",
                         (G["game"], need, kind, out.relative_to(root).as_posix(), now()))
    return out


def probe_groups(store: Store, G: dict) -> list[list[int]]:
    """The findings each probe-writer task gets: every finding with a coded result outside development, in a seeded
    order, PROBE_TASK batches to a task. Fixed by the game, so that a task written again gets the same findings."""
    asked = [f for f, p in probes(store, G["game"]).items() if p["split"] != "development" and p["coded"]]
    order = _order(asked, G["game"], "probe")
    size = PROBE_BATCH * PROBE_TASK
    return [order[i:i + size] for i in range(0, len(order), size)]


def probe_need(k: int) -> str:
    return "probe" if k == 1 else f"probe:{k}"


def probe_tasks(store: Store, G: dict, k: int = 1) -> Path:
    run_id = G["run_id"]
    pr = probes(store, G["game"])
    group = probe_groups(store, G)[k - 1]
    pending = [f for f in group if pr[f]["status"] == "pending"]
    rows = {r["finding_id"]: r for r in tables.finding_rows(store, run_id)}
    meta_w = tables.works(store, {rows[f]["work_id"] for f in pending}, run_id)
    items, names = [], {}
    for f in _order(pending, G["game"], "probe"):
        r = rows[f]
        fields = json.loads(r["fields"] or "{}")
        items.append({"id": f, "finding": r["finding"], "for_whom": r["for_whom"] or "", "passage": r["evidence"],
                      **context_of(fields, G["cfg"])})
        names[str(f)] = sorted({writer.name_parts(n)[0] for n in writer._names(meta_w[r["work_id"]]["authors"])
                                if len(writer.name_parts(n)[0]) >= 4})
    batches = [{"findings": items[i:i + PROBE_BATCH]} for i in range(0, len(items), PROBE_BATCH)]
    return _write(store, G, "evo_probe", probe_need(k), "probes" if k == 1 else f"probes-{k}",
                  probe_instructions(G["cfg"]), batches, {"records": len(items), "surnames": names}, "probe")


def split_targets(store: Store, game_id: str, split: str) -> dict[str, dict]:
    """The probes a forecast on a split asks: the contamination probe asks validation and test together."""
    if split == "contamination":
        return {**targets(store, game_id, "validation"), **targets(store, game_id, "test")}
    return targets(store, game_id, split)


def predict_need(lens_id: str, split: str, rep: int, chunk: int = 1, n: int = 1) -> str:
    return f"predict:{lens_id}:{split}:{rep}" + (f":{chunk}" if n > 1 else "")


def predict_task(store: Store, G: dict, lens: dict, split: str, rep: int, chunk: int = 1) -> Path:
    cfg, game_id = G["cfg"], G["game"]
    mode = lens["mode"]
    tg = split_targets(store, game_id, split)
    seen = [] if mode in ("prior", "recall") else shown(cfg, game_id, _split_ids(store, game_id, "development") + (
        _split_ids(store, game_id, "validation") if split == "test" else []))
    order = _order(list(tg), game_id, lens["lens_id"], split, rep)
    n = chunks(cfg, len(order))
    mine = order[(chunk - 1) * len(order) // n: chunk * len(order) // n]
    keys = {f"Q{i}": k for i, k in enumerate(mine, 1)}
    chart = tables.charting(store, G["run_id"])
    who = tables.works(store, {tg[k]["work_id"] for k in mine}, G["run_id"]) if mode == "recall" else {}
    items = []
    for q, k in keys.items():
        p = tg[k]
        item = {"probe": q, "question": p["question"],
                "design": ", ".join((chart.get(p["work_id"], {}).get("design") or {}).get("values", [])) or "not charted"}
        if mode == "recall":
            m = who[p["work_id"]]
            item["study"] = f"{m['cite']}, {m['venue'] or 'venue not recorded'}"
        items.append(item)
    data = {"classes": classes(cfg), "probes": items}
    if mode == "lens":
        data["lens"] = lens["card"]
    if seen:
        data["evidence"] = _evidence(store, G, seen)
    label = f"{lens['lens_id']}-{split}-r{rep}" + (f"-c{chunk}" if n > 1 else "")
    return _write(store, G, "evo_predict", predict_need(lens["lens_id"], split, rep, chunk, n), label,
                  predict_instructions(cfg, mode, lens.get("card")), [data],
                  {"lens_id": lens["lens_id"], "mode": mode, "split": split, "replicate": rep, "keys": keys,
                   "chunk": chunk, "chunks": n, "evidence_ids": seen, "records": len(items)}, "predict")


def _full(row: dict) -> dict:
    return {**row["card"], "level": row["level"], "mechanism": row["mechanism"]}


def mutate_task(store: Store, G: dict, child: str, arm: str, island: str, gen: int, parents: list[dict]) -> Path:
    cfg = G["cfg"]
    operator = "crossover" if len(parents) == 2 else "mutate"
    data = {"operator": operator, "domain": cfg["domain"], "parent": _full(parents[0]),
            "evidence": _dev_evidence(store, G),
            "levels": cfg["archive"]["levels"], "mechanisms": cfg["archive"]["mechanisms"]}
    if operator == "crossover":
        data["parent_b"] = _full(parents[1])
    return _write(store, G, "evo_mutate", f"mutate:{child}", child, mutate_instructions(cfg, operator), [data],
                  {"child": child, "arm": arm, "island": island, "generation": gen, "operator": operator,
                   "parents": [p["lens_id"] for p in parents], "records": 1}, "mutate")


def judge_parents_task(store: Store, G: dict, rows: dict, arm: str, gen: int) -> Path:
    cfg = G["cfg"]
    pops = {i: population(cfg, rows, arm, i, gen) for i in cfg["islands"]}
    every = _order([lid for p in pops.values() for lid in p], G["game"], arm, gen, "mask")
    mask = {f"L{n}": lid for n, lid in enumerate(every, 1)}
    back = {v: k for k, v in mask.items()}
    isl = {f"I{n}": i for n, i in enumerate(cfg["islands"], 1)}
    data = {"domain": cfg["domain"], "programmes": {k: _public(rows[v]) for k, v in mask.items()},
            "islands": {k: sorted(back[lid] for lid in pops[i]) for k, i in isl.items()},
            "recombine": gen >= int(cfg["schedule"].get("crossover_from", 2)),
            "evidence": _dev_evidence(store, G)}
    return _write(store, G, "evo_judge", f"judge:parents:{arm}:g{gen}", f"parents-{ABBREV[arm]}-g{gen}",
                  judge_instructions("parents"), [data],
                  {"mode": "parents", "arm": arm, "generation": gen, "mask": mask, "island_keys": isl,
                   "records": len(mask)}, "judge")


def judge_rank_task(store: Store, G: dict, rows: dict) -> Path:
    ids = [lid for lid, r in rows.items() if r["mode"] == "lens"]
    mask = {f"L{n}": lid for n, lid in enumerate(_order(ids, G["game"], "rank"), 1)}
    data = {"domain": G["cfg"]["domain"], "programmes": {k: _public(rows[v]) for k, v in mask.items()},
            "evidence": _dev_evidence(store, G)}
    return _write(store, G, "evo_judge", "judge:rank", "rank", judge_instructions("rank"), [data],
                  {"mode": "rank", "mask": mask, "records": len(mask)}, "judge")


def _public(row: dict) -> dict:
    """A programme as the judge sees it: no name (names carry lineage), no arm, no generation."""
    c = row["card"]
    return {"core": c["core"], "heuristic": c["heuristic"], "auxiliaries": c.get("auxiliaries") or []}


# The schedule

def step(store: Store, run_id: int, root: Path = ROOT, game_id: str | None = None, start: bool = True) -> dict:
    """Write every task the state allows that is not yet written, and say what is open."""
    G = game(store, run_id, root, game_id, start)
    cfg, game_id = G["cfg"], G["game"]
    have = tasks(store, game_id)
    written: list[Path] = []

    def need(key: str, make) -> None:
        t = have.get(key)
        if t and (t["status"] == "imported" or (root / t["folder"]).exists()):
            return
        written.append(make())

    pr = probes(store, game_id)
    if any(p["status"] == "pending" for p in pr.values()):
        for k, group in enumerate(probe_groups(store, G), 1):
            if any(pr[f]["status"] == "pending" for f in group):
                need(probe_need(k), lambda k=k: probe_tasks(store, G, k))
        return _status(store, G, written)
    rows = lens_rows(store, game_id)
    fc = forecasts(store, game_id)
    size = {s: len(split_targets(store, game_id, s)) for s in ("validation", "test", "contamination")}

    def forecast(row: dict, split: str, rep: int) -> None:
        n = chunks(cfg, size[split])
        for c in range(1, n + 1):
            need(predict_need(row["lens_id"], split, rep, c, n),
                 lambda row=row, split=split, rep=rep, c=c: predict_task(store, G, row, split, rep, c))

    gen0 = list(cfg["islands"]) + [cfg["placebo"], "shuffled", "no_card"]
    for lid in gen0:
        for rep in range(1, reps(cfg, "seeds") + 1):
            forecast(rows[lid], "validation", rep)
    for lid in ("prior", "recall"):
        forecast(rows[lid], "contamination", 1)
    if not all(len(fc.get((lid, "validation"), [])) >= reps(cfg, "seeds") for lid in gen0):
        return _status(store, G, written)
    sc = Scores(store, G, "validation")
    for gen in range(1, generations(cfg) + 1):
        for arm in (["shared"] if gen == 1 else list(ARMS)):
            earlier = [c for g in range(1, gen) for c in expected(cfg, "shared" if g == 1 else arm, g)]
            if not all(c in rows for c in earlier):
                continue
            if arm == "judge" and not any(e["arm"] == arm and e["generation"] == gen
                                          for e in events(store, game_id, "judge_parents")):
                need(f"judge:parents:{arm}:g{gen}", lambda arm=arm, gen=gen: judge_parents_task(store, G, rows, arm, gen))
                continue
            parents = {i: choose_parent(store, G, rows, sc, arm, i, gen) for i in cfg["islands"]}
            for i, p in parents.items():
                kid = child_id(i, gen, arm)
                if p and kid not in rows:
                    _note_choice(store, G, arm, gen, i, p)
                    need(f"mutate:{kid}", lambda kid=kid, arm=arm, i=i, gen=gen, p=p:
                         mutate_task(store, G, kid, arm, i, gen, [rows[p]]))
            if arm != "shared" and gen >= int(cfg["schedule"].get("crossover_from", 2)):
                kid = child_id("x", gen, arm)
                pair = None if kid in rows else choose_pair(store, G, rows, sc, arm, gen, parents)
                if pair:
                    _note_choice(store, G, arm, gen, "x", pair)
                    need(f"mutate:{kid}", lambda kid=kid, arm=arm, gen=gen, pair=pair:
                         mutate_task(store, G, kid, arm, rows[pair[0]]["island"], gen, [rows[x] for x in pair]))
        for lid, r in rows.items():
            if r["generation"] == gen and r["mode"] == "lens" and r["arm"] not in ("seed", "control"):
                for rep in range(1, reps(cfg, "children") + 1):
                    forecast(r, "validation", rep)
    finals = [c for kids in final_children(cfg).values() for c in kids]
    if all(c in rows and len(fc.get((c, "validation"), [])) >= reps(cfg, "children") for c in finals):
        if not events(store, game_id, "test_opened"):
            with store.db:
                _event(store, game_id, "test_opened", {"lenses": test_set(cfg)})
        need("judge:rank", lambda: judge_rank_task(store, G, rows))
        for lid in test_set(cfg):
            for rep in range(1, reps(cfg, "test") + 1):
                forecast(rows[lid], "test", rep)
    return _status(store, G, written)


def _note_choice(store: Store, G: dict, arm: str, gen: int, island: str, parent) -> None:
    detail = {"arm": arm, "generation": gen, "island": island, "parent": parent}
    if detail not in events(store, G["game"], "parent_chosen"):
        with store.db:
            _event(store, G["game"], "parent_chosen", detail)


def _status(store: Store, G: dict, written: list[Path]) -> dict:
    t = tasks(store, G["game"])
    return {"game": G["game"], "written": written,
            "open": sorted(v["folder"] for v in t.values() if v["status"] == "open"),
            "imported": sum(1 for v in t.values() if v["status"] == "imported"),
            "test_opened": bool(events(store, G["game"], "test_opened")),
            "done": bool(t) and all(v["status"] == "imported" for v in t.values()) and "judge:rank" in t}


# Instructions

def probe_instructions(cfg: dict) -> str:
    n = per_finding(cfg)
    example = {"finding": 12, "population": "...", "first": "...", "second": "...", "outcome": "...",
               "answer": "positive | negative | null | mixed"}
    more = []
    if n > 1:
        example["more"] = [{"outcome": "another outcome the passage reports", "answer": "positive | negative | null"}]
        more = [f"- more (optional): up to {n - 1} further probes from the same finding, one for each other outcome or "
                "subgroup the passage reports with a result of its own (for a mixed finding: the parts it is mixed "
                "of). Give only the parts that differ from the main probe, and the answer for that part alone: "
                "positive, negative or null. Leave `more` out when the passage reports a single result."]
    return "\n".join([
        "# Turning findings into neutral questions", "",
        f"A review collects studies of {cfg['domain']}. Each item in `findings` (batch_NN.json) is one finding with "
        "its verbatim passage. Other agents will later forecast these results without seeing them, so each finding "
        "must become a question that asks for the result without giving it away.", "",
        "For each finding write four short parts; code joins them into the question \"Among <population>: compared "
        "with <second>, is <outcome> higher in <first> (positive), lower (negative), no different (null), or does "
        "it differ by subgroup or measure (mixed)?\"", "",
        "- population: who was studied, in plain words (for example \"older adults in a national panel\").",
        "- first and second: the two groups or conditions compared, first being the group with more of the "
        "predictor, or the group after the transition (for example \"people who retired\" and \"people who kept "
        "working\"; for a continuous predictor \"people for whom work is more central\" and \"... less central\").",
        "- outcome: what was measured (for example \"sense of purpose\").",
        "- answer: the result the finding reports for that question: positive, negative, null or mixed.",
        "- skip: instead of the parts, a reason, when the finding cannot be asked as such a comparison (it only "
        "describes, or the passage does not give a direction).", *more, "",
        "Rules:",
        "- Use only the batch files of this folder; open no other file (not task.json), do not search the web.",
        "- Never give the result away: none of these words or their forms in any part: increase, decrease, improve, "
        "worsen, benefit, harm, buffer, protect, reduce, boost, decline, predict, associated, linked, impact, "
        "significant, unrelated, effect of, no difference. No study names, authors, years or finding numbers.",
        *(["- Name no sample: write the population in general terms (who, not which study): no country, region, city, "
           "institution, named survey, cohort or dataset, no sample size, no digits at all."] if anonymous(cfg) else []),
        "- Keep the comparison the finding makes; orient it so that the answer reads off the finding directly.",
        "- Every finding exactly once. Each part at most 25 words.", "",
        "Output: for each batch_NN.json, one JSON object in batch_NN_result.json:", "", "```json",
        json.dumps({"probes": [example, {"finding": 13, "skip": "why it cannot be asked as a comparison"}]}, indent=1),
        "```"]) + "\n"


OUTPUT_PREDICT = {"predictions": [{"probe": "Q1", "p": {"positive": 0.4, "negative": 0.2, "null": 0.3, "mixed": 0.1},
                                   "basis": ["core:J2", "evidence:12"], "why": "at most 20 words"}],
                  "strains": [12]}


def predict_instructions(cfg: dict, mode: str, card: dict | None) -> str:
    scale = ["- positive: the outcome is higher in the first group than in the second;",
             "- negative: it is lower;", "- null: no reliable difference;",
             "- mixed: it differs by subgroup or measure."]
    out = dict(OUTPUT_PREDICT)
    if mode == "lens":
        head = [f"# Forecasting unseen findings through one lens: {card['name']}", "",
                f"A review collects studies of {cfg['domain']}. `evidence` holds some of its findings with their "
                "results. Other findings are hidden. For each item in `probes` you forecast what the hidden study "
                "found, as a committed proponent of the lens below would.", "",
                "## The lens", "", "Hard core:", *[f"- {p}" for p in card["core"]], "",
                f"Heuristic: {card['heuristic']}", "", "Protective belt (auxiliary assumptions):",
                *([f"- {a['id']}: {a['statement']}" for a in card.get("auxiliaries") or []] or ["- none yet"]), "",
                "Let the lens drive the forecast where it applies: derive the expected result from its core and belt, "
                "and weigh the evidence it points you to. Where the lens is silent, fall back on the evidence and say "
                "so in basis."]
        basis = ("basis: what the forecast rests on, one or more of core:<id> and aux:<id> (from the lens above), "
                 "evidence:<id> (a finding in `evidence`), base_rate (how often each result occurs in the evidence), "
                 "background (general knowledge of the field).")
        extra = ["- strains: the ids of findings in `evidence` that sit badly with the lens's hard core. An admitted "
                 "anomaly costs nothing."]
    else:
        out = {"predictions": out["predictions"]}
        intro = {"no_card": "`evidence` holds some of its findings with their results. Forecast from the evidence and "
                            "your judgement.",
                 "prior": "You see none of its findings. Forecast from general knowledge of the field.",
                 "recall": "You see none of its findings, but each probe names its study (authors, year, venue). If "
                           "you know that study, forecast from what you remember of it; otherwise from general "
                           "knowledge of the field."}[mode]
        head = [f"# Forecasting unseen findings", "",
                f"A review collects studies of {cfg['domain']}. For each item in `probes` you forecast what the study "
                f"found. {intro}"]
        basis = ("basis: what the forecast rests on, one or more of " +
                 ("evidence:<id> (a finding in `evidence`), base_rate, " if mode == "no_card" else "") +
                 ("recall (you remember this study), " if mode == "recall" else "") + "background (general knowledge).")
        extra = (["- known: true only if you remember this specific study's result."] if mode == "recall" else [])
        if mode == "recall":
            out["predictions"] = [dict(out["predictions"][0], basis=["recall"], known=False)]
    return "\n".join(head + [
        "", "## The forecast", "",
        "For every probe give probabilities for the four results, which sum to 1:", *scale, "",
        "Be calibrated: code scores the forecasts against the hidden results (Brier score), and a confident wrong "
        "forecast costs much more than a cautious one.", "",
        "Rules:",
        "- Use only batch_01.json; open no other file of the project (not task.json), do not search the web.",
        "- Every probe exactly once, with p over exactly the four results.",
        f"- {basis}",
        "- why: at most 20 words.", *extra, "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps(out, ensure_ascii=False, indent=1), "```"]) + "\n"


def mutate_instructions(cfg: dict, operator: str) -> str:
    if operator == "crossover":
        task = ["Your task: recombine. `parent` and `parent_b` are two programmes from different traditions. Write one "
                "child programme that joins them into a single, coherent programme that should forecast findings "
                "not yet seen better than either parent.",
                "- Take at least one hard-core proposition verbatim from each parent (with its id). You may add one "
                "new bridging proposition. change = \"recombination\"."]
    else:
        task = ["Your task: write one child programme that should forecast findings not yet seen better than the "
                "parent does. Read the evidence through the parent. Where it strains or is silent, revise it in one "
                "of two ways:",
                "- belt: keep the hard core verbatim and change the protective belt (add, replace or drop auxiliary "
                "assumptions) or sharpen the heuristic. change = \"belt\".",
                "- core: rewrite or replace at most one hard-core proposition, keeping the others verbatim (a "
                "problem shift). change = \"core\"."]
    return "\n".join([
        "# Developing a research programme", "",
        f"A lens is a research programme in Lakatos's sense for reading the evidence on {cfg['domain']}: a hard core "
        "(what it holds fixed), a heuristic (the question it asks of every finding) and a protective belt of "
        "auxiliary assumptions. batch_01.json holds the programme(s), `evidence` (findings with their results) "
        "and the lists for `level` and `mechanism`.", "",
        *task, "",
        "Each child is tested by code: an agent forecasts hidden findings through it, and the forecasts are scored "
        "against the results. Bold content pays when it is right: a programme that forbids more and says more about "
        f"who and when is worth more. Each auxiliary assumption costs {cfg['score'].get('aux_penalty', 0.005)} in "
        "Brier score, so it must earn its place in better forecasts; one that only rescues a single finding is ad "
        "hoc.", "",
        "Rules:",
        "- Use only batch_01.json; open no other file of the project (not task.json), do not search the web.",
        "- The programme must be general: no study names, authors, years or finding numbers anywhere in it.",
        "- core: 2 to 4 propositions, each starting with an id (\"J1: ...\"), each at most 50 words. heuristic: at "
        "most 25 words. auxiliaries: at most 4, each with an id (A1, ...), a statement and a consequence (an "
        "observable result, beyond the case it rescues, that would follow from it).",
        "- level and mechanism: one value each from the lists, for what the child explains.",
        "- name: a new name for the child, at most 5 words.",
        "- anomalies: the ids of evidence findings the parent strains on that the child addresses.",
        "- rationale: at most 80 words.", "",
        "Output: one JSON object in batch_01_result.json:", "", "```json",
        json.dumps({"name": "...", "core": ["J1: ...", "J2: ..."], "heuristic": "...",
                    "auxiliaries": [{"id": "A1", "statement": "...", "consequence": "..."}],
                    "level": "one of levels", "mechanism": "one of mechanisms",
                    "change": "belt | core | recombination", "anomalies": [12], "rationale": "..."}, indent=1),
        "```"]) + "\n"


def judge_instructions(mode: str) -> str:
    criteria = ("Judge as a philosopher of science would: coherence, explanatory reach over the evidence, boldness "
                "(what it forbids), and promise for predicting findings not yet seen. The programmes' names are hidden "
                "and their order is random; do not reward length.")
    if mode == "parents":
        body = ["`islands` groups the programmes by tradition. For each island choose the one programme most worth "
                "developing next. If `recombine` is true, also choose two programmes from different islands whose "
                "combination looks most fruitful.", "", criteria, "",
                "Output: one JSON object in batch_01_result.json:", "", "```json",
                json.dumps({"choices": {"I1": "L3"}, "recombine": ["L1", "L4"],
                            "reasons": {"I1": "at most 25 words", "recombine": "at most 25 words"}}, indent=1), "```"]
    else:
        body = ["Rank every programme in `programmes` from the most to the least promising.", "", criteria, "",
                "Output: one JSON object in batch_01_result.json:", "", "```json",
                json.dumps({"ranking": ["L3", "L1", "L2"], "reasons": {"L3": "at most 20 words"}}, indent=1), "```"]
    return "\n".join([
        "# Judging research programmes", "",
        "batch_01.json holds `programmes` (research programmes in Lakatos's sense: hard core, heuristic, protective "
        "belt), the `domain` they interpret and `evidence` (findings with their results).", "",
        "- Use only batch_01.json; open no other file of the project (not task.json), do not search the web.", "",
        *body]) + "\n"


# Checking and storing answers

def _words(s) -> int:
    return len(str(s or "").split())


def _part_errs(where: str, k: str, v: str, names: list[str], anon: bool) -> list[str]:
    errs = []
    if _words(v) > 25:
        errs.append(f"{where}: `{k}` has more than 25 words")
    m = LEAK.search(v) or CITE.search(v) or (re.search(r"\d+", v) if anon else None)
    if m:
        errs.append(f"{where}: `{k}` gives the result or the study away ({m.group(0)!r})")
    for name in names:
        if re.search(rf"\b{re.escape(name)}\b", v, re.I):
            errs.append(f"{where}: `{k}` names an author ({name})")
    return errs


def check_probes(data: dict, answer, surnames: dict, cls: list[str], per: int = 1, anon: bool = False) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("probes"), list):
        return ["the answer needs a `probes` list"]
    errs = []
    want = {f["id"] for f in data["findings"]}
    got = Counter(x.get("finding") for x in answer["probes"] if isinstance(x, dict))
    if set(got) != want or any(v > 1 for v in got.values()):
        errs.append(f"every finding exactly once: missing {sorted(want - set(got))}, unknown "
                    f"{sorted(f for f in got if f not in want)}, twice {sorted(f for f, v in got.items() if v > 1)}")
    for x in answer["probes"]:
        if not isinstance(x, dict):
            errs.append("each probe is an object")
            continue
        f = x.get("finding")
        if x.get("skip"):
            continue
        names = surnames.get(str(f), [])
        parts = {k: str(x.get(k) or "").strip() for k in PARTS}
        for k, v in parts.items():
            if not v:
                errs.append(f"finding {f}: `{k}` is empty (or give `skip` with a reason)")
            errs += _part_errs(f"finding {f}", k, v, names, anon)
        if x.get("answer") not in cls:
            errs.append(f"finding {f}: answer must be one of {cls}")
        more = x.get("more") or []
        if not isinstance(more, list) or len(more) > per - 1:
            errs.append(f"finding {f}: `more` holds at most {per - 1} further probes" if per > 1 else
                        f"finding {f}: this game asks one probe per finding, without `more`")
            continue
        seen = {question(parts)}
        for j, m in enumerate(more, 2):
            where = f"finding {f}, further probe {j}"
            if not isinstance(m, dict):
                errs.append(f"{where}: an object")
                continue
            given = {k: str(m.get(k) or "").strip() for k in PARTS if str(m.get(k) or "").strip()}
            q = question({**parts, **given})
            if not given or q in seen:
                errs.append(f"{where}: give the part(s) in which it differs from the main probe and the others")
            seen.add(q)
            for k, v in given.items():
                errs += _part_errs(where, k, v, names, anon)
            if m.get("answer") not in DIRECTIONS:
                errs.append(f"{where}: answer must be one of {list(DIRECTIONS)}")
    return errs


def question(parts: dict) -> str:
    return (f"Among {parts['population']}: compared with {parts['second']}, is {parts['outcome']} higher in "
            f"{parts['first']} (positive), lower (negative), no different (null), or does it differ by subgroup or "
            "measure (mixed)?")


def _ids(card: dict) -> tuple[set, set]:
    core = {m.group(1) for p in card.get("core") or [] if (m := PROP_ID.match(p))}
    return core, {a.get("id") for a in card.get("auxiliaries") or []}


def check_predictions(meta: dict, data: dict, answer, cls: list[str]) -> list[str]:
    if not isinstance(answer, dict) or not isinstance(answer.get("predictions"), list):
        return ["the answer needs a `predictions` list"]
    errs = []
    keys = set(meta["keys"])
    got = Counter(x.get("probe") for x in answer["predictions"] if isinstance(x, dict))
    if set(got) != keys or any(v > 1 for v in got.values()):
        errs.append(f"every probe exactly once: missing {sorted(keys - set(got), key=lambda q: int(q[1:]))}, unknown "
                    f"{sorted(q for q in got if q not in keys)}, twice {sorted(q for q, v in got.items() if v > 1)}")
    mode = meta["mode"]
    core, aux = _ids(data.get("lens") or {})
    ev = {e["id"] for e in data.get("evidence") or []}
    for x in answer["predictions"]:
        if not isinstance(x, dict):
            errs.append("each prediction is an object")
            continue
        q = x.get("probe")
        p = x.get("p")
        if not isinstance(p, dict) or set(p) != set(cls) or not all(isinstance(v, (int, float)) and v >= 0
                                                                  for v in p.values()):
            errs.append(f"{q}: p needs exactly {cls}, each a number of at least 0")
        elif not 0.97 <= sum(p.values()) <= 1.03:
            errs.append(f"{q}: the probabilities sum to {sum(p.values()):.2f}, not 1")
        basis = x.get("basis")
        if not isinstance(basis, list) or not basis:
            errs.append(f"{q}: basis is a non-empty list")
        else:
            for b in basis:
                if not isinstance(b, str) or not BASIS.match(b):
                    errs.append(f"{q}: basis {b!r} is not core:<id>, aux:<id>, evidence:<id>, base_rate, background "
                                "or recall")
                elif b.startswith("core:") and (mode != "lens" or b[5:] not in core):
                    errs.append(f"{q}: {b} is not a hard-core proposition of the lens")
                elif b.startswith("aux:") and (mode != "lens" or b[4:] not in aux):
                    errs.append(f"{q}: {b} is not an auxiliary of the lens")
                elif b.startswith("evidence:") and int(b[9:]) not in ev:
                    errs.append(f"{q}: {b} is not a finding in `evidence`")
                elif b == "recall" and mode != "recall":
                    errs.append(f"{q}: recall only where the probe names its study")
        if _words(x.get("why")) > 40:
            errs.append(f"{q}: why has more than 40 words")
        if mode == "recall" and not isinstance(x.get("known"), bool):
            errs.append(f"{q}: known must be true or false")
    strains = answer.get("strains") or []
    if mode != "lens" and strains:
        errs.append("strains only for a lens")
    if not isinstance(strains, list) or any(s not in ev for s in strains):
        errs.append(f"strains must be ids from `evidence`: {[s for s in strains if s not in ev] if isinstance(strains, list) else strains}")
    return errs


def _norm(p: str) -> str:
    return " ".join(str(p).split()).casefold()


def check_child(meta: dict, data: dict, answer, cfg: dict) -> list[str]:
    if not isinstance(answer, dict):
        return ["the answer is one JSON object"]
    errs = []
    name = str(answer.get("name") or "").strip()
    parents = [data["parent"]] + ([data["parent_b"]] if "parent_b" in data else [])
    if not name or _words(name) > 5:
        errs.append("name: at most 5 words")
    elif any(_norm(name) == _norm(p["name"]) for p in parents):
        errs.append("name: a new name, not the parent's")
    core = answer.get("core")
    written = [p for p in core if isinstance(p, str)] if isinstance(core, list) else []
    if not isinstance(core, list) or not 2 <= len(core) <= 4 or not all(isinstance(p, str) for p in core):
        errs.append("core: 2 to 4 propositions")
        core = []
    ids = [m.group(1) if (m := PROP_ID.match(p)) else None for p in core]
    if None in ids or len(set(ids)) != len(ids):
        errs.append("core: each proposition starts with its own id, e.g. \"J1: ...\"")
    if any(_words(p) > 55 for p in core):
        errs.append("core: each proposition at most 50 words")
    if not answer.get("heuristic") or _words(answer.get("heuristic")) > 25:
        errs.append("heuristic: at most 25 words")
    aux = answer.get("auxiliaries") or []
    if not isinstance(aux, list) or len(aux) > 4:
        errs.append("auxiliaries: at most 4")
        aux = []
    for a in aux:
        if not isinstance(a, dict) or not re.fullmatch(r"A\d+", str(a.get("id"))) or not a.get("statement") \
                or not a.get("consequence"):
            errs.append(f"auxiliary {a.get('id') if isinstance(a, dict) else a}: needs id (A1, ...), statement and "
                        "consequence")
    if len({a.get("id") for a in aux if isinstance(a, dict)}) != len(aux):
        errs.append("auxiliaries: each id once")
    if answer.get("level") not in cfg["archive"]["levels"]:
        errs.append(f"level must be one of {cfg['archive']['levels']}")
    if answer.get("mechanism") not in cfg["archive"]["mechanisms"]:
        errs.append(f"mechanism must be one of {cfg['archive']['mechanisms']}")
    text = " ".join([name, str(answer.get("heuristic") or ""), *written,
                     *[f"{a.get('statement', '')} {a.get('consequence', '')}" for a in aux if isinstance(a, dict)]])
    m = CITE.search(text)
    if m:
        errs.append(f"the programme must be general: no study names, years or finding numbers ({m.group(0)!r})")
    change = answer.get("change")
    kept = [{_norm(p) for p in par["core"]} for par in parents]
    mine = {_norm(p) for p in core}
    if meta["operator"] == "crossover":
        if change != "recombination":
            errs.append("change must be \"recombination\"")
        if not all(k & mine for k in kept):
            errs.append("recombination: take at least one hard-core proposition verbatim from each parent")
    elif change == "belt":
        if mine != kept[0] or len(core) != len(parents[0]["core"]):
            errs.append("change \"belt\" keeps the hard core verbatim")
    elif change == "core":
        same = len(kept[0] & mine)
        if same < len(parents[0]["core"]) - 1 or same == len(core) == len(parents[0]["core"]):
            errs.append("change \"core\" rewrites or replaces exactly one hard-core proposition and keeps the others "
                        "verbatim")
    else:
        errs.append("change must be \"belt\" or \"core\"")
    ev = {e["id"] for e in data["evidence"]}
    anomalies = answer.get("anomalies") or []
    if not isinstance(anomalies, list) or any(a not in ev for a in anomalies):
        errs.append("anomalies: ids of findings in `evidence`")
    if _words(answer.get("rationale")) > 100:
        errs.append("rationale: at most 80 words")
    return errs


def check_judge(meta: dict, data: dict, answer) -> list[str]:
    if not isinstance(answer, dict):
        return ["the answer is one JSON object"]
    keys = set(meta["mask"])
    if meta["mode"] == "rank":
        r = answer.get("ranking")
        if not isinstance(r, list) or sorted(r) != sorted(keys):
            return [f"ranking: every programme exactly once ({len(keys)})"]
        return []
    errs = []
    ch = answer.get("choices") or {}
    for k, members in data["islands"].items():
        if ch.get(k) not in members:
            errs.append(f"choices: {k} needs one of {members}")
    if data["recombine"]:
        pair = answer.get("recombine")
        where = {lid: k for k, members in data["islands"].items() for lid in members}
        if not isinstance(pair, list) or len(pair) != 2 or any(p not in where for p in pair) \
                or where.get(pair[0]) == where.get(pair[1]):
            errs.append("recombine: two programmes from different islands")
    return errs


def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions: str) -> dict:
    from .agents import _strip_fences, batch_files
    ensure(store)
    row = store.db.execute("SELECT * FROM evo_games WHERE game = ?", (meta["game"],)).fetchone()
    if row is None or row["config_sha256"] != meta["config_sha256"]:
        return {"stored": 0, "problems": ["this task belongs to a game whose pre-registered file is not this one"]}
    cfg = game_config(row["config"], meta["game"])
    cls = classes(cfg)
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
        kind = meta["kind"]
        errs = (check_probes(data, answer, meta.get("surnames") or {}, cls, per_finding(cfg), anonymous(cfg))
                if kind == "evo_probe" else
                check_predictions(meta, data, answer, cls) if kind == "evo_predict" else
                check_child(meta, data, answer, cfg) if kind == "evo_mutate" else check_judge(meta, data, answer))
        problems += [f"{result.name}: {e}" for e in errs]
        answers.append((batch, data, answer))
        raws.append(raw)
    if problems:
        return {"stored": 0, "problems": problems}
    raw_all = "\n".join(raws)
    call_id = store.log_call(meta["run_id"], "llm", meta["agent"], f"{instructions}\n\n[{meta['kind']} "
                             f"{meta['need']}, batches sha256 " + ", ".join(
                                 hashlib.sha256(b.read_bytes()).hexdigest() for b, _, _ in answers) + "]",
                             200, 0, raw_all.encode("utf-8"), {"agent": meta["agent"], "task": meta["kind"],
                                                              "game": meta["game"], "need": meta["need"]})
    sha = _sha(raw_all)
    game_id = meta["game"]
    stored = 0
    with store.db:
        if meta["kind"] == "evo_probe":
            coded = {r["finding_id"]: r["coded"] for r in store.db.execute(
                "SELECT finding_id, coded FROM evo_probes WHERE game = ?", (game_id,))}
            for _, _, answer in answers:
                for x in answer["probes"]:
                    f = x["finding"]
                    if x.get("skip"):
                        store.db.execute("UPDATE evo_probes SET status = 'excluded', note = ?, call_id = ? WHERE "
                                         "game = ? AND finding_id = ?", (f"skipped: {x['skip']}", call_id, game_id, f))
                        continue
                    parts = {k: str(x[k]).strip() for k in PARTS}
                    agree = x["answer"] == coded[f]
                    store.db.execute(
                        "UPDATE evo_probes SET status = ?, question = ?, parts = ?, read_as = ?, note = ?, call_id = ? "
                        "WHERE game = ? AND finding_id = ?",
                        ("ok" if agree else "excluded", question(parts), json.dumps(parts, ensure_ascii=False),
                         x["answer"], None if agree else f"the probe writer read the result as {x['answer']}, the "
                         f"extraction coded {coded[f]}", call_id, game_id, f))
                    stored += 1
                    more = x.get("more") or []
                    split_up = len({m["answer"] for m in more}) >= 2
                    for k, m in enumerate(more, 2):
                        sub = {**parts, **{p: str(m[p]).strip() for p in PARTS if str(m.get(p) or "").strip()}}
                        if not agree:
                            note = "the finding's own probe did not count"
                        elif coded[f] == "mixed":
                            note = None if split_up else "the further probes of a mixed finding show one result only"
                        else:
                            note = None if m["answer"] == coded[f] else (
                                f"read as {m['answer']}, but the extraction coded the whole finding {coded[f]}")
                        store.db.execute("INSERT OR REPLACE INTO evo_extra VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                         (game_id, f, k, "excluded" if note else "ok", question(sub),
                                          json.dumps(sub, ensure_ascii=False), m["answer"], note, call_id))
                        stored += 1
        elif meta["kind"] == "evo_predict":
            _, _, answer = answers[0]
            by = {}
            for x in answer["predictions"]:
                total = sum(x["p"].values())
                by[str(meta["keys"][x["probe"]])] = {"p": [x["p"][c] / total for c in cls], "basis": x["basis"],
                                                      "why": x.get("why"), **({"known": x["known"]} if "known" in x
                                                                              else {})}
            content = {"by_finding": by, "strains": sorted(set(answer.get("strains") or []))}
            if meta.get("chunks", 1) > 1:
                content.update(chunk=meta["chunk"], chunks=meta["chunks"])
            store.db.execute("INSERT INTO evo_predictions (game, lens_id, split, replicate, agent, folder, content, "
                             "sha256, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (game_id, meta["lens_id"], meta["split"], meta["replicate"], meta["agent"], folder.name,
                              json.dumps(content, ensure_ascii=False), sha, call_id, now()))
            stored = len(by)
        elif meta["kind"] == "evo_mutate":
            _, _, a = answers[0]
            card = {"name": a["name"].strip(), "core": [" ".join(p.split()) for p in a["core"]],
                    "heuristic": a["heuristic"].strip(),
                    "auxiliaries": [{"id": x["id"], "statement": x["statement"], "consequence": x["consequence"]}
                                    for x in a.get("auxiliaries") or []]}
            store.db.execute(
                "INSERT OR REPLACE INTO evo_lenses (game, lens_id, mode, arm, island, generation, operator, parents, "
                "card, level, mechanism, change, rationale, agent, call_id, sha256, created_at) "
                "VALUES (?, ?, 'lens', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (game_id, meta["child"], "shared" if meta["generation"] == 1 else meta["arm"], meta["island"],
                 meta["generation"], meta["operator"], json.dumps(meta["parents"]),
                 json.dumps(card, ensure_ascii=False), a["level"], a["mechanism"], a["change"],
                 json.dumps({"rationale": a.get("rationale"), "anomalies": a.get("anomalies") or []},
                            ensure_ascii=False), meta["agent"], call_id, sha, now()))
            stored = 1
        else:
            _, _, a = answers[0]
            mask = meta["mask"]
            if meta["mode"] == "rank":
                _event(store, game_id, "judge_rank", {"ranking": [mask[k] for k in a["ranking"]],
                                                      "reasons": {mask.get(k, k): v for k, v in
                                                                  (a.get("reasons") or {}).items()}})
            else:
                isl = meta["island_keys"]
                _event(store, game_id, "judge_parents", {
                    "arm": meta["arm"], "generation": meta["generation"],
                    "choices": {isl[k]: mask[v] for k, v in a["choices"].items() if k in isl},
                    "crossover": [mask[k] for k in a.get("recombine") or []] or None,
                    "reasons": a.get("reasons") or {}})
            stored = 1
        store.db.execute("UPDATE evo_tasks SET status = 'imported' WHERE game = ? AND need = ?", (game_id, meta["need"]))
    return {"stored": stored, "problems": []}


# Evaluation: code counts, no agent scores

def changed_after_import(store: Store, G: dict) -> list[str]:
    """Imported tasks whose answer files no longer match what was stored (an agent kept writing after the
    import): the stored answer is the one that counts, and the report names these."""
    calls = {}
    for r in store.db.execute("SELECT meta, response_sha256 FROM calls WHERE meta LIKE ? ORDER BY call_id",
                              (f'%"game": "{G["game"]}"%',)):
        m = json.loads(r["meta"] or "{}")
        if m.get("need"):
            calls[m["need"]] = r["response_sha256"]
    from .agents import batch_files
    out = []
    for need, t in tasks(store, G["game"]).items():
        folder = G["root"] / t["folder"]
        if t["status"] != "imported" or not folder.exists():
            continue
        results = [b.with_name(b.stem + "_result.json") for b in batch_files(folder)]
        raw = "\n".join(r.read_text(encoding="utf-8") for r in results if r.exists())
        if calls.get(need) and hashlib.sha256(raw.encode("utf-8")).hexdigest() != calls[need]:
            out.append(need)
    return sorted(out)


def _label(delta: float | None, floor: float, accommodated: bool) -> str:
    if delta is None:
        return "not scored"
    if delta > floor:
        return "progressive"
    if accommodated:
        return "degenerating"
    if delta < -floor:
        return "worse"
    return "no gain"


def group_brier(S: Scores, ids: list[str]) -> dict[str, float]:
    """Per probe, the mean Brier score of a group of lenses, on the probes every one of them forecast."""
    per = [S.probe_scores(i) for i in ids]
    per = [p for p in per if p]
    if not per:
        return {}
    if all(k.isdigit() for p in per for k in p):
        # one probe per finding: the order of the integer set the 0.1 games resampled in, so their reports reproduce
        common = [str(f) for f in set.intersection(*(set(int(k) for k in p) for p in per))]
    else:      # string keys: a fixed order, not the set's, which changes with every process
        common = sorted(set.intersection(*(set(p) for p in per)), key=lambda k: tuple(map(int, k.split("."))))
    return {f: _mean(p[f]["brier"] for p in per) for f in common}


def group_diffs(S: Scores, a_ids: list[str], b_ids: list[str]) -> dict[str, float]:
    """Per probe, how much better group a forecasts than group b (b's Brier minus a's)."""
    a, b = group_brier(S, a_ids), group_brier(S, b_ids)
    return {f: b[f] - a[f] for f in a if f in b}


def group_weights(S: Scores, a_ids: list[str], b_ids: list[str]) -> dict[str, float]:
    """Each lens's weight in group_diffs: a group's mean counts every scored lens of it equally."""
    w: dict[str, float] = {}
    for ids, sign in ((a_ids, -1.0), (b_ids, 1.0)):
        scored = [i for i in ids if S.probe_scores(i)]
        for i in scored:
            w[i] = w.get(i, 0.0) + sign / len(scored)
    return w


def comparison_groups(cfg: dict, arms: dict) -> dict[str, tuple]:
    """The pre-registered comparisons: each arm's final lenses against the seeds, the arms against each other, and
    the seeds against the three controls."""
    seeds, lenses_ = list(cfg["islands"]), {arm: arms[arm]["lenses"] for arm in ARMS}
    out = {f"{arm} vs seeds": (lenses_[arm], seeds, f"{arm}-seeds") for arm in ARMS}
    out["selected vs no_selection"] = (lenses_["selected"], lenses_["no_selection"], "s-n")
    out["selected vs judge"] = (lenses_["selected"], lenses_["judge"], "s-j")
    out["judge vs no_selection"] = (lenses_["judge"], lenses_["no_selection"], "j-n")
    out["seeds vs placebo"] = (seeds, [cfg["placebo"]], "seeds-p")
    out["seeds vs shuffled"] = (seeds, ["shuffled"], "seeds-sh")
    out["seeds vs no_card"] = (seeds, ["no_card"], "seeds-nc")
    return out


def evaluate(store: Store, run_id: int, root: Path = ROOT, game_id: str | None = None) -> dict:
    G = game(store, run_id, root, game_id, start=False)
    cfg, game_id = G["cfg"], G["game"]
    rows = lens_rows(store, game_id)
    pr = probes(store, game_id)
    opened = bool(events(store, game_id, "test_opened"))
    V = Scores(store, G, "validation")
    T = Scores(store, G, "test") if opened else None
    placebo = cfg["placebo"]
    floor = V.noise_floor(list(cfg["islands"]) + [placebo, "shuffled", "no_card"]) or 0.0
    replicated = isinstance(cfg.get("replicates"), dict)

    def floor_for(a: str, b: str) -> float:
        """The noise floor for a gap between two lenses' scores: with [replicates], each score averages its runs."""
        if not replicated:
            return floor
        ra, rb = (max(1, len(V.fc.get((x, "validation")) or [])) for x in (a, b))
        return floor * math.sqrt((1 / ra + 1 / rb) / 2)

    def card_of(lid):
        r = rows[lid]
        return {"lens": lid, "name": (r["card"] or {}).get("name") or lid, "arm": r["arm"], "island": r["island"],
                "generation": r["generation"], "operator": r["operator"], "parents": r["parents"],
                "change": r["change"], "level": r["level"], "mechanism": r["mechanism"], "aux": V.aux(lid)}

    def score_row(S: Scores, lid: str) -> dict | None:
        s = S.summary(lid)
        if s is None:
            return None
        g_p, g_f, g_b = S.gain(lid, placebo), S.gain(lid, "no_card"), S.gain(lid, "base_rate")
        out = {**s, "gain_placebo": g_p, "gain_no_card": g_f, "gain_base_rate": g_b, "strains": S.strains(lid),
               "fitness": S.fitness(lid) if S.split == "validation" else
               (None if g_p[0] is None or g_f[0] is None else min(g_p[0], g_f[0])
                - float(cfg["score"].get("aux_penalty", 0.005)) * S.aux(lid))}
        c = S.fc.get((lid, S.split)) or []
        if c and rows.get(lid, {}).get("mode") == "lens":
            bases = [b for x in c for v in x["by_finding"].values() for b in [v["basis"]]]
            out["lens_use"] = _mean(1.0 if any(b.startswith(("core:", "aux:")) for b in bs) else 0.0 for bs in bases)
        return out

    lens_ids = [lid for lid in rows if lid not in ("prior", "recall")]
    val = {lid: score_row(V, lid) for lid in lens_ids + ["base_rate"]}
    test = {lid: score_row(T, lid) for lid in lens_ids + ["base_rate"]} if T else {}

    # lineage with Lakatos labels, on validation
    lineage = []
    for lid, r in rows.items():
        if r["generation"] == 0:
            continue
        parent = max(r["parents"], key=lambda p: (V.fitness(p) if V.fitness(p) is not None else -9))
        a, b = V.probe_scores(lid), V.probe_scores(parent)
        delta = cluster_ci({f: b[f]["brier"] - a[f]["brier"] for f in a if f in b}, V.study, V.n,
                           f"{lid}-{parent}", V.run_terms({lid: -1.0, parent: 1.0})) if a and b else (None, None, None)
        acc_aux = V.aux(lid) > V.aux(parent)
        sp, sc = V.strains(parent), V.strains(lid)
        acc_in = sp is not None and sc is not None and sc < sp
        lineage.append({**card_of(lid), "compared_with": parent, "delta": delta,
                        "aux_added": V.aux(lid) - V.aux(parent), "strains_parent": sp, "strains_child": sc,
                        "label": _label(delta[0], floor_for(lid, parent), acc_aux or acc_in),
                        "rationale": (r["rationale"] or {}).get("rationale"),
                        "anomalies": (r["rationale"] or {}).get("anomalies")})

    # arms: final populations
    finals = final_children(cfg)
    arms = {}
    for arm, kids in finals.items():
        ok = [k for k in kids if k in rows]
        arms[arm] = {"lenses": ok,
                     "validation_fitness": _mean(v for k in ok if (v := (val.get(k) or {}).get("fitness")) is not None),
                     "test_fitness": _mean(v for k in ok if (v := (test.get(k) or {}).get("fitness")) is not None)
                     if T else None}
    rank = (events(store, game_id, "judge_rank") or [None])[-1]
    if rank:
        for arm in arms:
            mine = [lid for lid in rank["ranking"] if lid in arms[arm]["lenses"]]
            arms[arm]["judge_pick"] = mine[0] if mine else None
    for arm in arms:
        ok = arms[arm]["lenses"]
        if arm == "selected":
            fits = {k: (val.get(k) or {}).get("fitness") for k in ok}
            arms[arm]["champion"] = max((k for k in ok if fits[k] is not None), key=lambda k: fits[k], default=None)
        elif arm == "no_selection":
            arms[arm]["champion"] = _rng(game_id, cfg["seed"], "champion").choice(ok) if ok else None
        else:
            arms[arm]["champion"] = arms[arm].get("judge_pick")

    seeds = list(cfg["islands"])
    comparisons = {}
    for S, name in ((V, "validation"), (T, "test")):
        if S is None:
            continue
        c = {}
        for k, (a, b, tag) in comparison_groups(cfg, arms).items():
            c[k] = cluster_ci(group_diffs(S, a, b), S.study, S.n, f"{tag}-{name}", S.run_terms(group_weights(S, a, b)))
        comparisons[name] = c

    # does validation fitness carry to test; does the judge's taste track either
    tested = [lid for lid in lens_ids if (test.get(lid) or {}).get("fitness") is not None] if T else []
    transfer = spearman([val[l]["fitness"] for l in tested], [test[l]["fitness"] for l in tested]) if tested else None
    judge = None
    if rank:
        order = {lid: i for i, lid in enumerate(rank["ranking"])}
        on_v = [l for l in order if (val.get(l) or {}).get("fitness") is not None]
        on_t = [l for l in order if (test.get(l) or {}).get("fitness") is not None]
        judge = {"ranking": rank["ranking"], "reasons": rank.get("reasons") or {},
                 "vs_validation": spearman([-order[l] for l in on_v], [val[l]["fitness"] for l in on_v]),
                 "vs_test": spearman([-order[l] for l in on_t], [test[l]["fitness"] for l in on_t]) if on_t else None,
                 "placebo_rank": order.get(placebo), "shuffled_rank": order.get("shuffled"), "n": len(order)}

    # contamination: the recall probe
    contamination = {}
    for split in ("validation", "test"):
        if split == "test" and not opened:
            continue
        ans = {k: V.cls.index(t["answer"]) for k, t in targets(store, game_id, split).items()}
        fc = forecasts(store, game_id)
        row = {}
        for mode in ("prior", "recall"):
            c = fc.get((mode, "contamination"))
            if c:
                s = per_probe(c, ans, V.cls, V.floor)
                row[mode] = {"brier": _mean(v["brier"] for v in s.values()), "hit": _mean(v["hit"] for v in s.values()),
                             "n": len(s)}
                if mode == "recall":
                    known = [c[0]["by_finding"][str(f)].get("known") for f in ans if str(f) in c[0]["by_finding"]]
                    row[mode]["known"] = sum(1 for k in known if k)
                    kn = {f: v for f, v in s.items() if c[0]["by_finding"][str(f)].get("known")}
                    row[mode]["hit_known"] = _mean(v["hit"] for v in kn.values())
        if "prior" in row and "recall" in row:
            sp_, sr = per_probe(fc[("prior", "contamination")], ans, V.cls, V.floor), \
                per_probe(fc[("recall", "contamination")], ans, V.cls, V.floor)
            row["recall_gain"] = cluster_ci({f: sp_[f]["brier"] - sr[f]["brier"] for f in sr if f in sp_}, V.study,
                                            V.n, f"recall-{split}")
        contamination[split] = row

    # diversity: what the lenses say (signatures on validation), and the archive
    sig = {lid: V.signature(lid) for lid in lens_ids if rows[lid]["mode"] == "lens"}
    sig = {k: v for k, v in sig.items() if v}

    def spread(ids):
        ids = [i for i in ids if i in sig]
        pairs = [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]]
        return _mean(sum(x != y for x, y in zip(sig[a], sig[b])) / max(1, len(sig[a])) for a, b in pairs)

    diversity = {"seeds": spread(seeds), "generation 1": spread([c for c in expected(cfg, "shared", 1)]),
                 **{f"{arm} final": spread(arms[arm]["lenses"]) for arm in ARMS},
                 "seeds vs placebo": _mean(sum(x != y for x, y in zip(sig[s], sig[placebo])) / max(1, len(sig[s]))
                                           for s in seeds if s in sig and placebo in sig)}
    archive = {}
    for lid, r in rows.items():
        if r["mode"] == "lens" and r["level"] and lid != placebo:
            cell = f"{r['level']} / {r['mechanism']}"
            f = (val.get(lid) or {}).get("fitness")
            archive.setdefault(cell, []).append((lid, f))
    counts = Counter(p["status"] for p in pr.values())
    split_counts = {s: {"studies": len({p["work_id"] for p in pr.values() if p["split"] == s}),
                        "findings": sum(1 for p in pr.values() if p["split"] == s),
                        "probes": len(targets(store, game_id, s)),
                        "further": sum(1 for k in targets(store, game_id, s) if "." in k)}
                    for s in ("development", "validation", "test")}
    excluded = [{"finding": f, "split": p["split"], "note": p["note"]} for f, p in pr.items() if p["status"] == "excluded"]
    further = Counter(r["note"] or "counted" for r in store.db.execute(
        "SELECT note FROM evo_extra WHERE game = ?", (game_id,)))
    calls = Counter(t["kind"] for t in tasks(store, game_id).values() if t["status"] == "imported")
    return {"game": game_id, "corpus": G["corpus"], "config_path": G["path"], "config_sha256": G["sha"], "cfg": cfg,
            "test_opened": opened, "noise_floor": floor,
            "noise_floor_scores": floor / math.sqrt(reps(cfg, "children")) if replicated else None,
            "splits": split_counts, "probe_status": dict(counts), "excluded": excluded, "further": dict(further),
            "cards": {lid: card_of(lid) for lid in rows}, "texts": {lid: rows[lid]["card"] for lid in rows},
            "validation": val, "test": test, "lineage": lineage, "arms": arms, "comparisons": comparisons,
            "transfer": transfer, "judge": judge, "contamination": contamination, "diversity": diversity,
            "archive": archive, "calls": dict(calls), "base_rates": {"validation": V.base, "test": T.base if T else None},
            "choices": events(store, game_id, "parent_chosen"), "changed_after_import": changed_after_import(store, G),
            "judge_parents": events(store, game_id, "judge_parents")}


def pool_file(root: Path, pool_id: str) -> tuple[Path, str, dict]:
    for path in _files(root):
        text = path.read_text(encoding="utf-8")
        cfg = tomllib.loads(text)
        if cfg.get("parts") and cfg.get("game") == pool_id:
            return path, text, cfg
    raise ValueError(f"no pooled game {pool_id!r} in protocol/evolution_corpus_*.toml")


def evaluate_pool(store: Store, pool_id: str, root: Path = ROOT) -> dict:
    """The games of a pooled file together. Each comparison pools the per-probe differences of every game and resamples
    whole studies; validation fitness is set against test fitness across every tested lens of every game."""
    path, text, cfg = pool_file(root, pool_id)
    results, diffs, study, runs = [], {}, {}, {}
    for p in cfg["parts"]:
        r = evaluate(store, p["run"], root, p["game"])
        results.append(r)
        G = game(store, p["run"], root, p["game"], start=False)
        for split in ("validation", "test"):
            if split == "test" and not r["test_opened"]:
                continue
            S = Scores(store, G, split)
            for k, (a, b, _) in comparison_groups(G["cfg"], r["arms"]).items():
                for f, d in group_diffs(S, a, b).items():
                    diffs.setdefault(split, {}).setdefault(k, {})[f"{p['game']}:{f}"] = d
                    study[f"{p['game']}:{f}"] = S.study[f]
                # each game's lenses keep runs of their own, under the pooled probe keys
                runs.setdefault(split, {}).setdefault(k, []).extend(
                    (w, [{f"{p['game']}:{f}": x for f, x in run.items()} for run in rs])
                    for w, rs in S.run_terms(group_weights(S, a, b)))
    n = int(cfg["score"].get("bootstrap", 2000))
    comparisons = {split: {k: cluster_ci(d, study, n, f"pool-{k}-{split}", runs[split][k]) for k, d in by.items()}
                   for split, by in diffs.items()}
    pairs = [(r["validation"][lid]["fitness"], r["test"][lid]["fitness"]) for r in results for lid in r["test"]
             if lid != "base_rate" and (r["test"][lid] or {}).get("fitness") is not None
             and (r["validation"].get(lid) or {}).get("fitness") is not None]
    return {"pool": pool_id, "config": path.relative_to(root).as_posix(), "config_sha256": _sha(text),
            "games": [r["game"] for r in results], "results": results, "comparisons": comparisons,
            "probes": {split: sum(r["splits"][split]["probes"] for r in results) for split in ("validation", "test")},
            "transfer": spearman([a for a, _ in pairs], [b for _, b in pairs]) if pairs else None, "tested": len(pairs)}


def pool_report(result: dict) -> str:
    L = [f"# Lens evolution, pooled game {result['pool']}: {', '.join(result['games'])}", "",
         f"Generated by `python -m ichnos evolve --game {result['pool']} --report` from the evidence store. "
         f"Pre-registered settings: {result['config']}, sha256 `{result['config_sha256'][:16]}…`. Each game has its "
         "own report; this one pools them.", "",
         f"Probes: {result['probes']['validation']} on validation, {result['probes']['test']} on test. Each comparison "
         "below pools the per-probe differences of every game (positive = the first group forecasts better), with 95% "
         "intervals from resampling whole studies"
         + (" and each lens's runs." if any(isinstance(r["cfg"].get("replicates"), dict) for r in result["results"])
            else "."), ""]
    for split, c in result["comparisons"].items():
        L += [f"## {split.capitalize()}", "", *[f"- {k}: {_ci(t)}" for k, t in c.items()], ""]
    if result["transfer"] is not None:
        L += [f"Validation fitness against test fitness across the {result['tested']} tested lenses (Spearman): "
              f"{_f(result['transfer'], 2)}.", ""]
    return "\n".join(L)


# Planning: what a game would cost and could show, before it begins

def schedule_calls(cfg: dict, n_val: int, n_test: int, n_asked: int) -> dict[str, Counter]:
    """The agent tasks step() writes, by stage and kind, if every answer is imported: for n_val validation and n_test
    test probes, and n_asked findings sent to the probe writer."""
    kv, kt, kc = chunks(cfg, n_val), chunks(cfg, n_test), chunks(cfg, n_val + n_test)
    out = {"probes": Counter(evo_probe=math.ceil(n_asked / (PROBE_BATCH * PROBE_TASK))),
           "generation 0": Counter(evo_predict=(len(cfg["islands"]) + 3) * reps(cfg, "seeds") * kv + 2 * kc)}
    for gen in range(1, generations(cfg) + 1):
        kids = sum(len(expected(cfg, arm, gen)) for arm in (["shared"] if gen == 1 else ARMS))
        out[f"generation {gen}"] = Counter(evo_mutate=kids, evo_predict=kids * reps(cfg, "children") * kv,
                                           evo_judge=0 if gen == 1 else 1)
    out["test"] = Counter(evo_predict=len(test_set(cfg)) * reps(cfg, "test") * kt, evo_judge=1)
    return out


def _phi(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def measured(store: Store, G: dict) -> dict | None:
    """PILOT's variances measured on a game's own generation 0 (validation, every seed and control run at least
    twice), or None before that: the noise of one probe between runs, the shift of a whole task, and the spread of
    the seeds' true gains over the placebo and the no-lens forecaster."""
    S = Scores(store, G, "validation")
    ids = list(G["cfg"]["islands"]) + [G["cfg"]["placebo"], "shuffled", "no_card"]
    runs = {lid: S.fc.get((lid, "validation")) or [] for lid in ids}
    probes_ = sorted(S.answers)
    if not probes_ or any(len(c) < 2 for c in runs.values()):
        return None
    var = lambda xs: sum((x - _mean(xs)) ** 2 for x in xs) / (len(xs) - 1)
    within, between, mean_of = [], [], {}
    for lid, content in runs.items():
        per = [[brier(c["by_finding"][f]["p"], S.answers[f]) for f in probes_] for c in content]
        within += [var([p[j] for p in per]) for j in range(len(probes_))]
        between.append(var([_mean(p) for p in per]))
        mean_of[lid] = [_mean(p[j] for p in per) for j in range(len(probes_))]
    s_probe2, n, k = _mean(within), len(probes_), chunks(G["cfg"], len(probes_))
    s_run2 = max(0.0, (_mean(between) - s_probe2 / n) * k)
    r = len(runs[ids[0]])
    gains = [var([mean_of[ref][j] - mean_of[lid][j] for j in range(n)]) for lid in G["cfg"]["islands"]
             for ref in (G["cfg"]["placebo"], "no_card")]
    s_true2 = max(0.0, _mean(gains) - 2 * s_probe2 / r)
    return {**PILOT, "sigma_probe": math.sqrt(s_probe2), "sigma_run": math.sqrt(s_run2),
            "sigma_true": math.sqrt(s_true2), "source": f"measured on generation 0 of {G['game']} ({n} probes)"}


def power(delta: float, n: float, deff: float, ra: int, rb: int, ka: int = 1, kb: int = 1, lenses: int = 1,
          v: dict | None = None) -> dict:
    """Power of a two-sided test at 5% for a mean Brier difference delta between two groups of `lenses` lenses, each
    run ra and rb times in ka and kb tasks per run, on n probes with design effect deff (variances v, by default
    PILOT's). The run-level part does not shrink with probes, only with runs and tasks."""
    s = v or PILOT
    var_probe = s["sigma_true"] ** 2 + s["sigma_probe"] ** 2 * (1 / ra + 1 / rb) / lenses
    var_run = s["sigma_run"] ** 2 * (1 / (ra * ka) + 1 / (rb * kb)) / lenses
    se = math.sqrt(var_probe * deff / n + var_run) if n else float("inf")
    room = (delta / (1.959964 + 0.841621)) ** 2 - var_run
    return {"power": (_phi(delta / se - 1.959964) if se else 1.0) if n else 0.0, "se": se,
            "n_for_80": math.ceil(var_probe * deff / room) if room > 0 else None}


def deff(per_study: float, per_finding_: float = 1.0) -> float:
    """Design effect of probes nested in findings nested in studies."""
    return 1 + (per_finding_ - 1) * PILOT["icc_finding"] + max(0.0, per_study - per_finding_) * PILOT["icc_study"]


def plan(store: Store, root: Path = ROOT, game_id: str | None = None, run_id: int | None = None) -> dict:
    """Every game of a pre-registered file, or one game, as it would be played: the split, the probes to expect, the
    power they give, the noise floor and the agent calls. Reads only; writes nothing and begins no game."""
    if game_id:
        hits = []
        for path in _files(root):
            text = path.read_text(encoding="utf-8")
            cfg = tomllib.loads(text)
            if cfg.get("parts"):
                hits += [(path, text, part(cfg, p["game"])) for p in cfg["parts"]
                         if game_id in (cfg["game"], p["game"])]
            elif cfg.get("game") == game_id:
                if run_id is None:
                    raise ValueError(f"game {game_id} names no run: give the run, `evolve RUN --game {game_id} --plan`")
                hits.append((path, text, cfg | {"run": run_id}))
        if not hits:
            raise ValueError(f"no pre-registered game {game_id!r} in protocol/evolution_corpus_*.toml")
    else:
        if run_id is None:
            raise ValueError("give a run or a game")
        corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
        path, text, cfg = load(corpus, root, None, run_id)
        hits = [(path, text, cfg | {"run": cfg.get("run") or run_id})]
    parts = [_plan_part(store, root, *h) for h in hits]
    total = Counter()
    for p in parts:
        for stage in p["calls"].values():
            total.update(stage)
    return {"games": parts, "calls": dict(total), "calls_high": dict(sum((Counter(p["calls_high_total"])
                                                                          for p in parts), Counter())),
            "pooled_test_probes": sum(p["probes"]["test"]["expected"] for p in parts),
            "pooled_validation_probes": sum(p["probes"]["validation"]["expected"] for p in parts)}


def _plan_part(store: Store, root: Path, path: Path, text: str, cfg: dict) -> dict:
    run_id = cfg["run"]
    row = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None:
        raise ValueError(f"no run {run_id}")
    _, _, lens_cfg = lenses.load(row["corpus"], root)
    check_config(cfg, lens_cfg)
    rows = tables.finding_rows(store, run_id)
    meta = tables.works(store, {r["work_id"] for r in rows})
    per_study = Counter(r["work_id"] for r in rows)
    sp = cfg["split"]
    begun = bool(store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'evo_games'").fetchone()
                 and store.db.execute("SELECT 1 FROM evo_games WHERE game = ?", (cfg["game"],)).fetchone())
    v = None
    if begun:
        v = measured(store, {"game": cfg["game"], "cfg": cfg, "run_id": run_id})
    v = v or {**PILOT, "source": "the v0 pilot (evo-a1)"}
    dated, info = {}, {}
    if sp.get("test") == "after_cutoff":
        cand = date_candidates(cfg, meta)
        dated = dates(store, cand)
        info = {"cutoff": sp["cutoff"], "candidates": len(cand),
                "missing": sum(1 for w in cand if w not in dated or dated[w]["status"] == "error"),
                "undated": sum(1 for w in cand if w in dated and not dated[w]["published"]),
                "after": sum(1 for w in cand if ((dated.get(w) or {}).get("published") or "") > sp["cutoff"])}
    split = assign_splits(cfg, cfg["game"], per_study, meta, dated)
    coded = {r["finding_id"]: coded_of(json.loads(r["fields"] or "{}"), cfg) for r in rows}
    by = {s: [r for r in rows if split[r["work_id"]] == s] for s in ("development", "validation", "test")}
    n = per_finding(cfg)
    counts, probes_, powers, floors = {}, {}, {}, {}
    for s, rs in by.items():
        asked = [r for r in rs if coded[r["finding_id"]]]
        studies = len({r["work_id"] for r in rs})
        counts[s] = {"studies": studies, "findings": len(rs), "coded": len(asked)}
        if s == "development":
            continue
        low = round(len(asked) * PILOT["yield"])
        probes_[s] = {"expected": low, "with_all_further": low * n,
                      "studies_with_probes": len({r["work_id"] for r in asked})}
        if not low:
            continue
        m = low / max(1, probes_[s]["studies_with_probes"])
        d = deff(m)
        kv = chunks(cfg, low)
        child, seed, test = reps(cfg, "children"), reps(cfg, "seeds"), reps(cfg, "test")
        if s == "validation":
            powers[s] = {f"{delta}": power(delta, low, d, child, seed, kv, kv, v=v) for delta in (0.02, 0.03)}
        else:
            kids = len(expected(cfg, "selected", generations(cfg))) if generations(cfg) > 1 else len(cfg["islands"])
            powers[s] = {f"{delta}": power(delta, low, d, test, test, kv, kv, v=v) for delta in (0.02, 0.03)}
            powers["arms on test"] = {f"{delta}": power(delta, low, d, test, test, kv, kv, lenses=kids, v=v)
                                      for delta in (0.02, 0.03)}
        floors[s] = {"one run": 2 / math.sqrt(math.pi) * math.sqrt(v["sigma_run"] ** 2 / kv
                                                                   + v["sigma_probe"] ** 2 / max(1, low)),
                     "deff": d}
        floors[s]["child score"] = floors[s]["one run"] / math.sqrt(child)
    n_val, n_test = probes_["validation"]["expected"], probes_["test"]["expected"]
    asked = counts["validation"]["coded"] + counts["test"]["coded"]
    calls = schedule_calls(cfg, n_val, n_test, asked)
    high = schedule_calls(cfg, n_val * n, n_test * n, asked)
    G = {"run_id": run_id, "cfg": cfg}
    dev_ids = [r["finding_id"] for r in by["development"]]
    seen_v = shown(cfg, cfg["game"], dev_ids)
    seen_t = shown(cfg, cfg["game"], dev_ids + [r["finding_id"] for r in by["validation"]])
    size = lambda ids: len(json.dumps(_evidence(store, G, ids), ensure_ascii=False)) if ids else 0
    ev_v, ev_t = size(seen_v), size(seen_t)
    per_task = lambda n_: math.ceil(n_ / chunks(cfg, n_)) if n_ else 0      # ~300 characters a probe
    tokens = {"forecast on validation": (ev_v + 4000 + 300 * per_task(n_val)) // 4,
              "forecast on test": (ev_t + 4000 + 300 * per_task(n_test)) // 4,
              "mutation or judge": (ev_v + 12000) // 4}
    return {"game": cfg["game"], "pool": cfg.get("pool"), "corpus": row["corpus"], "run": run_id,
            "config": path.relative_to(root).as_posix(), "config_sha256": _sha(text),
            "begun": begun, "variance": {k: v[k] for k in ("sigma_probe", "sigma_run", "sigma_true", "source")},
            "split": counts, "dates": info, "probes": probes_, "power": powers, "floor": floors,
            "replicates": {k: reps(cfg, k) for k in ("seeds", "children", "test")},
            "chunks": {"validation": chunks(cfg, n_val), "test": chunks(cfg, n_test)},
            "calls": {k: dict(v) for k, v in calls.items()},
            "calls_total": dict(sum(calls.values(), Counter())),
            "calls_high_total": dict(sum(high.values(), Counter())), "tokens_in": tokens,
            "evidence": {"validation": [len(seen_v), len(dev_ids)],
                         "test": [len(seen_t), len(dev_ids) + len(by["validation"])]},
            "aux_penalty": float(cfg["score"].get("aux_penalty", 0.005))}


def plan_text(p: dict) -> str:
    """The plan as the command line prints it."""
    L = []
    for g in p["games"]:
        L += [f"Game {g['game']}" + (f" (pool {g['pool']})" if g.get("pool") else "") + f": corpus {g['corpus']}, run "
              f"{g['run']}, {g['config']} sha256 {g['config_sha256'][:16]}… "
              + ("ALREADY BEGUN" if g["begun"] else "not begun"), ""]
        if g["dates"]:
            d = g["dates"]
            L.append(f"  Test = first published after {d['cutoff']}: {d['after']} of {d['candidates']} candidate studies"
                     + (f"; {d['missing']} still without a date (fetch with --dates)" if d["missing"] else "")
                     + (f"; {d['undated']} without any date in Crossref (never test)" if d["undated"] else ""))
        for s, c in g["split"].items():
            pr = g["probes"].get(s)
            L.append(f"  {s:<12} {c['studies']:>4} studies {c['findings']:>5} findings {c['coded']:>5} with a coded result"
                     + (f"  -> ~{pr['expected']} probes ({pr['with_all_further']} if every finding gave all its "
                        "further probes)" if pr else "  (evidence)"))
        L.append(f"  Replicates: {g['replicates']}; tasks per forecast: {g['chunks']}; aux penalty {g['aux_penalty']}")
        va = g["variance"]
        L.append(f"  Noise model, from {va['source']}: one probe between runs SD {va['sigma_probe']:.3f}, a whole task "
                 f"{va['sigma_run']:.3f}, true gains across probes {va['sigma_true']:.3f}")
        L.append("  Evidence shown: " + "; ".join(f"{s} forecasts {a} of {b} findings" for s, (a, b)
                                                  in g["evidence"].items()))
        for s, f in g["floor"].items():
            L.append(f"  Noise floor on {s}: {f['one run']:.3f} between two runs, {f['child score']:.3f} between two "
                     f"scores of a child (design effect {f['deff']:.2f})")
        for what, by_delta in g["power"].items():
            L.append(f"  Power, {what}: " + "; ".join(
                f"{d} Brier: {x['power']:.0%} (SE {x['se']:.4f}; 80% needs "
                + (f"{x['n_for_80']} probes)" if x["n_for_80"] else "more runs, not probes)") for d, x in by_delta.items()))
        L.append("  Calls: " + "; ".join(f"{stage} " + ", ".join(f"{k} {v}" for k, v in sorted(c.items()) if v)
                                         for stage, c in g["calls"].items()))
        L.append("  Total: " + ", ".join(f"{k} {v}" for k, v in sorted(g["calls_total"].items()))
                 + f" = {sum(g['calls_total'].values())} tasks (up to {sum(g['calls_high_total'].values())} if every "
                 "finding gave all its further probes)")
        L.append("  Input per task, about: " + ", ".join(f"{k} {v:,} tokens" for k, v in g["tokens_in"].items()))
        L.append("")
    if len(p["games"]) > 1:
        L.append(f"Pooled: ~{p['pooled_validation_probes']} validation and ~{p['pooled_test_probes']} test probes; "
                 + ", ".join(f"{k} {v}" for k, v in sorted(p["calls"].items()))
                 + f" = {sum(p['calls'].values())} tasks (up to {sum(p['calls_high'].values())}).")
    return "\n".join(L)


# The report

def _f(x, d=3, sign=True):
    if x is None:
        return "–"
    return f"{x:+.{d}f}" if sign else f"{x:.{d}f}"


def _ci(t):
    m, lo, hi = t if t else (None, None, None)
    if m is None:
        return "–"
    return _f(m) + (f" [{_f(lo)}, {_f(hi)}]" if lo is not None else "")


def report(result: dict, store: Store, run_id: int) -> str:
    cfg, cards = result["cfg"], result["cards"]
    name = lambda lid: cards[lid]["name"] if lid in cards else lid
    sp = result["splits"]
    corpus = result.get("corpus", "A")
    own = result.get("config_path", f"protocol/evolution_corpus_{corpus.lower()}.toml")
    test_rule = (f"first published after {cfg['split']['cutoff']}, the forecaster's training cutoff"
                 if cfg["split"].get("test") == "after_cutoff" else f"from {cfg['split']['test_from_year']} on")
    game_flag = "" if own == f"protocol/evolution_corpus_{corpus.lower()}.toml" else f" --game {result['game']}"
    L = [f"# Lens evolution {cfg.get('version', '')}: corpus {corpus}, game {result['game']}", "",
         f"Generated by `python -m ichnos evolve {run_id}{game_flag} --report` from the evidence store. Every "
         "number below is computed by code from the stored forecasts; no agent scored anything. Pre-registered "
         f"settings: {own}, sha256 `{result['config_sha256'][:16]}…`.", "",
         "## The game", "",
         f"Findings are split by study. Development: {sp['development']['studies']} studies, "
         f"{sp['development']['findings']} findings, whose results every agent sees. Validation: "
         f"{sp['validation']['studies']} studies, {sp['validation']['probes']} probes, the selection signal. Test: "
         f"{sp['test']['studies']} studies {test_rule}, {sp['test']['probes']} probes, "
         f"sealed until evolution ended ({'opened' if result['test_opened'] else 'still sealed'}).", "",
         f"A probe asks for one finding's result (positive, negative, null, mixed) without giving it. "
         f"{len(result['excluded'])} findings were left out of the game:", ""]
    if len(result["excluded"]) <= 40:
        for x in result["excluded"]:
            L.append(f"- finding {x['finding']} ({x['split']}): {x['note']}")
    else:
        reasons = Counter(re.sub(r"skipped: .*", "skipped by the probe writer", x["note"] or "")
                          for x in result["excluded"])
        L += [f"- {n}: {why}" for why, n in reasons.most_common()]
    if result.get("further"):
        n_ok = (result["further"] or {}).get("counted", 0)
        L += ["", f"Further probes (another outcome or subgroup of a finding): {n_ok} counted"
              + "".join(f"; {n} not, because {why}" for why, n in result["further"].items() if why != "counted") + "."]
    L += ["", f"Score: Brier (0 is perfect; always saying 25% to each result scores 0.75). Fitness = the smaller of the "
          f"gains over the placebo lens and over the no-lens forecaster, minus {cfg['score']['aux_penalty']} per "
          "auxiliary assumption. Gains are in Brier units (positive = better than the reference), with 95% intervals "
          "from resampling whole studies"
          + (" and, for a lens with several runs, its runs, so that a shift of a whole forecast task counts."
             if isinstance(cfg.get("replicates"), dict) else "."), "",
          f"Noise floor: two runs of the same lens differ by {_f(result['noise_floor'], sign=False)} in mean Brier on "
          "average. A difference smaller than that is not evidence of anything."
          + (f" A child's score averages {reps(cfg, 'children')} runs, so two scores of the same child would differ by "
             f"about {_f(result['noise_floor_scores'], sign=False)}; the labels use the floor for the runs each "
             "score averages." if result.get("noise_floor_scores") is not None else ""), "",
          "## Generation 0: seeds and controls on validation", "",
          "| Lens | Brier | Hit rate | Gain over placebo | Gain over no lens | Gain over base rate | Fitness | Uses its core |",
          "| --- | ---: | ---: | --- | --- | --- | ---: | ---: |"]
    v = result["validation"]
    for lid in list(cfg["islands"]) + [cfg["placebo"], "shuffled", "no_card", "base_rate"]:
        s = v.get(lid)
        if not s:
            continue
        use = s.get("lens_use")
        L.append(f"| {name(lid) if lid != 'base_rate' else 'Base rate (code)'} | {_f(s['brier'], sign=False)} | "
                 f"{s['hit']:.0%} | {_ci(s['gain_placebo'])} | {_ci(s['gain_no_card'])} | {_ci(s['gain_base_rate'])} | "
                 f"{_f(s['fitness'])} | {'' if use is None else f'{use:.0%}'} |")
    L += ["", "## Lineage", "",
          "Each child against its parent on validation (for a recombination, the fitter parent). Δ is the parent's "
          "Brier minus the child's (positive = the child forecasts better). Labels, after Lakatos: progressive = Δ "
          "above the noise floor; degenerating = no such gain while the child added auxiliaries or claims fewer "
          "anomalies in the evidence it saw; worse = Δ below minus the noise floor; no gain = within the noise.", "",
          "| Child | Arm | From | Change | Level / mechanism | Δ held-out | Aux. added | Anomalies (parent → child) | Label | Fitness |",
          "| --- | --- | --- | --- | --- | --- | ---: | --- | --- | ---: |"]
    for x in sorted(result["lineage"], key=lambda x: (x["generation"], x["arm"], x["lens"])):
        L.append(f"| {x['lens']}: {x['name']} | {x['arm']} | {', '.join(x['parents'])} | {x['change']} | "
                 f"{x['level']} / {x['mechanism']} | {_ci(x['delta'])} | {x['aux_added']:+d} | "
                 f"{_f(x['strains_parent'], 0, False)} → {_f(x['strains_child'], 0, False)} | {x['label']} | "
                 f"{_f((v.get(x['lens']) or {}).get('fitness'))} |")
    labels = Counter(x["label"] for x in result["lineage"])
    L += ["", "Labels: " + ", ".join(f"{k} {n}" for k, n in labels.most_common()), "",
          "Parent choices from generation 2 on:", ""]
    for c in result["choices"]:
        if c["generation"] >= 2:
            par = c["parent"] if isinstance(c["parent"], str) else " + ".join(c["parent"])
            L.append(f"- {c['arm']}, generation {c['generation']}, {c['island']}: {par}")
    L += ["", "## Arms", "",
          "| Arm | Final lenses | Mean validation fitness | Mean test fitness | Champion |", "| --- | --- | ---: | ---: | --- |"]
    for arm, a in result["arms"].items():
        L.append(f"| {arm} | {', '.join(a['lenses'])} | {_f(a['validation_fitness'])} | {_f(a['test_fitness'])} | "
                 f"{a.get('champion') or '–'} |")
    for split in ("validation", "test"):
        c = result["comparisons"].get(split)
        if not c:
            continue
        L += ["", f"Comparisons on {split} (mean Brier of the first group's lenses against the second's, per probe; "
              "positive = the first forecasts better):", ""]
        L += [f"- {k}: {_ci(t)}" for k, t in c.items()]
    if result["test_opened"]:
        L += ["", f"Validation fitness against test fitness across the {len([l for l in result['test'] if result['test'][l] and result['test'][l].get('fitness') is not None])} "
              f"tested lenses (Spearman): {_f(result['transfer'], 2)}."]
    j = result["judge"]
    if j:
        L += ["", "## The LLM judge against the held-out score", "",
              f"A judge of another tier ranked all {j['n']} programmes blind, without scores. Spearman of its ranking "
              f"with validation fitness {_f(j['vs_validation'], 2)}, with test fitness {_f(j['vs_test'], 2)}. It put "
              f"the placebo at {j['placebo_rank'] + 1 if j['placebo_rank'] is not None else '–'} and the shuffled lens "
              f"at {j['shuffled_rank'] + 1 if j['shuffled_rank'] is not None else '–'} of {j['n']}.", "",
              "Its ranking: " + ", ".join(f"{i + 1}. {l}" for i, l in enumerate(j["ranking"]))]
    L += ["", "## Contamination: the recall probe", "",
          "The same forecaster, with neither evidence nor lens, once with the study named (authors, year, venue) "
          "and once without. If naming the study helps, the model may remember the results, and held-out forecasts "
          "would be partly memory.", ""]
    for split, row in result["contamination"].items():
        p, r = row.get("prior"), row.get("recall")
        if p and r:
            L.append(f"- {split}: unnamed Brier {_f(p['brier'], sign=False)} (hit {p['hit']:.0%}), named "
                     f"{_f(r['brier'], sign=False)} (hit {r['hit']:.0%}); gain from naming {_ci(row.get('recall_gain'))}; "
                     f"it claimed to know {r['known']} of {r['n']} studies' results"
                     + (f", and was right on {r['hit_known']:.0%} of those." if r.get("hit_known") is not None else "."))
    d = result["diversity"]
    L += ["", "## Diversity", "",
          "Share of validation probes on which two lenses forecast different most-likely results, averaged over pairs "
          "(FunSearch's score signature):", ""]
    L += [f"- {k}: {_f(x, 2, False)}" for k, x in d.items()]
    L += ["", "Archive (level of explanation / mechanism family; validation fitness):", ""]
    for cell, members in sorted(result["archive"].items()):
        L.append(f"- {cell}: " + ", ".join(f"{lid} ({_f(f)})" for lid, f in members))
    L += ["", "## What this pilot can and cannot show", "",
          f"- Power. Validation has {sp['validation']['probes']} probes from {sp['validation']['studies']} studies and "
          f"test {sp['test']['probes']} from {sp['test']['studies']}. Intervals that resample studies are wide; the "
          "pilot measures the noise and the size of differences, so that a full run can be sized. It cannot show that "
          "one arm is better.",
          f"- One seed, {cfg['schedule']['generations']} generations, one child per island per generation. The "
          "published successes ran thousands to millions of evaluations.",
          "- One model family. The islands differ in their seed, not in the model, so the diversity that different "
          "model families would add is not tested.",
          "- The forecaster is an LLM applying a lens, so a lens's score mixes its content with how well the forecaster "
          "uses it ('Uses its core' shows how often it cited the core or belt).",
          "- The coding is the target. A lens can fit quirks of the extraction's sign coding; probes count only where "
          "a second agent read the same result, which removes some but not all of this.", "",
          "## Calls", "", ", ".join(f"{k} {n}" for k, n in sorted(result["calls"].items())) + ". Answers changed "
          "after their import (the stored answer counts): " + (", ".join(result["changed_after_import"]) or "none") + ".", "",
          "## Appendix: every programme", ""]
    for lid, c in result["texts"].items():
        if not c:
            continue
        k = cards[lid]
        L += [f"### {lid}: {c['name']}", "",
              f"{k['arm']}, generation {k['generation']}, {k['operator']}"
              + (f" from {', '.join(k['parents'])}" if k["parents"] else "")
              + (f", change: {k['change']}" if k["change"] else "")
              + (f"; {k['level']} / {k['mechanism']}" if k["level"] else ""), "",
              *[f"- {p}" for p in c["core"]], "", f"Heuristic: {c['heuristic']}", ""]
        if c.get("auxiliaries"):
            L += ["Belt:", *[f"- {a['id']}: {a['statement']} (consequence: {a['consequence']})" for a in c["auxiliaries"]], ""]
        rat = next((x for x in result["lineage"] if x["lens"] == lid), None)
        if rat and rat.get("rationale"):
            L += [f"Rationale: {rat['rationale']}", ""]
    return "\n".join(L) + "\n"


def rating_sheet(result: dict, store: Store) -> tuple[str, dict]:
    """Blind pairs for a person: each island's seed against the selected arm's final child of that island, in a
    random order and without names. The key is stored in the game's events, not in the sheet."""
    cfg = result["cfg"]
    pairs, key = [], {}
    for n, island in enumerate(cfg["islands"], 1):
        child = child_id(island, generations(cfg), "selected" if generations(cfg) > 1 else "shared")
        if child not in result["texts"]:
            continue
        a, b = _order([island, child], result["game"], "rate", island)
        key[f"P{n}"] = {"A": a, "B": b}
        pairs.append((f"P{n}", a, b))
    L = ["# Which programme is better? A blind rating sheet", "",
         "Each pair shows two research programmes for reading the same evidence. For each pair, say which one you "
         "would rather develop (A or B, or no difference), and why in a sentence: is it clearer, bolder (does it "
         "forbid more), closer to what the literature needs? The names are hidden. Blinding is partial: an evolved "
         "programme may carry a protective belt that a seed lacks.", ""]
    for p, a, b in pairs:
        L += [f"## Pair {p}", ""]
        for side, lid in (("A", a), ("B", b)):
            c = result["texts"][lid]
            L += [f"**Programme {side}**", "", *[f"- {x}" for x in c["core"]], "", f"Heuristic: {c['heuristic']}", ""]
            if c.get("auxiliaries"):
                L += ["Belt:", *[f"- {x['id']}: {x['statement']}" for x in c["auxiliaries"]], ""]
        L += ["Your choice: A / B / no difference. Why:", "", ""]
    if key and not events(store, result["game"], "rating_key"):
        with store.db:
            _event(store, result["game"], "rating_key", key)
    return "\n".join(L), key
