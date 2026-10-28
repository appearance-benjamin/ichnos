"""Discovery: candidate directions found by code in everything the projects hold, not only in one finished paper.

Directions (directions.py) come from an agent reading one finished paper. Discovery reads every project's findings,
charting and insights at once and lists what any of them could become, each candidate with the counts, studies and
verbatim passages it rests on. No agent or model takes part: every candidate is a count anyone can redo.

Five generators:
- gap: a cell of a corpus's findings matrix, or of its evidence map (match_across against a match_on dimension),
  with at most one study where the row and column totals lead one to expect several (expected = row x column / N);
- contradiction: findings on the same question (the matrix fields and the outcome, without the direction) whose
  directions disagree across studies, with the fields that best separate the two sides as candidate moderators;
- bridge: an outcome studied in two or more projects (matched by its words), with what each project studied as its
  causes and how many studies the projects share. A comparator strand and its project are not a bridge;
- cluster: insights noted independently (in different agent calls or by different people) in similar words or about
  the same studies: a pattern noticed more than once;
- trend: a charted value whose share of the studies published in the last three years is well above its share before.

A protocol may set what counts as the same question and as a cause:

    [discovery]
    same_question = ["candidate_rule", "modern_input", "outcome"]
    causes = ["modern_input"]

A candidate is noticed in the studies it lists. A pattern claimed from it is tested on other studies (the
`noticed_in` rule of [[patterns]]); the page says so. Each sweep recomputes every candidate; a candidate keeps its key
across sweeps, so the author's choice and note stay with it, and one not found again is marked gone, not deleted.

What the author chooses goes through agent tasks of its own (output/discovery/agents, imported with agent-import):
1. discovery_score: an agent reads each chosen discovery's dossier (its counts and the findings, with passages, and
   insights it rests on) and writes a testable statement, the kind of output, value, feasibility with its basis
   (data in the store, a new search, new data) and the risk that the pattern is an artefact of coding or selection;
2. the lens layer's novelty check (novelty.py) under the tournament "discovery": search strings for the first
   scorer's statement, with recall controls from the discoveries' own studies; the searches; two judges label the
   hits; the verdict (novel, near, prior, ...) comes from novelty.verdicts.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import projects, tables
from .agents import removed_ids
from .config import ROOT
from .review import latest_protocol
from .store import Store, now

KINDS = ("contradiction", "gap", "bridge", "cluster", "trend")
STATUSES = ("open", "chosen", "set_aside")
KIND_LABELS = {"contradiction": "Contradiction", "gap": "Gap", "bridge": "Bridge", "cluster": "Recurring insight",
               "trend": "Rising topic"}
KIND_HELP = {
    "contradiction": "Studies on the same question disagree. The fields that separate the two sides are candidate "
                     "moderators: a reanalysis or a focused paper.",
    "gap": "A combination the rest of the map makes likely has (almost) no studies: a review strand or an empirical "
           "study.",
    "bridge": "Two projects study the same outcome from different causes and barely share studies: a cross-project "
              "paper or theory.",
    "cluster": "Agents or people noticed the same thing in separate passes: a pattern to pre-register and test.",
    "trend": "A value is much more common among recent studies than before: an emerging question.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS discoveries (
    discovery_id INTEGER PRIMARY KEY,
    key          TEXT NOT NULL UNIQUE,          -- stable across sweeps: kind, corpora and what was compared
    kind         TEXT NOT NULL,
    corpora      TEXT NOT NULL,                 -- "C" or "A,F"
    runs         TEXT NOT NULL,                 -- JSON list of run ids
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL,
    strength     REAL NOT NULL,                 -- within a kind: higher is stronger (see each generator)
    evidence     TEXT NOT NULL,                 -- JSON: counts, works, findings, insights, quotes
    status       TEXT NOT NULL DEFAULT 'open',
    note         TEXT,
    decided_by   TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL,
    sweep_id     INTEGER NOT NULL,              -- the sweep that last found it
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovery_sweeps (
    sweep_id     INTEGER PRIMARY KEY,
    sweep_at     TEXT NOT NULL,
    counts       TEXT NOT NULL                  -- JSON: per kind, how many candidates
);
"""

# Labels that say nothing about the content of a cell
VOID = {"", "other", "none", "not_applicable", "not_reported", "unclear", "not_stated", "direction_not_reported",
        "neither", "other_evolutionary", "other_coping", "multiple"}
# How each corpus's direction labels point: the same outcome went up / better, down / worse, or did not move
UP = {"increase", "positive_association", "favourable", "lower_better", "raises", "better", "positive", "higher_better"}
DOWN = {"decrease", "negative_association", "unfavourable", "lower_worse", "lowers", "worse", "negative", "higher_worse"}
NULL = {"no_change", "no_association", "no_difference", "no_relation", "null"}
DIRECTION_FIELDS = {"direction", "sign"}
# Fields about how a study knows, or how a finding is filed, not what it is about
EVIDENCE_FIELDS = {"evidence_type", "design", "aim_link", "bearing", "relates", "outcome_domain", "who"}
MAX_VALUES = 30            # a dimension with more distinct labels is free text, too fine to count cells on
MAX_PER_SOURCE = 8         # candidates kept per matrix, per corpus and kind
RECENT_YEARS = 3

STOP = set("""a about above across after again against all also among an and any are as at be because been before
being between both but by can could did do does during each either few for from further had has have having here how
if in into is it its itself more most much no nor not of off on once only or other our out over own per same several
she should so some such than that the their them then there these they this those through to too under until up upon
very was we were what when where whether which while who whom why will with within without would you study studies
finding findings found show shows showed shown report reports reported effect effects associated association
higher lower more less greater one two three among across participants sample samples result results however
evidence suggests suggest suggesting related relation relationship""".split())


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


# What one project holds

@dataclass
class Corpus:
    corpus: str
    run_id: int
    name: str
    protocol: dict
    findings: list[dict] = field(default_factory=list)          # finding_id, work_id, fields, evidence
    labels: dict[int, dict[str, set]] = field(default_factory=dict)   # work_id -> field or dimension -> values
    fields: list[str] = field(default_factory=list)             # categorical finding fields
    dims: list[str] = field(default_factory=list)               # map dimensions with few enough labels
    years: dict[int, int] = field(default_factory=dict)
    titles: dict[int, str] = field(default_factory=dict)


def _values(raw) -> set[str]:
    if raw is None:
        return set()
    items = raw if isinstance(raw, list) else str(raw).split(";")
    return {str(v).strip().lower() for v in items if str(v).strip()}


def load(store: Store, root: Path = ROOT) -> list[Corpus]:
    """Every project's main run with its findings, charting and study years. A project without findings or charting
    is left out."""
    reg = projects.registry(root)
    out = []
    for p in projects.brief(store, root):
        if reg.get(p["corpus"].upper(), {}).get("archived"):
            continue
        protocol = latest_protocol(store, p["corpus"])
        c = Corpus(p["corpus"], p["run"], p["name"], protocol)
        spec = (protocol.get("findings") or {}).get("fields") or []
        c.fields = [f["name"] for f in spec if f.get("categories")]
        gone = removed_ids(store, p["run"])
        for r in tables.finding_rows(store, p["run"]):
            fields = json.loads(r["fields"] or "{}")
            values = {k: _values(fields.get(k)) for k in c.fields}
            c.findings.append({"finding_id": r["finding_id"], "work_id": r["work_id"], "fields": values,
                               "finding": r["finding"], "evidence": r["evidence"]})
            study = c.labels.setdefault(r["work_id"], {})
            for k, vs in values.items():
                study.setdefault(k, set()).update(vs)
        charting = {w: d for w, d in tables.charting(store, p["run"]).items() if w not in gone}
        vocab: dict[str, set] = defaultdict(set)
        for w, dims in charting.items():
            for dim, cell in dims.items():
                vs = _values(cell["values"])
                vocab[dim].update(vs - VOID)
                c.labels.setdefault(w, {})[f"map:{dim}"] = vs
        c.dims = [d for d, vs in vocab.items() if 1 < len(vs) <= MAX_VALUES and d not in c.fields]
        if not c.labels:
            continue
        ids = tuple(c.labels)
        for chunk in range(0, len(ids), 900):
            part = ids[chunk:chunk + 900]
            for r in store.db.execute(f"SELECT work_id, year, title FROM works WHERE work_id IN "
                                      f"({','.join('?' * len(part))})", part):
                if r["year"]:
                    c.years[r["work_id"]] = int(r["year"])
                c.titles[r["work_id"]] = r["title"] or ""
        out.append(c)
    return out


def _settings(c: Corpus) -> tuple[list[str], list[str]]:
    """Which finding fields make two findings answer the same question, and which are causes (exposures, settings,
    design choices): the protocol's [discovery], else the findings matrix and the outcome without the direction."""
    disc = c.protocol.get("discovery") or {}
    matrix = (c.protocol.get("findings") or {}).get("matrix") or []
    plain = [f for f in c.fields if f not in DIRECTION_FIELDS | EVIDENCE_FIELDS]
    same = [f for f in disc.get("same_question") or
            [m for m in matrix if m in plain] + (["outcome"] if "outcome" in plain and "outcome" not in matrix else [])
            if f in c.fields]
    causes = [f for f in disc.get("causes") or [f for f in plain if f != "outcome"] if f in c.fields]
    if not causes:                          # no cause among the finding fields: the evidence map's comparison axes
        spec = c.protocol.get("map") or {}
        causes = [f"map:{d}" for d in [spec.get("match_across")] + list(spec.get("match_on") or [])
                  if d in c.dims and "outcome" not in d]
    return same, causes


def _direction_field(c: Corpus) -> str | None:
    return "direction" if "direction" in c.fields else ("sign" if "sign" in c.fields else None)


def _polarity(values: set[str]) -> int | None:
    up, down = bool(values & UP), bool(values & DOWN)
    if up and not down:
        return 1
    if down and not up:
        return -1
    if values & NULL and not (up or down):
        return 0
    return None


def _quotes(c: Corpus, finding_ids: list[int], n: int = 4) -> list[dict]:
    by = {f["finding_id"]: f for f in c.findings}
    out = []
    for fid in finding_ids[:n]:
        f = by[fid]
        out.append({"corpus": c.corpus, "work_id": f["work_id"], "finding_id": fid, "finding": f["finding"],
                    "text": f["evidence"], "title": c.titles.get(f["work_id"], ""), "year": c.years.get(f["work_id"])})
    return out


def _pretty(label: str) -> str:
    return label.replace("map:", "").replace("_", " ")


def _key(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:16]


# Generators: each returns candidates as dicts with key, kind, corpora, runs, title, summary, strength, evidence

def contradictions(c: Corpus) -> list[dict]:
    """Same question, opposite directions: at least two studies on each side. Strength: the smaller side."""
    same, _ = _settings(c)
    direction = _direction_field(c)
    if not same or not direction:
        return []
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for f in c.findings:
        keys = [sorted(f["fields"].get(k, set()) - VOID) for k in same]
        if any(not k for k in keys):
            continue
        for combo in _product(keys):
            groups[combo].append(f)
    out = []
    for combo, fs in groups.items():
        per_study: dict[int, list[int]] = defaultdict(list)
        for f in fs:
            p = _polarity(f["fields"].get(direction, set()))
            if p is not None:
                per_study[f["work_id"]].append(p)
        sides = {1: [], -1: [], 0: []}
        for w, ps in per_study.items():
            moved = set(ps) - {0}
            if len(moved) <= 1:                     # a study pointing both ways is on neither side
                sides[moved.pop() if moved else 0].append(w)
        up, down, null = sorted(sides[1]), sorted(sides[-1]), sorted(sides[0])
        if len(up) < 2 or len(down) < 2:
            continue
        on = dict(zip(same, combo))
        moderators = _separators(c, set(up), set(down), exclude=set(same) | {direction})
        pick = lambda ws, p: [f["finding_id"] for f in fs if f["work_id"] in ws  # noqa: E731
                              and _polarity(f["fields"].get(direction, set())) == p]
        question = ", ".join(_pretty(v) for v in combo)
        label = lambda ps: _pretty(Counter(v for f in fs for v in f["fields"].get(direction, set())  # noqa: E731
                                           if v in ps).most_common(1)[0][0])
        split = (" Splits best by " + "; ".join(f"{_pretty(m['field'])} = {_pretty(m['value'])} "
                                                  f"({m['up']} of {len(up)} vs {m['down']} of {len(down)})"
                                                  for m in moderators[:2]) + ".") if moderators else ""
        out.append({
            "key": _key("contradiction", c.corpus, *[f"{k}={v}" for k, v in on.items()]),
            "kind": "contradiction", "corpora": [c.corpus], "runs": [c.run_id],
            "title": f"Mixed directions on {question}",
            "summary": f"{len(up)} studies {label(UP)}, {len(down)} {label(DOWN)}, {len(null)} no difference.{split}",
            "strength": float(min(len(up), len(down))),
            "evidence": {"on": on, "direction_field": direction, "up": up, "down": down, "null": null,
                         "moderators": moderators[:4], "works": up + down + null,
                         "findings": [f["finding_id"] for f in fs],
                         "quotes": _quotes(c, pick(set(up), 1)[:2] + pick(set(down), -1)[:2])}})
    return _top(out)


def _separators(c: Corpus, a: set[int], b: set[int], exclude: set[str]) -> list[dict]:
    """For each other field and charted dimension: the value whose share differs most between the two groups of
    studies. Kept when the shares differ by at least 0.4 and the value occurs in at least two studies."""
    out = []
    names = [f for f in c.fields if f not in exclude] + [f"map:{d}" for d in c.dims]
    for name in names:
        best = None
        values = {v for w in a | b for v in c.labels.get(w, {}).get(name, set())} - VOID
        for v in values:
            na = sum(v in c.labels.get(w, {}).get(name, set()) for w in a)
            nb = sum(v in c.labels.get(w, {}).get(name, set()) for w in b)
            if na + nb < 2:
                continue
            diff = na / len(a) - nb / len(b)
            if best is None or abs(diff) > abs(best["diff"]):
                best = {"field": name, "value": v, "up": na, "down": nb, "diff": round(diff, 2)}
        if best and abs(best["diff"]) >= 0.4:
            out.append(best)
    return sorted(out, key=lambda m: -abs(m["diff"]))


def gaps(c: Corpus) -> list[dict]:
    """Structural holes: cells with at most one study where row x column / N expects at least three.
    Strength: expected minus observed."""
    pairs = []
    matrix = (c.protocol.get("findings") or {}).get("matrix") or []
    if len(matrix) == 2 and all(m in c.fields for m in matrix) and not set(matrix) & DIRECTION_FIELDS:
        pairs.append(tuple(matrix))
    spec = c.protocol.get("map") or {}
    across = spec.get("match_across")
    if across in c.dims:
        pairs += [(f"map:{across}", f"map:{d}") for d in spec.get("match_on") or [] if d in c.dims]
    _, causes = _settings(c)
    if "outcome" in c.fields:
        pairs += [(cause, "outcome") for cause in causes[:2] if (cause, "outcome") not in pairs]
    out = []
    for rdim, cdim in pairs:
        cells: dict[tuple, set] = defaultdict(set)
        rows, cols, studies = defaultdict(set), defaultdict(set), set()
        for w, lab in c.labels.items():
            rv, cv = lab.get(rdim, set()) - VOID, lab.get(cdim, set()) - VOID
            if not rv or not cv:
                continue
            studies.add(w)
            for r in rv:
                rows[r].add(w)
                for col in cv:
                    cells[(r, col)].add(w)
            for col in cv:
                cols[col].add(w)
        n = len(studies)
        if n < 10:
            continue
        found = []
        for r, rws in rows.items():
            for col, cws in cols.items():
                expected = len(rws) * len(cws) / n
                observed = cells.get((r, col), set())
                if len(observed) <= 1 and expected >= 3:
                    found.append((expected - len(observed), r, col, expected, observed, rws, cws))
        for gap, r, col, expected, observed, rws, cws in sorted(found, reverse=True)[:MAX_PER_SOURCE]:
            next_to = sorted(rws, key=lambda w: -len(c.labels[w].get(cdim, set())))[:3] + \
                sorted(cws, key=lambda w: -len(c.labels[w].get(rdim, set())))[:3]
            fids = [f["finding_id"] for f in c.findings if f["work_id"] in observed]
            out.append({
                "key": _key("gap", c.corpus, rdim, r, cdim, col),
                "kind": "gap", "corpora": [c.corpus], "runs": [c.run_id],
                "title": f"Few studies on {_pretty(r)} × {_pretty(col)}",
                "summary": f"{len(observed)} {'study' if len(observed) == 1 else 'studies'} where {expected:.1f} "
                           f"would be expected: {len(rws)} studies have {_pretty(rdim)} = {_pretty(r)}, "
                           f"{len(cws)} have {_pretty(cdim)} = {_pretty(col)}, of {n}.",
                "strength": round(gap, 2),
                "evidence": {"row": [rdim, r], "col": [cdim, col], "observed": sorted(observed), "n_works": n,
                             "expected": round(expected, 2), "row_n": len(rws), "col_n": len(cws), "n": n,
                             "works": sorted(observed) + next_to, "findings": fids, "quotes": _quotes(c, fids, 2)}})
    return _top(out)


def _concepts(label: str) -> set[str]:
    words = re.split(r"[^a-z]+", label.lower().replace("well_being", "wellbeing").replace("well-being", "wellbeing")
                     .replace("behavior", "behaviour"))
    generic = {"other", "of", "and", "or", "in", "the", "to", "life", "same", "later", "symptoms", "rate", "quality",
               "behaviour", "perception", "attitudes", "toward", "intentions", "desires", "itself", "use"}
    return {w for w in words if len(w) > 2 and w not in generic}


def bridges(corpora: list[Corpus], root: Path = ROOT) -> list[dict]:
    """An outcome concept that two projects study, each with at least three studies; a project and its comparator
    strand are not a bridge. Strength: studies on the smaller side, halved when the two already share studies."""
    reg = projects.registry(root)
    linked = {frozenset((k, str(e.get("strand_of")).upper())) for k, e in reg.items() if e.get("strand_of")}
    by_concept: dict[str, dict[str, dict]] = defaultdict(dict)
    for c in corpora:
        if "outcome" not in c.fields:
            continue
        _, causes = _settings(c)
        for f in c.findings:
            for label in f["fields"].get("outcome", set()) - VOID:
                for concept in _concepts(label):
                    side = by_concept[concept].setdefault(c.corpus, {"c": c, "labels": Counter(), "works": set(),
                                                                     "findings": [], "causes": Counter()})
                    side["labels"][label] += 1
                    side["works"].add(f["work_id"])
                    side["findings"].append(f["finding_id"])
        for sides in by_concept.values():
            if c.corpus in sides:                # causes per study: finding fields or charted dimensions
                side = sides[c.corpus]
                side["causes"] = Counter(f"{cause}={v}" for w in side["works"] for cause in causes
                                         for v in c.labels.get(w, {}).get(cause, set()) - VOID)
    out = []
    for concept, all_sides in by_concept.items():
        for pair in _pairs(sorted(k for k, s in all_sides.items() if len(s["works"]) >= 3)):
            if frozenset(pair) in linked:
                continue
            out.append(_bridge(concept, {k: all_sides[k] for k in pair}))
    return sorted(out, key=lambda d: -d["strength"])[:3 * MAX_PER_SOURCE]


def _bridge(concept: str, sides: dict[str, dict]) -> dict:
    names = sorted(sides)
    shared = set.intersection(*(s["works"] for s in sides.values()))
    smallest = min(len(s["works"]) for s in sides.values())
    parts = []
    for k in names:
        s = sides[k]
        top = ", ".join(_pretty(x.split("=", 1)[1]) for x, _ in s["causes"].most_common(3))
        parts.append(f"{k}: {len(s['works'])} studies" + (f" (via {top})" if top else ""))
    return {
        "key": _key("bridge", concept, *names),
        "kind": "bridge", "corpora": names, "runs": [sides[k]["c"].run_id for k in names],
        "title": f"{concept.capitalize()} in {sides[names[0]]['c'].name} and {sides[names[1]]['c'].name}",
        "summary": "; ".join(parts) + f". Studies in both: {len(shared)}.",
        "strength": round(smallest / (2 if shared else 1), 2),
        "evidence": {"concept": concept, "shared": sorted(shared),
                     "n_works": len(set().union(*(s["works"] for s in sides.values()))),
                     "sides": {k: {"labels": dict(sides[k]["labels"]), "n": len(sides[k]["works"]),
                                   "causes": dict(sides[k]["causes"].most_common(8))} for k in names},
                     "works": sorted(shared) + [w for k in names for w in sorted(sides[k]["works"])[:3]],
                     "findings": [f for k in names for f in sides[k]["findings"][:50]],
                     "quotes": [q for k in names for q in _quotes(sides[k]["c"], sides[k]["findings"], 1)]}}


def _pairs(names):
    return [(a, b) for i, a in enumerate(names) for b in names[i + 1:]]


def _words(text: str) -> set[str]:
    return {w.rstrip("s") for w in re.findall(r"[a-z]{4,}", (text or "").lower()) if w not in STOP}


def clusters(store: Store, corpora: list[Corpus]) -> list[dict]:
    """Insights noted in separate passes (agent calls or people) that say the same thing. Two insights are close
    when they share words (Jaccard >= 0.3) or two studies and some words (>= 0.15). The insight with the most close
    neighbours becomes a centre and takes its unclaimed neighbours, then the next: no chains of loosely linked
    insights. A cluster needs three insights from three passes. Method notes (a duplicate record, a mislabelled type)
    are about the pipeline, not the field, and are left out. Strength: the number of separate passes."""
    out = []
    for c in corpora:
        rows = store.db.execute("SELECT * FROM insights WHERE corpus = ? AND kind != 'method' AND status NOT IN "
                                "('dropped')", (c.corpus,)).fetchall()
        items = []
        for r in rows:
            support = json.loads(r["support"] or "{}")
            items.append({"id": r["insight_id"], "kind": r["kind"], "statement": r["statement"],
                          "words": _words(r["statement"]), "works": set(support.get("works") or []),
                          "pass": r["call_id"] or r["author"], "quotes": support.get("quotes") or []})
        index: dict[str, list[int]] = defaultdict(list)
        for i, it in enumerate(items):
            for w in it["words"]:
                index[w].append(i)
        near: dict[int, dict[int, float]] = defaultdict(dict)
        for i, it in enumerate(items):
            for j in {j for w in it["words"] if len(index[w]) < 80 for j in index[w] if j > i}:
                other = items[j]
                if it["pass"] == other["pass"]:
                    continue
                jac = len(it["words"] & other["words"]) / (len(it["words"] | other["words"]) or 1)
                if jac >= 0.3 or (len(it["works"] & other["works"]) >= 2 and jac >= 0.15):
                    near[i][j] = near[j][i] = jac
        taken: set[int] = set()
        for centre in sorted(near, key=lambda i: (-len(near[i]), -sum(near[i].values()), items[i]["id"])):
            if centre in taken:
                continue
            members = [centre] + [j for j in near[centre] if j not in taken]
            passes = {items[i]["pass"] for i in members}
            if len(members) < 3 or len(passes) < 3:
                continue
            taken.update(members)
            sim = near[centre]
            kinds = Counter(items[i]["kind"] for i in members)
            works = sorted(set().union(*(items[i]["works"] for i in members)))
            ids = sorted(items[i]["id"] for i in members)
            quotes = [dict(q, corpus=c.corpus, title=c.titles.get(q.get("work_id"), ""),
                           year=c.years.get(q.get("work_id")))
                      for i in sorted(members, key=lambda i: -sim.get(i, 1.0))[:3] for q in items[i]["quotes"][:1]]
            out.append({
                "key": _key("cluster", c.corpus, items[centre]["id"]),
                "kind": "cluster", "corpora": [c.corpus], "runs": [c.run_id],
                "title": _short(items[centre]["statement"]),
                "summary": f"{len(members)} insights from {len(passes)} separate passes ("
                           + ", ".join(f"{n} {k}" for k, n in kinds.most_common()) + f"), about {len(works)} studies.",
                "strength": float(len(passes)),
                "evidence": {"insights": ids, "centre": items[centre]["id"], "statement": items[centre]["statement"],
                             "kinds": dict(kinds), "passes": len(passes), "works": works, "findings": [],
                             "quotes": quotes}})
    return sorted(out, key=lambda d: -d["strength"])[:4 * MAX_PER_SOURCE]


def _short(text: str, n: int = 140) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + "…"


def trends(c: Corpus, this_year: int | None = None) -> list[dict]:
    """Values at least twice as common among the studies of the last three years as before, by at least eight
    points, in at least four recent studies. Strength: the difference in share, in points."""
    this_year = this_year or datetime.now(timezone.utc).year
    recent = {w for w, y in c.years.items() if y > this_year - RECENT_YEARS and w in c.labels}
    earlier = {w for w, y in c.years.items() if y <= this_year - RECENT_YEARS and w in c.labels}
    if len(recent) < 15 or len(earlier) < 15:
        return []
    out = []
    filing = {"aim_link", "outcome_domain", "bearing", "relates"}       # how a finding is filed, not the field
    for name in [f for f in c.fields if f not in DIRECTION_FIELDS | filing] + [f"map:{d}" for d in c.dims]:
        has_r = [w for w in recent if c.labels[w].get(name)]
        has_e = [w for w in earlier if c.labels[w].get(name)]
        if len(has_r) < 15 or len(has_e) < 15:
            continue
        counts_r = Counter(v for w in has_r for v in c.labels[w][name] - VOID)
        counts_e = Counter(v for w in has_e for v in c.labels[w][name] - VOID)
        for v, nr in counts_r.items():
            r, e = nr / len(has_r), counts_e.get(v, 0) / len(has_e)
            if nr >= 4 and r >= 2 * e and r - e >= 0.08:
                works = sorted((w for w in has_r if v in c.labels[w][name]), key=lambda w: -c.years[w])
                fids = [f["finding_id"] for f in c.findings if f["work_id"] in set(works[:6])]
                out.append({
                    "key": _key("trend", c.corpus, name, v),
                    "kind": "trend", "corpora": [c.corpus], "runs": [c.run_id],
                    "title": f"Rising: {_pretty(name)} = {_pretty(v)}",
                    "summary": f"{r:.0%} of the {len(has_r)} studies since {this_year - RECENT_YEARS + 1} "
                               f"against {e:.0%} of the {len(has_e)} before.",
                    "strength": round(100 * (r - e), 1),
                    "evidence": {"field": name, "value": v, "recent": [nr, len(has_r)], "n_works": nr,
                                 "earlier": [counts_e.get(v, 0), len(has_e)], "since": this_year - RECENT_YEARS + 1,
                                 "works": works[:12], "findings": fids, "quotes": _quotes(c, fids, 2)}})
    return _top(out)


def _top(items: list[dict]) -> list[dict]:
    return sorted(items, key=lambda d: -d["strength"])[:MAX_PER_SOURCE * 2]


def _product(lists):
    out = [()]
    for xs in lists:
        out = [o + (x,) for o in out for x in xs]
    return out


# Sweeps and statuses

def find(store: Store, root: Path = ROOT, this_year: int | None = None, log=print) -> list[dict]:
    """Every candidate the generators find now, without storing anything."""
    corpora = load(store, root)
    found = []
    for c in corpora:
        log(f"Corpus {c.corpus} (run {c.run_id}): {len(c.findings)} findings, {len(c.labels)} studies")
        found += contradictions(c) + gaps(c) + trends(c, this_year)
    found += bridges(corpora, root) + clusters(store, corpora)
    return found


def sweep(store: Store, root: Path = ROOT, this_year: int | None = None, log=print) -> dict:
    """Find the candidates and store them: new ones open, known ones updated (their status and note stay)."""
    ensure(store)
    found = find(store, root, this_year, log)
    stamp = now()
    counts = dict(Counter(d["kind"] for d in found))
    with store.db:
        sweep_id = store.db.execute("INSERT INTO discovery_sweeps (sweep_at, counts) VALUES (?, ?)",
                                    (stamp, json.dumps(counts))).lastrowid
        for d in found:
            row = store.db.execute("SELECT discovery_id FROM discoveries WHERE key = ?", (d["key"],)).fetchone()
            values = (d["kind"], ",".join(d["corpora"]), json.dumps(d["runs"]), d["title"], d["summary"],
                      d["strength"], json.dumps(d["evidence"], ensure_ascii=False))
            if row:
                store.db.execute("UPDATE discoveries SET kind = ?, corpora = ?, runs = ?, title = ?, summary = ?, "
                                 "strength = ?, evidence = ?, last_seen = ?, sweep_id = ?, updated_at = ? "
                                 "WHERE key = ?", values + (stamp, sweep_id, stamp, d["key"]))
            else:
                store.db.execute("INSERT INTO discoveries (kind, corpora, runs, title, summary, strength, evidence, "
                                 "key, first_seen, last_seen, sweep_id, updated_at) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                 values + (d["key"], stamp, stamp, sweep_id, stamp))
    return {"found": len(found), **counts}


def last_sweep(store: Store) -> str | None:
    ensure(store)
    row = store.db.execute("SELECT sweep_at FROM discovery_sweeps ORDER BY sweep_id DESC LIMIT 1").fetchone()
    return row[0] if row else None


def rows(store: Store, include_gone: bool = False) -> list[dict]:
    """The stored candidates, strongest first within each kind; `gone` marks those the latest sweep did not find."""
    ensure(store)
    latest = store.db.execute("SELECT max(sweep_id) FROM discovery_sweeps").fetchone()[0]
    out = []
    for r in store.db.execute("SELECT * FROM discoveries ORDER BY kind, strength DESC, discovery_id"):
        gone = r["sweep_id"] != latest
        if gone and not include_gone:
            continue
        out.append({**{k: r[k] for k in ("discovery_id", "kind", "title", "summary", "strength", "status", "note",
                                         "decided_by", "first_seen", "last_seen")},
                    "corpora": r["corpora"].split(","), "runs": json.loads(r["runs"]),
                    "evidence": json.loads(r["evidence"]), "gone": gone})
    order = {k: i for i, k in enumerate(KINDS)}
    return sorted(out, key=lambda d: (order.get(d["kind"], 99), -d["strength"], d["discovery_id"]))


def set_status(store: Store, discovery_id: int, status: str, who: str, note: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    ensure(store)
    with store.db:
        cur = store.db.execute("UPDATE discoveries SET status = ?, note = ?, decided_by = ?, updated_at = ? "
                               "WHERE discovery_id = ?", (status, note, who, now(), discovery_id))
    if cur.rowcount == 0:
        raise ValueError(f"no discovery {discovery_id}")


def open_count(store: Store) -> int:
    return sum(d["status"] == "open" for d in rows(store))


# Agent tasks on chosen discoveries: scoring, then the novelty check of the lens layer under its own tournament

TASK_KINDS = ("discovery_score", "novelty_queries", "novelty_judge")
TOURNAMENT = "discovery"
BASES = ("data_in_store", "new_search", "new_data")
DEFAULT_AGENT = "model:claude-opus-5-5"
MAX_DOSSIER_FINDINGS = 12
SCORE_SCHEMA = """
CREATE TABLE IF NOT EXISTS discovery_scores (
    score_id      INTEGER PRIMARY KEY,
    discovery_id  INTEGER NOT NULL REFERENCES discoveries(discovery_id),
    agent         TEXT NOT NULL,
    statement     TEXT NOT NULL,               -- what a study would test, one sentence: the novelty check's input
    output        TEXT NOT NULL,               -- directions.DIRECTION_KINDS
    value         INTEGER NOT NULL,            -- 1-5
    feasibility   INTEGER NOT NULL,            -- 1-5
    basis         TEXT NOT NULL,               -- data_in_store, new_search or new_data
    artefact_risk INTEGER NOT NULL,            -- 1-5: how likely the pattern comes from coding, selection or labels
    why           TEXT NOT NULL,
    risks         TEXT,
    next_step     TEXT NOT NULL,
    cites         TEXT NOT NULL,               -- JSON: findings and insights from the dossier
    call_id       INTEGER REFERENCES calls(call_id),
    created_at    TEXT NOT NULL
);
"""
SCORE_OUTPUT = {"scores": [{
    "key": "d12",
    "statement": "what a study would test, one sentence (a claim or a question), as specific as the evidence allows",
    "output": "new_review | reanalysis | empirical_study | intervention_design | theory",
    "value": 4, "feasibility": 3,
    "basis": " | ".join(BASES),
    "artefact_risk": 2,
    "why": "why it matters, 1-2 sentences",
    "risks": "what could make it an artefact or a dead end, 1-2 sentences",
    "next_step": "the concrete first step, 1 sentence",
    "cites": {"findings": [101, 102], "insights": [7]}}]}


def ensure_tasks(store: Store) -> None:
    from . import novelty
    ensure(store)
    novelty.ensure(store)
    store.db.executescript(SCORE_SCHEMA)


def task_folder(root: Path, kind: str, agent: str) -> Path:
    """output/discovery/agents/<kind>__<agent>; an answered task is kept and the next one gets __2, __3, ..."""
    slug = re.sub(r"[^a-z0-9]+", "-", agent.lower()).strip("-")
    out = base = root / "output" / "discovery" / "agents" / f"{kind}__{slug}"
    n = 2
    while any(out.glob("batch_*_result.json")):
        out = base.with_name(f"{base.name}__{n}")
        n += 1
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("batch_*.json"):
        old.unlink()
    return out


def tasks(root: Path = ROOT) -> list[dict]:
    from .agents import task_state
    folder = root / "output" / "discovery" / "agents"
    out = []
    for meta_file in sorted(folder.glob("*/task.json")) if folder.exists() else []:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        out.append({"folder": meta_file.parent.name, "path": str(meta_file.parent), "kind": meta["kind"],
                    "agent": meta["agent"], "records": meta.get("records", 0), "state": task_state(meta_file.parent)})
    return out


def dossier(store: Store, d: dict, names: dict[str, dict]) -> dict:
    """What the scoring agent reads about one discovery: the counts, the projects, and the findings and insights it
    rests on (each finding with its verbatim passage)."""
    ev = d["evidence"]
    counts = {k: (len(v) if k in ("up", "down", "null", "observed", "shared") else v) for k, v in ev.items()
              if k not in ("works", "findings", "quotes", "insights", "statement", "centre")}
    ids = list(dict.fromkeys([q["finding_id"] for q in ev.get("quotes") or [] if q.get("finding_id")]
                             + list(ev.get("findings") or [])))[:MAX_DOSSIER_FINDINGS]
    findings = []
    if ids:
        for r in store.db.execute(f"SELECT f.finding_id, f.work_id, f.run_id, f.finding, f.fields, f.evidence, "
                                  f"w.title, w.year FROM findings f JOIN works w ON w.work_id = f.work_id WHERE "
                                  f"f.finding_id IN ({','.join('?' * len(ids))})", tuple(ids)):
            findings.append({"finding_id": r["finding_id"], "work_id": r["work_id"], "study": r["title"],
                             "year": r["year"], "finding": r["finding"], "fields": json.loads(r["fields"] or "{}"),
                             "passage": r["evidence"]})
    found = []
    if ev.get("insights"):
        part = ev["insights"][:10]
        found = [{"insight_id": r["insight_id"], "kind": r["kind"], "statement": r["statement"]}
                 for r in store.db.execute(f"SELECT insight_id, kind, statement FROM insights WHERE insight_id IN "
                                           f"({','.join('?' * len(part))})", tuple(part))]
    return {"key": f"d{d['discovery_id']}", "kind": d["kind"], "what_this_kind_means": KIND_HELP[d["kind"]],
            "title": d["title"], "summary": d["summary"],
            "projects": [names.get(c, {"corpus": c}) for c in d["corpora"]],
            "counts": counts, "findings": findings, "insights": found}


def score_instructions() -> str:
    return "\n".join([
        "# Scoring discoveries", "",
        "Code found each discovery in this batch by counting a review's coded findings, charting or insights: a "
        "contradiction, a gap, a bridge between two projects, an insight noticed several times, or a rising topic. "
        "The author chose them to explore. For each one, say what it could become and how much it is worth:", "",
        "- statement: what a study would test, in one sentence: a claim or a question, as specific as the evidence "
        "allows (outcome, condition or cause, population). It is searched for in the literature afterwards, so "
        "write the idea itself, not a description of the discovery;",
        "- output: new_review (a review strand of its own), reanalysis (of the findings already coded), "
        "empirical_study, intervention_design or theory;",
        "- value (1-5): what answering it would change for researchers or practice in these projects' fields;",
        "- feasibility (1-5) and basis: whether it can be answered from the data already coded (data_in_store), "
        "needs a new literature search (new_search), or new data collection (new_data);",
        "- artefact_risk (1-5): how likely the pattern comes from how studies were coded, selected or labelled "
        "rather than from the field: mixed measures under one label, a direction coded on different scales, a gap "
        "that only reflects the search string, a topic that rose because one group publishes much. 5 = probably "
        "an artefact;",
        "- why, risks, next_step: short, concrete;",
        "- cites: the finding_ids and insight_ids of the dossier your answer rests on.", "",
        "Judge from the batch alone: open no other file, do not search the web, and do not claim that something is "
        "new or known; the novelty check does that next. A discovery that is probably an artefact gets a high "
        "artefact_risk and a low value, not a polite score.", "",
        "Output: one JSON object per batch file, written to batch_NN_result.json, every key exactly once:", "",
        "```json", json.dumps(SCORE_OUTPUT, indent=1), "```"]) + "\n"


def write_task(store: Store, kind: str, agent: str = DEFAULT_AGENT, root: Path = ROOT, ids: list[int] | None = None,
               per_batch: int = 6) -> Path:
    """Write a task folder for the chosen discoveries (or `ids`): discovery_score, novelty_queries (search strings for
    the scored statements, with recall controls) or novelty_judge (labels for the hits of searched checks)."""
    from . import novelty
    if kind not in TASK_KINDS:
        raise ValueError(f"kind must be one of {TASK_KINDS}")
    if not re.fullmatch(r"[\w.:\-]{3,60}", agent or ""):
        raise ValueError("give the agent a name such as model:claude-opus-5-5")
    ensure_tasks(store)
    meta = {"scope": "discovery", "kind": kind, "agent": agent, "run_id": None, "tournament": TOURNAMENT}
    current = {d["discovery_id"]: d for d in rows(store)}
    chosen = [d for i, d in current.items() if (i in ids if ids else d["status"] == "chosen")]
    if ids and len(chosen) < len(ids):
        raise ValueError(f"no current discovery {sorted(set(ids) - set(current))}")
    if kind == "discovery_score":
        done = {r[0] for r in store.db.execute("SELECT discovery_id FROM discovery_scores WHERE agent = ?", (agent,))}
        items = [d for d in chosen if ids or d["discovery_id"] not in done]
        if not items:
            raise ValueError(f"{agent} has scored every chosen discovery" if chosen else
                             "no discovery is chosen yet: choose some on the Discovery page")
        names = {p["corpus"]: {"corpus": p["corpus"], "name": p["name"], "about": p["about"],
                               "question": " ".join(((latest_protocol(store, p["corpus"]).get("findings") or {})
                                                     .get("question") or "").split())}
                 for p in projects.brief(store, root)}
        out = task_folder(root, kind, agent)
        groups = [items[i:i + per_batch] for i in range(0, len(items), per_batch)]
        for n, group in enumerate(groups, 1):
            (out / f"batch_{n:02d}.json").write_text(json.dumps({"discoveries": [dossier(store, d, names)
                                                                                 for d in group]},
                                                                ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "instructions.md").write_text(score_instructions(), encoding="utf-8")
        meta.update(batches=len(groups), records=len(items))
    elif kind == "novelty_queries":
        have = {novelty.check_key(r) for r in store.db.execute(
            "SELECT output_id, lens, hypothesis, control_work FROM novelty_checks WHERE tournament = ?", (TOURNAMENT,))}
        statements = statement_by_discovery(store)
        items = [d for d in chosen if d["discovery_id"] in statements
                 and (None, "discovery", str(d["discovery_id"])) not in have]
        if not items:
            raise ValueError("every scored discovery has search strings already" if statements else
                             "no chosen discovery is scored yet: run the discovery_score task first")
        keys = {f"d{d['discovery_id']}": {"lens": "discovery", "hypothesis": str(d["discovery_id"]),
                                          "run_id": d["runs"][0], "statement": statements[d["discovery_id"]]}
                for d in items}
        keys.update(_controls(store, items, have))
        data = {"items": [{"key": k, "statement": c["statement"]} for k, c in keys.items()]}
        out = task_folder(root, kind, agent)
        (out / "batch_01.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "instructions.md").write_text(novelty.queries_instructions(), encoding="utf-8")
        meta.update(keys=keys, batches=1, records=len(keys))
    else:
        searched = store.db.execute("SELECT * FROM novelty_checks WHERE tournament = ? AND searched_at IS NOT NULL "
                                    "ORDER BY check_id", (TOURNAMENT,)).fetchall()
        if not searched:
            raise ValueError("no searched checks yet: import the search strings, then run the searches")
        batches = novelty.judge_batches(store, searched, agent, per_batch)
        out = task_folder(root, kind, agent)
        for n, data in enumerate(batches, 1):
            (out / f"batch_{n:02d}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        (out / "instructions.md").write_text(novelty.judge_instructions(), encoding="utf-8")
        meta.update(batches=len(batches), records=sum(len(b["checks"]) for b in batches))
    (out / "task.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _controls(store: Store, items: list[dict], have: set, n: int = 2) -> dict:
    """Recall controls: findings of studies (with a DOI) that the discoveries rest on. The search should find
    those studies; if it does not, a "novel" verdict means little."""
    out: dict[str, dict] = {}
    for d in items:
        for fid in d["evidence"].get("findings") or []:
            if len(out) >= n:
                return out
            r = store.db.execute("SELECT f.work_id, f.finding, w.doi FROM findings f JOIN works w ON "
                                 "w.work_id = f.work_id WHERE f.finding_id = ?", (fid,)).fetchone()
            if r and r["doi"] and ("control", r["work_id"]) not in have and f"c{r['work_id']}" not in out:
                out[f"c{r['work_id']}"] = {"control_work": r["work_id"], "run_id": d["runs"][0],
                                           "statement": r["finding"]}
    return out


def _check_scores(data: dict, answer) -> list[str]:
    from .directions import DIRECTION_KINDS
    if not isinstance(answer, dict):
        return ["the answer must be a JSON object"]
    want = {d["key"]: d for d in data["discoveries"]}
    got = [s.get("key") for s in answer.get("scores") or []]
    errs = [] if sorted(got) == sorted(want) else ["scores: every discovery key exactly once"]
    for s in answer.get("scores") or []:
        k, d = s.get("key"), want.get(s.get("key"))
        if d is None:
            continue
        words = len(str(s.get("statement") or "").split())
        if not 5 <= words <= 60:
            errs.append(f"{k}: statement: one sentence of 5-60 words ({words})")
        if s.get("output") not in DIRECTION_KINDS:
            errs.append(f"{k}: output must be one of {DIRECTION_KINDS}")
        if s.get("basis") not in BASES:
            errs.append(f"{k}: basis must be one of {BASES}")
        for f in ("value", "feasibility", "artefact_risk"):
            if not (isinstance(s.get(f), int) and 1 <= s[f] <= 5):
                errs.append(f"{k}: {f} must be a whole number from 1 to 5")
        for f in ("why", "next_step"):
            if not str(s.get(f) or "").strip():
                errs.append(f"{k}: {f} is empty")
        cites = s.get("cites") or {}
        extra = set(cites.get("findings") or []) - {x["finding_id"] for x in d["findings"]}
        extra |= {f"insight {i}" for i in set(cites.get("insights") or []) - {x["insight_id"] for x in d["insights"]}}
        if extra:
            errs.append(f"{k}: cites what is not in its dossier: {sorted(map(str, extra))}")
    return errs


def import_task(folder: Path, store: Store, meta: dict, instructions: str) -> dict:
    from . import novelty
    from .agents import _strip_fences, batch_files
    ensure_tasks(store)
    if meta["kind"] != "discovery_score":
        return novelty.import_task(folder, store, meta, {}, instructions)
    problems, answers = [], []
    for batch in batch_files(folder):
        result = batch.with_name(batch.stem + "_result.json")
        if not result.exists():
            problems.append(f"{result.name} is missing")
            continue
        raw = result.read_text(encoding="utf-8")
        try:
            answer = json.loads(_strip_fences(raw))
        except ValueError as err:
            problems.append(f"{result.name}: not valid JSON ({err})")
            continue
        data = json.loads(batch.read_text(encoding="utf-8"))
        problems += [f"{result.name}: {e}" for e in _check_scores(data, answer)]
        answers.append((batch, raw, answer))
    if problems:
        return {"stored": 0, "problems": problems}
    stored = 0
    for batch, raw, answer in answers:
        call_id = store.log_call(None, "llm", meta["agent"], f"{instructions}\n\n[discovery_score batch, sha256 "
                                 f"{hashlib.sha256(batch.read_bytes()).hexdigest()}]", 200, 0, raw.encode("utf-8"),
                                 {"agent": meta["agent"], "task": meta["kind"]})
        with store.db:
            for s in answer["scores"]:
                store.db.execute(
                    "INSERT INTO discovery_scores (discovery_id, agent, statement, output, value, feasibility, basis, "
                    "artefact_risk, why, risks, next_step, cites, call_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (int(s["key"][1:]), meta["agent"], " ".join(s["statement"].split()), s["output"], s["value"],
                     s["feasibility"], s["basis"], s["artefact_risk"], s["why"], s.get("risks"), s["next_step"],
                     json.dumps(s.get("cites") or {}), call_id, now()))
                stored += 1
    return {"stored": stored, "problems": []}


def scores(store: Store) -> dict[int, list[dict]]:
    """Each discovery's latest score per agent, the earliest agent first."""
    ensure_tasks(store)
    out: dict[int, dict[str, dict]] = defaultdict(dict)
    for r in store.db.execute("SELECT * FROM discovery_scores ORDER BY score_id"):
        out[r["discovery_id"]][r["agent"]] = {k: r[k] for k in r.keys()}
    return {d: sorted(by.values(), key=lambda s: s["score_id"]) for d, by in out.items()}


def statement_by_discovery(store: Store) -> dict[int, str]:
    """The statement the novelty check searches for: the first scorer's."""
    return {d: s[0]["statement"] for d, s in scores(store).items()}


def priority(ss: list[dict]) -> float | None:
    """Mean value x mean feasibility, lowered by the mean artefact risk: (6 - risk) / 5."""
    if not ss:
        return None
    mean = lambda f: sum(s[f] for s in ss) / len(ss)  # noqa: E731
    return round(mean("value") * mean("feasibility") * (6 - mean("artefact_risk")) / 5, 1)


def novelty_state(store: Store) -> tuple[dict[int, dict], dict]:
    """The novelty verdict per discovery, and how the recall controls fared."""
    from . import novelty
    ensure_tasks(store)
    v = novelty.verdicts(store, TOURNAMENT)
    by = {int(k[2]): e for k, e in v.items() if k[0] is None and k[1] == "discovery"}
    ctrl = [e for k, e in v.items() if k[0] == "control"]
    return by, {"controls": len(ctrl), "found": sum(bool(e["control_found"]) for e in ctrl)}


def search(store: Store, settings, log=print) -> dict:
    """Run the novelty searches of the discovery checks not searched yet, run by run (each check belongs to the
    first run of its discovery), then redo empty plain searches on OpenAlex."""
    from . import novelty
    ensure_tasks(store)
    counts: Counter = Counter()
    for (run_id,) in store.db.execute("SELECT DISTINCT run_id FROM novelty_checks WHERE tournament = ?",
                                      (TOURNAMENT,)).fetchall():
        counts.update(novelty.search(store, run_id, settings, TOURNAMENT, log=log))
        counts.update({f"plain redone: {k}": n for k, n in novelty.fill_plain(store, run_id, settings, TOURNAMENT,
                                                                              log=log).items()})
    return dict(counts)


def next_step(d: dict, ss: list[dict], nv: dict | None) -> str | None:
    """What a chosen discovery needs next in the scoring and novelty pipeline."""
    if d["status"] != "chosen":
        return None
    if not ss:
        return "score it (discovery_score)"
    if nv is None:
        return "write its search strings (novelty_queries)"
    if not nv["searched"]:
        return "run the novelty searches"
    if nv["verdict"] == "unjudged" or not nv["judges"]:
        return "judge the hits (novelty_judge, by two agents)"
    if len(nv["judges"]) < 2:
        return "judge the hits with a second agent (novelty_judge)"
    return None


# The log and the page

def write_log(store: Store, path: Path) -> Path:
    """notes/discoveries.md: every current candidate by kind, with its counts and status."""
    items = rows(store)
    ss = scores(store)
    nv, _ = novelty_state(store)
    lines = ["# Discoveries", "", f"Latest sweep: {last_sweep(store) or 'none'}. Found by code in the evidence store "
             "(`python -m psyscientist discover`); each is noticed in the studies it lists, so a pattern claimed from "
             "it is tested on other studies.", ""]
    chosen = sorted((d for d in items if d["status"] == "chosen"),
                    key=lambda d: -(priority(ss.get(d["discovery_id"], [])) or 0))
    if chosen:
        lines += ["## Chosen", "", "| # | Discovery | Statement | Output | Value | Feasibility | Artefact risk | "
                  "Priority | Novelty |", "|---|---|---|---|---|---|---|---|---|"]
        for d in chosen:
            s = ss.get(d["discovery_id"], [])
            mean = (lambda f: f"{sum(x[f] for x in s) / len(s):.1f}") if s else (lambda f: "")
            v = nv.get(d["discovery_id"])
            lines.append(f"| {d['discovery_id']} | {d['title']} | {s[0]['statement'] if s else '*not scored*'} | "
                         f"{s[0]['output'] if s else ''} | {mean('value')} | {mean('feasibility')} | "
                         f"{mean('artefact_risk')} | {priority(s) or ''} | {v['verdict'] if v else 'not checked'} |")
        lines.append("")
    for kind in KINDS:
        these = [d for d in items if d["kind"] == kind]
        if not these:
            continue
        lines += [f"## {KIND_LABELS[kind]} ({len(these)})", "", KIND_HELP[kind], ""]
        for d in these:
            mark = {"chosen": " **[chosen]**", "set_aside": " [set aside]"}.get(d["status"], "")
            lines.append(f"- **#{d['discovery_id']}** [{','.join(d['corpora'])}] {d['title']}{mark}  \n"
                         f"  {d['summary']}" + (f"  \n  Note: {d['note']}" if d["note"] else ""))
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


NOVELTY_LABELS = {"novel": "no prior test found", "near": "a neighbour was tested", "prior": "already stated or tested",
                  "contested": "judges disagree", "unjudged": "hits not judged yet",
                  "not_searchable": "nothing readable came back", "not_searched": "not searched yet"}


def _assessment(d: dict, ss: list[dict], nv: dict | None, step: str | None) -> str:
    """The chosen discovery's scores, novelty verdict and next step."""
    if d["status"] != "chosen":
        return ""
    parts = []
    if ss:
        first = ss[0]
        mean = lambda f: sum(s[f] for s in ss) / len(ss)  # noqa: E731
        parts.append(f'<p class="statement">“{_e(first["statement"])}”</p>'
                     f'<p class="scores"><span class="out">{_e(first["output"].replace("_", " "))}</span>'
                     f' value <b>{mean("value"):.1f}</b> · feasibility <b>{mean("feasibility"):.1f}</b>'
                     f' ({_e(first["basis"].replace("_", " "))}) · artefact risk <b>{mean("artefact_risk"):.1f}</b>'
                     f' · priority <b>{priority(ss)}</b> <span class="muted">({len(ss)} '
                     f'{"scorer" if len(ss) == 1 else "scorers"})</span></p>')
        parts.append("<details><summary>Why, risks and next step</summary>" + "".join(
            f'<p><b>{_e(s["agent"])}</b>: {_e(s["why"])}'
            + (f' <i>Risks:</i> {_e(s["risks"])}' if s["risks"] else "")
            + f' <i>Next:</i> {_e(s["next_step"])}</p>' for s in ss) + "</details>")
    if nv:
        hits = nv["prior"] or nv["contested"] or nv["near"]
        parts.append(f'<p class="novelty"><span class="nv {_e(nv["verdict"])}">{_e(NOVELTY_LABELS.get(nv["verdict"], nv["verdict"]))}'
                     f'</span> <span class="muted">{nv["hits"]} hits, {len(nv["judges"])} '
                     f'{"judge" if len(nv["judges"]) == 1 else "judges"}</span></p>'
                     + "".join(f'<p class="hit">' + (f'<a href="{_e(h["link"])}" target="_blank" rel="noopener">'
                                                       f'{_e(h["title"])}</a>' if h["link"] else _e(h["title"]))
                               + (f' ({_e(h["year"])})' if h["year"] else "")
                               + (f' — “{_e(_short(h["quote"], 200))}”' if h.get("quote") else "") + "</p>"
                               for h in hits[:3]))
    if step:
        parts.append(f'<p class="next">Next: {_e(step)}</p>')
    return f'<div class="assess">{"".join(parts)}</div>'


def _card(d: dict, runs: dict[str, int], ss: list[dict] | None = None, nv: dict | None = None) -> str:
    ss = ss or []
    ev = d["evidence"]
    badges = "".join(f'<a class="badge-c" href="/run/{runs.get(c, d["runs"][0])}" title="Open project {_e(c)}">{_e(c)}</a>'
                     for c in d["corpora"])
    details = []
    if d["kind"] == "contradiction" and ev.get("moderators"):
        details.append("<p><b>Candidate moderators</b> (share of studies on each side):</p><ul>" + "".join(
            f"<li>{_e(_pretty(m['field']))} = {_e(_pretty(m['value']))}: {m['up']} of {len(ev['up'])} vs "
            f"{m['down']} of {len(ev['down'])}</li>" for m in ev["moderators"]) + "</ul>")
    if d["kind"] == "bridge":
        details.append("<ul>" + "".join(
            f"<li><b>{_e(k)}</b>: {s['n']} studies; outcomes "
            f"{_e(', '.join(_pretty(x) for x in s['labels']))}; causes "
            f"{_e(', '.join(_pretty(x.split('=', 1)[1]) for x in list(s['causes'])[:5]) or 'none coded')}</li>"
            for k, s in ev["sides"].items()) + "</ul>")
    if d["kind"] == "cluster":
        details.append(f"<p>Insights #{_e(', #'.join(map(str, ev['insights'][:20])))}"
                       + (" …" if len(ev["insights"]) > 20 else "") + "</p>")
    quotes = "".join(
        f'<blockquote><span class="badge-c sm">{_e(q.get("corpus", ""))}</span> “{_e(_short(q.get("text", ""), 320))}”'
        f'<cite>#{_e(q.get("work_id"))} {_e(_short(q.get("title", ""), 90))}'
        + (f" ({_e(q['year'])})" if q.get("year") else "") + "</cite></blockquote>"
        for q in ev.get("quotes") or [])
    works = ev.get("works") or []
    details.append(f'<p class="muted">Noticed in {ev.get("n_works") or len(set(works))} studies'
                   + (f", {len(ev.get('findings') or [])} findings" if ev.get("findings") else "")
                   + ". A pattern claimed from it is tested on other studies.</p>")
    actions = (f'<button class="btn primary" data-act="chosen">Explore this</button>'
               f'<button class="btn" data-act="set_aside">Set aside</button>' if d["status"] == "open" else
               f'<span class="state {_e(d["status"])}">{_e(d["status"].replace("_", " "))}</span>'
               f'<button class="btn" data-act="open">Reopen</button>')
    return (f'<article class="card disc" data-id="{d["discovery_id"]}" data-kind="{_e(d["kind"])}" '
            f'data-status="{_e(d["status"])}" data-corpora="{_e(" ".join(d["corpora"]))}">'
            f'<header><span class="kind {_e(d["kind"])}">{_e(KIND_LABELS[d["kind"]])}</span>{badges}'
            f'<span class="muted strength" title="strength within its kind">{_e(d["strength"])}</span></header>'
            f'<h2>{_e(d["title"])}</h2><p class="about">{_e(d["summary"])}</p>'
            + _assessment(d, ss, nv, next_step(d, ss, nv))
            + f'<details><summary>Evidence</summary>{"".join(details)}{quotes}</details>'
            + (f'<p class="note">Note: {_e(d["note"])}</p>' if d["note"] else "")
            + f'<footer>{actions}<input class="note-in" placeholder="Note (optional)"></footer></article>')


def _pipeline(items: list[dict], ss: dict, nv: dict, controls: dict, root: Path) -> str:
    """The chosen discoveries' agent tasks: prepare each step, run the searches, import answered tasks."""
    chosen = [d for d in items if d["status"] == "chosen"]
    if not chosen:
        return ""
    steps = Counter(next_step(d, ss.get(d["discovery_id"], []), nv.get(d["discovery_id"])) for d in chosen)
    waiting = "; ".join(f"{n} to {s}" for s, n in steps.items() if s) or "every chosen discovery is scored and judged"
    rows_ = "".join(
        f'<li><code>{_e(t["folder"])}</code> <span class="muted">{_e(t["records"])} items · {_e(t["state"])}</span>'
        + (f' <button class="btn" data-job="discovery_import" data-folder="{_e(t["folder"])}">Import answers</button>'
           if t["state"] == "answered" else "") + "</li>" for t in tasks(root))
    return ('<section class="block pipeline"><h2>Chosen discoveries <span class="count">' + str(len(chosen))
            + '</span></h2><p class="muted">Agents score what you chose (a testable statement, value, feasibility, '
            "artefact risk), then the novelty check searches the literature for each statement and two judges label "
            "the hits, as for the lenses' hypotheses. An agent answers each prepared task in a Claude session; import "
            f"its answers here. Waiting: {_e(waiting)}."
            + (f" Recall controls found by the searches: {controls['found']} of {controls['controls']}."
               if controls["controls"] else "") + "</p>"
            '<p class="row"><input id="agent" class="note-in" value="' + DEFAULT_AGENT + '" aria-label="agent name">'
            '<button class="btn" data-job="discovery_task" data-kind="discovery_score">Prepare scoring</button>'
            '<button class="btn" data-job="discovery_task" data-kind="novelty_queries">Prepare search strings</button>'
            '<button class="btn" data-job="discovery_search">Run searches</button>'
            '<button class="btn" data-job="discovery_task" data-kind="novelty_judge">Prepare judging</button></p>'
            + (f'<ul class="tasks">{rows_}</ul>' if rows_ else "") + "</section>")


def page(store: Store, root: Path = ROOT, token: str | None = None) -> str:
    items = rows(store)
    ss = scores(store)
    nv, controls = novelty_state(store)
    # chosen discoveries first, by priority; the rest by kind and strength
    items.sort(key=lambda d: (d["status"] != "chosen", -(priority(ss.get(d["discovery_id"], [])) or 0)))
    runs = {p["corpus"]: p["run"] for p in projects.brief(store, root)}
    kinds = Counter(d["kind"] for d in items)
    status = Counter(d["status"] for d in items)
    corpora = sorted({c for d in items for c in d["corpora"]})
    sweep_at = last_sweep(store)
    chips = ('<div class="chips" id="statuses">'
             + "".join(f'<button class="chip{" on" if s == "open" else ""}" data-status="{s}">{label} <b>{status[s]}</b>'
                       "</button>" for s, label in (("open", "To choose"), ("chosen", "Chosen"),
                                                    ("set_aside", "Set aside")))
             + '</div><div class="chips" id="kinds"><button class="chip on" data-kind="">All kinds</button>'
             + "".join(f'<button class="chip" data-kind="{k}" title="{_e(KIND_HELP[k])}">{_e(KIND_LABELS[k])} '
                       f"<b>{kinds[k]}</b></button>" for k in KINDS if kinds[k]) + "</div>"
             + '<div class="chips" id="corpora"><button class="chip on" data-corpus="">All projects</button>'
             + "".join(f'<button class="chip" data-corpus="{_e(c)}">{_e(c)}</button>' for c in corpora) + "</div>")
    intro = ('<section class="block"><h2>Discovery</h2><p class="muted">Candidate directions found by code in all '
             "projects' findings, charting and insights: contradictions to explain, gaps in the map, outcomes two "
             "projects share, insights noticed more than once and rising topics. No model is involved; each card "
             "shows the counts and passages it rests on. Choose what to explore; the rest stays here."
             + (f" Latest sweep: {_e(sweep_at[:16].replace('T', ' '))} UTC." if sweep_at else " No sweep yet.")
             + '</p><p><button class="btn primary" data-job="discover">Run discovery</button> <span id="job" '
             'class="muted"></span></p></section>')
    cards = "".join(_card(d, runs, ss.get(d["discovery_id"]), nv.get(d["discovery_id"])) for d in items)
    body = intro + _pipeline(items, ss, nv, controls, root) + (
        chips + '<div class="grid">' + cards + "</div>" if items else '<p class="muted">Nothing found yet. Run discovery.</p>')
    out = (projects.PAGE.replace("<title>Projects · Psych-Scientist</title>", "<title>Discovery · Psych-Scientist</title>")
           .replace('<span class="muted">Your projects</span>',
                    '<a class="btn" href="/">← All projects</a><span class="muted">Discovery</span>')
           .replace("</style>", CSS + "</style>").replace("__BODY__", body))
    return out.replace("</body>", (SCRIPT + projects.KEEPALIVE).replace("__TOKEN__", token or ""), 1)


CSS = """
.disc h2 { font-size: 16px; margin: 0; }
.disc header { flex-wrap: wrap; }
.disc .strength { margin-left: auto; }
.kind { font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 10px; background: var(--chip); }
.kind.contradiction { color: var(--exc); } .kind.gap { color: var(--uns); } .kind.bridge { color: var(--accent); }
.kind.cluster { color: var(--inc); }
a.badge-c { text-decoration: none; }
.badge-c.sm { width: 18px; height: 18px; font-size: 11px; border-radius: 5px; vertical-align: 1px; margin-right: 4px; }
.chips { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 0 0 10px; }
.chip { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 14px; padding: 3px 11px;
  font: inherit; font-size: 13px; cursor: pointer; }
.chip.on { background: var(--accent); border-color: var(--accent); color: #fff; }
.chips label { margin-left: 6px; }
details summary { cursor: pointer; font-size: 13px; color: var(--muted); }
details ul { margin: 4px 0; padding-left: 18px; font-size: 13px; }
details p { font-size: 13px; margin: 6px 0; }
blockquote { margin: 6px 0; padding: 6px 10px; border-left: 3px solid var(--line); font-size: 13px; }
blockquote cite { display: block; color: var(--muted); font-style: normal; font-size: 12px; margin-top: 2px; }
.disc footer { flex-wrap: wrap; }
.note-in { flex: 1; min-width: 120px; border: 1px solid var(--line); background: var(--bg); color: var(--ink);
  border-radius: 8px; padding: 5px 8px; font: inherit; font-size: 13px; }
.state { font-size: 13px; font-weight: 600; }
.state.chosen { color: var(--inc); } .state.set_aside { color: var(--muted); }
.note { font-size: 13px; margin: 0; }
button.btn { cursor: pointer; font: inherit; font-size: 14px; }
.hide { display: none !important; }
.assess { border-top: 1px solid var(--line); padding-top: 8px; font-size: 13px; }
.assess p { margin: 4px 0; }
.statement { font-size: 14px; }
.out { font-weight: 600; padding: 1px 7px; border-radius: 9px; background: var(--chip); }
.nv { font-weight: 600; } .nv.novel { color: var(--inc); } .nv.near, .nv.contested { color: var(--uns); }
.nv.prior { color: var(--exc); } .nv.unjudged, .nv.not_searched, .nv.not_searchable { color: var(--muted); }
.hit { color: var(--muted); }
.next { color: var(--accent); font-weight: 600; }
.pipeline .row { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
.pipeline .row .note-in { flex: 0 1 230px; }
.tasks { list-style: none; padding: 0; margin: 8px 0 0; font-size: 13px; }
.tasks li { padding: 4px 0; border-top: 1px solid var(--line); display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
"""

SCRIPT = """<script>
(function () {
  const TOKEN = "__TOKEN__";
  const post = (url, body) => fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-Token": TOKEN},
                                          body: JSON.stringify(body)}).then(r => r.json());
  let kind = "", corpus = "", status = "open";
  try { status = sessionStorage.getItem("discovery-status") || "open"; } catch (e) {}
  function filter() {
    document.querySelectorAll(".disc").forEach(c => {
      const ok = (!kind || c.dataset.kind === kind) && (!corpus || c.dataset.corpora.split(" ").includes(corpus))
                 && c.dataset.status === status;
      c.classList.toggle("hide", !ok);
    });
    document.querySelectorAll("#statuses .chip").forEach(x => x.classList.toggle("on", x.dataset.status === status));
  }
  document.querySelectorAll("#statuses .chip").forEach(b => b.onclick = () => {
    status = b.dataset.status; try { sessionStorage.setItem("discovery-status", status); } catch (e) {} filter(); });
  document.querySelectorAll("#kinds .chip").forEach(b => b.onclick = () => {
    kind = b.dataset.kind; document.querySelectorAll("#kinds .chip").forEach(x => x.classList.toggle("on", x === b)); filter(); });
  document.querySelectorAll("#corpora .chip").forEach(b => b.onclick = () => {
    corpus = b.dataset.corpus; document.querySelectorAll("#corpora .chip").forEach(x => x.classList.toggle("on", x === b)); filter(); });
  document.querySelectorAll(".disc button[data-act]").forEach(b => b.onclick = async () => {
    const card = b.closest(".disc");
    const res = await post("/api/discovery", {id: card.dataset.id, status: b.dataset.act,
                                              note: card.querySelector(".note-in").value});
    if (res.error) { alert(res.error); return; }
    location.reload();
  });
  const job = document.getElementById("job"), agent = document.getElementById("agent");
  document.querySelectorAll("button[data-job]").forEach(b => b.onclick = async () => {
    const body = {action: b.dataset.job, kind: b.dataset.kind, folder: b.dataset.folder, agent: agent ? agent.value : ""};
    const out = b.closest(".pipeline") ? (b.closest(".pipeline").querySelector(".job-out") ||
      b.closest(".pipeline").appendChild(Object.assign(document.createElement("p"), {className: "muted job-out"}))) : job;
    const res = await post("/api/job", body);
    if (res.error) { out.textContent = res.error; return; }
    document.querySelectorAll("button[data-job]").forEach(x => x.disabled = true);
    out.textContent = "Running…";
    const poll = setInterval(async () => {
      const jobs = await fetch("/api/jobs").then(r => r.json());
      const j = jobs.find(x => x.id === res.job);
      if (!j || j.status === "running") { out.textContent = "Running… " + ((j && j.log.slice(-1)[0]) || ""); return; }
      clearInterval(poll);
      document.querySelectorAll("button[data-job]").forEach(x => x.disabled = false);
      if (j.status === "failed") { out.textContent = "Failed: " + j.error; return; }
      location.reload();
    }, 1500);
  });
  filter();
})();
</script>
"""
