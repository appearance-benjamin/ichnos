"""Pre-registered patterns: generalisations across studies, counted by code and judged on the studies they were not
drawn from.

A pattern (`[[patterns]]` in the protocol) is written down before the coding it needs: which studies count
(`among`), what it expects of them (`expect`), and for a contrast the two groups it compares (`a`, `b`). Its measure
is a share (expect among the studies counted) or a contrast (the share in group a minus the share in group b), and
`holds = [low, high]` is the range in which it holds (low inclusive, high exclusive unless it is 1). Each lens of the
corpus's lens file says beforehand whether the pattern holds, fails or is outside what it predicts.

The discovery studies of a pattern are those cited by the insights that proposed it (`noticed_in`); a pattern is
judged on the other studies only, and the discovery studies are counted beside them. Reports that may share a sample
(the overlap groups) count once, with their coded values pooled. Code, not agents, does the counting.

`[comparator] predictions` use the same form on another corpus's coding: the out-of-sample test of the lenses.

A second coder codes the same dimensions independently (`agent-tasks extract <run> --dims ... --second-coder`). Its
answers go to `second_codings`, never to the charting, so the primary coding stays what the tables and patterns use;
`agreement` gives Cohen's kappa per category, and a pattern that rests on a category below `[pattern_test] kappa_min`
(default 0.6) is reported as unreliable.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .agreement import cohen_kappa
from .config import ROOT
from .store import Store, now

MEASURES = ("share", "contrast")
PREDICTIONS = ("holds", "fails", "none")
LEVELS = ("study", "finding")


SCHEMA = """
CREATE TABLE IF NOT EXISTS second_codings (
    run_id      INTEGER NOT NULL REFERENCES runs(run_id),
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    coder       TEXT NOT NULL,
    dimension   TEXT NOT NULL,
    value       TEXT,
    canonical   TEXT,
    evidence    TEXT,
    grounded    INTEGER,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (run_id, work_id, coder, dimension)
);
"""
UNCODED = {"not_reported", "none_described", "not_determinable"}


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def settings(protocol: dict) -> dict:
    s = protocol.get("pattern_test") or {}
    return {"min_n": int(s.get("min_n", 5)), "min_group": int(s.get("min_group", 3)),
            "kappa_min": float(s.get("kappa_min", 0.6))}


# The second coder

def add_second(store: Store, run_id: int, work_id: int, coder: str, dimension: str, value: str, canonical: str,
               evidence: str | None, grounded: bool | None, call_id: int | None = None) -> None:
    ensure(store)
    with store.db:
        store.db.execute("INSERT OR REPLACE INTO second_codings (run_id, work_id, coder, dimension, value, canonical, "
                         "evidence, grounded, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (run_id, work_id, coder, dimension, value, canonical, evidence,
                          None if grounded is None else int(grounded), call_id, now()))


def _values(canonical: str | None) -> set[str]:
    return {v.strip() for v in (canonical or "").split(";") if v.strip()}


def retained_charting(store: Store, run_id: int) -> dict[int, dict]:
    """The run's charting of the studies it retains (agents.kept_ids): a record charted and excluded afterwards, by a
    recorded decision or at the eligibility stage, does not count. A run without screening decisions has no retained
    set yet, and all its charting counts."""
    from . import tables
    from .agents import kept_ids
    chart = tables.charting(store, run_id)
    if not store.db.execute("SELECT 1 FROM screening WHERE run_id = ? LIMIT 1", (run_id,)).fetchone():
        return chart
    kept = set(kept_ids(store, run_id))
    return {w: c for w, c in chart.items() if w in kept}


def agreement(store: Store, run_id: int, protocol: dict) -> list[dict]:
    """Per coded dimension and category: how often each coder chose it and Cohen's kappa on the retained studies both
    coded, each category a yes/no decision per study; a row with `category` None is the dimension's kappa over all
    its decisions. A category neither coder chose has no kappa."""
    ensure(store)
    dims = {d["name"]: d.get("categories") or [] for d in (protocol.get("map") or {}).get("dimensions") or []}
    first = retained_charting(store, run_id)
    second: dict = {}
    for r in store.db.execute("SELECT work_id, coder, dimension, canonical FROM second_codings WHERE run_id = ? "
                              "ORDER BY work_id", (run_id,)):
        second.setdefault(r["dimension"], {})[r["work_id"]] = (r["coder"], _values(r["canonical"]))
    out = []
    for dim in [d for d in dims if d in second]:
        coded = second[dim]
        cats = [c for c in dims[dim] if c not in UNCODED]
        works = [w for w in coded if dim in first.get(w, {})]
        pooled, rows = [], []
        for c in cats:
            pairs = [(str(c in set(first[w][dim]["values"])), str(c in coded[w][1])) for w in works]
            pooled += pairs
            n_a, n_b = sum(a == "True" for a, _ in pairs), sum(b == "True" for _, b in pairs)
            rows.append({"dimension": dim, "category": c, "n": len(pairs), "first": n_a, "second": n_b,
                         "kappa": cohen_kappa(pairs) if (n_a or n_b) else None})
        out.append({"dimension": dim, "category": None, "n": len(works), "coder": next(iter(coded.values()))[0],
                    "kappa": cohen_kappa(pooled) if pooled else None})
        out += rows
    return out


def unreliable(p: dict, agree: list[dict], kappa_min: float) -> list[str]:
    """The categories this pattern rests on whose agreement between the coders is below the bar."""
    low = {(a["dimension"], a["category"]) for a in agree
           if a["category"] and a["kappa"] is not None and a["kappa"] < kappa_min}
    used = {(c["dimension"], v) for _, c in _conditions(p) for v in c.get("values") or []}
    return sorted(f"{d}: {c}" for d, c in used & low)


def _vocab(protocol: dict) -> tuple[dict, dict]:
    """The coded dimensions of studies and the coded fields of findings: {name: categories}."""
    dims = {d["name"]: list(d.get("categories") or []) for d in (protocol.get("map") or {}).get("dimensions") or []}
    fields = {f["name"]: list(f.get("categories") or []) for f in (protocol.get("findings") or {}).get("fields") or []}
    return dims, fields


def _conditions(p: dict) -> list[tuple[str, dict]]:
    out = [("among", c) for c in p.get("among") or []]
    out += [(k, c) for k in ("a", "b") for c in p.get(k) or []]
    if p.get("expect"):
        out.append(("expect", p["expect"]))
    return out


def check(items: list[dict], protocol: dict, lens_ids: list[str] | None = None, insight_ids: set[int] | None = None
          ) -> list[str]:
    """What stops these patterns from being counted as written (empty when nothing does). `protocol` is the one whose
    coding they are counted on; `lens_ids` the lenses that must each make a prediction."""
    dims, fields = _vocab(protocol)
    errs, seen = [], set()
    for p in items:
        pid = p.get("id") or "?"
        if pid in seen:
            errs.append(f"{pid}: the id is used twice")
        seen.add(pid)
        for key in ("id", "name", "statement", "expect", "holds"):
            if not p.get(key):
                errs.append(f"{pid}: needs `{key}`")
        level = p.get("level", "study")
        if level not in LEVELS:
            errs.append(f"{pid}: level must be one of {LEVELS}")
        measure = p.get("measure", "contrast" if p.get("a") or p.get("b") else "share")
        if measure not in MEASURES:
            errs.append(f"{pid}: measure must be one of {MEASURES}")
        if measure == "contrast" and not (p.get("a") and p.get("b")):
            errs.append(f"{pid}: a contrast needs groups `a` and `b`")
        if p.get("uncoded", "skip") not in ("skip", "no"):
            errs.append(f"{pid}: uncoded must be \"skip\" (leave uncoded studies out) or \"no\" (count them as not meeting it)")
        h = p.get("holds")
        lo_ok = -1 if measure == "contrast" else 0
        if not (isinstance(h, list) and len(h) == 2 and all(isinstance(x, (int, float)) for x in h)
                and lo_ok <= h[0] < h[1] <= 1):
            errs.append(f"{pid}: holds must be [low, high] with {lo_ok} <= low < high <= 1")
        for where, c in _conditions(p):
            name, values = c.get("dimension"), c.get("values")
            if level == "finding" and name in fields:          # a finding's own field wins over its study's
                known = fields[name]
            elif name in dims:
                known = dims[name]
            elif name in fields:
                errs.append(f"{pid} {where}: {name} is a findings field; use level = \"finding\"")
                continue
            else:
                known = None
            if known is None:
                errs.append(f"{pid} {where}: no coded dimension or field named {name!r}")
            elif not known:
                errs.append(f"{pid} {where}: {name} has no categories to count")
            elif not values or not isinstance(values, list):
                errs.append(f"{pid} {where}: needs a list of `values`")
            else:
                bad = [v for v in values if v not in known]
                if bad:
                    errs.append(f"{pid} {where}: {name} has no categor{'y' if len(bad) == 1 else 'ies'} {bad}")
        preds = p.get("predictions") or {}
        for lens, said in preds.items():
            if said not in PREDICTIONS:
                errs.append(f"{pid}: the prediction of {lens} must be one of {PREDICTIONS}")
        if lens_ids is not None:
            missing, unknown = sorted(set(lens_ids) - set(preds)), sorted(set(preds) - set(lens_ids))
            if missing:
                errs.append(f"{pid}: no prediction from {missing}")
            if unknown:
                errs.append(f"{pid}: predictions from lenses not in the lens file: {unknown}")
        if insight_ids is not None:
            gone = [i for i in p.get("noticed_in") or [] if i not in insight_ids]
            if gone:
                errs.append(f"{pid}: noticed_in names insights that do not exist: {gone}")
    return errs


# Counting

def _match(values: dict[str, set], conds: list[dict]) -> bool:
    return all(set(c["values"]) & values.get(c["dimension"], set()) for c in conds)


def _coded(values: dict[str, set], conds: list[dict]) -> bool:
    return all(values.get(c["dimension"]) for c in conds)


def measure(units: list[dict], p: dict) -> dict:
    """The pattern's measure on these units (each {"values": {name: set}}): n counted, and for a contrast the groups."""
    # A unit without the expected coding is left out, unless the pattern is about whether it is coded at all
    # (`uncoded = "no"`: it then counts as not meeting the expectation).
    need = [] if p.get("uncoded") == "no" else [p["expect"]]
    among = [u for u in units if _coded(u["values"], p.get("among") or []) and _match(u["values"], p.get("among") or [])
             and _coded(u["values"], need)]

    def share(us):
        return (sum(1 for u in us if _match(u["values"], [p["expect"]])) / len(us)) if us else None

    def studies(us):        # finding units cluster within studies: how many studies stand behind them
        return len({w for u in us for w in u["works"]})
    if p.get("a") or p.get("b"):
        ga = [u for u in among if _match(u["values"], p["a"])]
        gb = [u for u in among if _match(u["values"], p["b"])]
        sa, sb = share(ga), share(gb)
        return {"n": len(among), "n_a": len(ga), "n_b": len(gb), "share_a": sa, "share_b": sb,
                "studies": studies(among), "studies_a": studies(ga), "studies_b": studies(gb),
                "value": None if sa is None or sb is None else sa - sb}
    return {"n": len(among), "studies": studies(among), "value": share(among)}


def verdict(p: dict, m: dict, s: dict) -> str:
    if not m["n"]:
        return "not coded"
    if (p.get("a") or p.get("b")) and min(m["n_a"], m["n_b"]) < s["min_group"]:
        return "too few to judge"
    if m["n"] < s["min_n"] or m["value"] is None:
        return "too few to judge"
    lo, hi = p["holds"]
    return "holds" if lo <= m["value"] < hi or (hi == 1 and m["value"] == 1) else "fails"


def judge(units: list[dict], items: list[dict], s: dict) -> list[dict]:
    """Each pattern judged on its test units (those it was not drawn from), with the discovery units beside it, and
    whether each lens's prediction came true."""
    out = []
    for p in items:
        found = set(p.get("_discovery") or [])
        test = [u for u in units if not set(u["works"]) & found]
        disc = [u for u in units if set(u["works"]) & found]
        m_test, m_disc = measure(test, p), measure(disc, p)
        v = verdict(p, m_test, s)
        if v == "not coded" and any(_coded(u["values"], [p["expect"]]) for u in units):
            v = "too few to judge"          # coded, but no test study qualifies (all are discovery studies)
        lenses = {}
        for lens, said in (p.get("predictions") or {}).items():
            lenses[lens] = ("no prediction" if said == "none" else "open" if v not in ("holds", "fails")
                            else "right" if said == v else "wrong")
        out.append({"id": p["id"], "name": p.get("name"), "level": p.get("level", "study"), "test": m_test,
                    "discovery": m_disc, "verdict": v,
                    "lenses": lenses, "blind": p.get("blind", True)})
    return out


def units(store: Store, run_id: int, protocol: dict, level: str, second: bool = False) -> list[dict]:
    """Study units (overlapping reports pooled) or finding units, each with its coded values and its works; only the
    retained studies count. `second`: the dimensions the second coder coded take its values instead of the
    charting's (a robustness check)."""
    from . import tables
    chart = retained_charting(store, run_id)
    if second:
        ensure(store)
        chart = {w: dict(c) for w, c in chart.items()}
        for r in store.db.execute("SELECT work_id, dimension, canonical FROM second_codings WHERE run_id = ?",
                                  (run_id,)):
            if r["work_id"] in chart:
                chart[r["work_id"]][r["dimension"]] = {"values": sorted(_values(r["canonical"]))}
    dims, fields = _vocab(protocol)
    try:
        root = tables.overlaps(store, run_id, protocol)["root"]
    except Exception:                         # a run without retained records has no overlap groups
        root = {}

    def study_values(w):
        return {d: set(c["values"]) for d, c in chart.get(w, {}).items() if d in dims and c.get("values")
                and set(c["values"]) - {"not_reported", "none_described", "not_determinable"}}
    if level == "study":
        groups: dict = defaultdict(lambda: {"values": defaultdict(set), "works": []})
        for w in chart:
            g = groups[root.get(w, w)]
            g["works"].append(w)
            for d, vs in study_values(w).items():
                g["values"][d] |= vs
        return [{"works": g["works"], "values": dict(g["values"])} for g in groups.values()]
    out = []
    for r in tables.finding_rows(store, run_id):
        f = json.loads(r["fields"] or "{}")
        vals = study_values(r["work_id"])
        for name in fields:
            v = f.get(name)
            vs = set(v if isinstance(v, list) else [v]) if v else set()
            if vs:
                vals[name] = vs
        out.append({"works": [r["work_id"]], "values": vals, "finding": r["finding_id"]})
    return out


def discovery_works(store: Store, items: list[dict]) -> list[dict]:
    """The items with `_discovery`: the works cited by the insights that proposed each pattern."""
    out = []
    for p in items:
        ws: set[int] = set()
        for i in p.get("noticed_in") or []:
            row = store.db.execute("SELECT support FROM insights WHERE insight_id = ?", (i,)).fetchone()
            if row and row[0]:
                ws |= set(json.loads(row[0]).get("works") or [])
        out.append({**p, "_discovery": sorted(ws)})
    return out


def count(store: Store, run_id: int, protocol: dict, items: list[dict] | None = None,
          settings_from: dict | None = None, second: bool = False) -> list[dict]:
    """Every pattern (or the given items, e.g. a comparator's predictions) counted on this run's coding (with
    `second`, on the second coder's coding of the dimensions it coded)."""
    items = discovery_works(store, items if items is not None else protocol.get("patterns") or [])
    s = settings(settings_from or protocol)
    out = []
    for level in LEVELS:
        mine = [p for p in items if p.get("level", "study") == level]
        if mine:
            out += judge(units(store, run_id, protocol, level, second), mine, s)
    agree = agreement(store, run_id, protocol)
    for r in out:
        r["unreliable"] = unreliable(next(p for p in items if p["id"] == r["id"]), agree, s["kappa_min"])
    order = {p["id"]: i for i, p in enumerate(items)}
    return sorted(out, key=lambda r: order[r["id"]])


def report(results: list[dict], title: str = "Pre-registered patterns") -> str:
    def pct(x):
        return "–" if x is None else f"{x:+.2f}" if x < 0 or x > 1 else f"{x:.2f}"
    lines = [f"## {title}", "", "| Pattern | Counted in the test set (studies, or findings) | Measure | Verdict | Discovery set | Lenses |",
             "| --- | ---: | ---: | --- | --- | --- |"]
    for r in results:
        t, d = r["test"], r["discovery"]
        n = f"{t['n']}" + (f" ({t['n_a']} vs {t['n_b']})" if "n_a" in t else "")
        if r.get("level") == "finding" and t["n"]:
            n += f"; studies {t['studies']}" + (f" ({t['studies_a']} vs {t['studies_b']})" if "n_a" in t else "")
        disc = f"{d['n']}: {pct(d['value'])}" if d["n"] else "none"
        lenses = ", ".join(f"{k} {v}" for k, v in r["lenses"].items() if v != "no prediction") or "–"
        flag = f" (unreliable: {'; '.join(r['unreliable'])})" if r.get("unreliable") else ""
        lines.append(f"| {r['id']} {r['name']}{'' if r['blind'] else ' (not blind)'} | {n} | {pct(t['value'])} | "
                     f"{r['verdict']}{flag} | {disc} | {lenses} |")
    return "\n".join(lines) + "\n"


def agreement_report(rows: list[dict]) -> str:
    if not rows:
        return "No second coding yet.\n"
    lines = ["## Agreement between the two coders (Cohen's kappa)", "",
             "| Dimension | Category | Studies | First coder | Second coder | Kappa |",
             "| --- | --- | ---: | ---: | ---: | ---: |"]
    for r in rows:
        k = "–" if r["kappa"] is None else f"{r['kappa']:.2f}"
        if r["category"] is None:
            lines.append(f"| **{r['dimension']}** | all categories ({r['coder']}) | {r['n']} | | | **{k}** |")
        else:
            lines.append(f"| | {r['category']} | {r['n']} | {r['first']} | {r['second']} | {k} |")
    return "\n".join(lines) + "\n"


def validate(corpus: str, root: Path = ROOT, store: Store | None = None) -> list[str]:
    """Check a corpus's patterns against its own coding and lenses, and its comparator predictions against the other
    corpus's protocol."""
    from . import lenses
    from .config import load_protocol
    _, _, protocol = load_protocol(corpus, root)
    try:
        lens_ids = [x["id"] for x in lenses.load(corpus, root)[2].get("lenses") or []]
    except ValueError:
        lens_ids = None
    known = ({r[0] for r in store.db.execute("SELECT insight_id FROM insights WHERE corpus = ?", (corpus,))}
             if store else None)
    errs = check(protocol.get("patterns") or [], protocol, lens_ids, known)
    comp = protocol.get("comparator") or {}
    if comp.get("predictions"):
        _, _, other = load_protocol(comp["corpus"], root)
        errs += [f"comparator {e}" for e in check(comp["predictions"], other, lens_ids)]
    return errs
