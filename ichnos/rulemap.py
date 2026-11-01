"""The evidence map, one section per value of the findings' first matrix axis: tables only, no conclusions.

For a review that maps evidence rather than writing it up (corpus E maps evidence for candidate decision rules),
the deliverable is a set of tables the author reads and cites. The protocol's [findings] matrix names the axes:
the first (e.g. candidate_rule) gives the sections, the second (e.g. outcome) the columns. Each section has

- the studies: how many, their designs, contexts and framing (from the charting, [map] dimensions);
- the directions found, by the finding's input and outcome: counts of lowers / raises / no relation / mixed;
- the constructs measured alongside (a [map] dimension named in `alongside`);
- every finding, with its study, fields, subgroup and the finding sentence;
- the studies, with citation, year, country, design and a link.

Findings whose outcome is `other` stand in a table of their own (in corpus E: the tests of the quantity-quality
trade-off itself, coded by what more children do to the investment per child). Nothing is weighted or pooled: a
count is a count of findings, and each row links to the study it comes from.

Only records still kept count, and a study recorded twice counts once: records with the same abstract, the same
title, or the same OpenAlex record (resolve.duplicates) are one study, shown through the record with the most
findings; the map says how many records were merged so.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from .agents import NOT_REPORTED, kept_ids, split_values
from .config import ROOT
from .store import Store, title_key

DIRECTIONS = (("lowers", "↓"), ("raises", "↑"), ("no_relation", "="), ("mixed", "~"))
STUDY_DIMS = ("design", "development_context", "evolutionary_framing", "country")


def _label(value: str) -> str:
    return (value or "").replace("_", " ")


def _findings(store: Store, run_id: int) -> list[dict]:
    row = store.db.execute("SELECT extractor FROM findings WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                           (run_id,)).fetchone()
    if row is None:
        return []
    out = []
    for r in store.db.execute("SELECT * FROM findings WHERE run_id = ? AND extractor = ? AND status != 'superseded' "
                              "ORDER BY work_id, finding_id", (run_id, row["extractor"])):
        out.append({**dict(r), "fields": json.loads(r["fields"] or "{}")})
    return out


def _features(store: Store, run_id: int) -> dict[int, dict[str, list[str]]]:
    """Each study's charted values per dimension (the newest charting of each study)."""
    out: dict[int, dict[str, list[str]]] = defaultdict(dict)
    seen: set[tuple[int, str]] = set()
    for r in store.db.execute("SELECT work_id, dimension, canonical FROM study_features WHERE run_id = ? "
                              "ORDER BY created_at DESC", (run_id,)):
        key = (r["work_id"], r["dimension"])
        if key in seen:
            continue
        seen.add(key)
        out[r["work_id"]][r["dimension"]] = [v for v in split_values(r["canonical"] or "") if v != NOT_REPORTED]
    return out


def same_study(store: Store, run_id: int, ids: set[int], weight: dict[int, int]) -> dict[int, int]:
    """Each record -> the record that stands for its study (itself unless it is a second record of one)."""
    from . import resolve
    rows = {r["work_id"]: r for r in store.db.execute(
        f"SELECT work_id, title, abstract, doi FROM works WHERE work_id IN ({','.join('?' * len(ids))})",
        tuple(ids))} if ids else {}
    parent = {w: w for w in rows}

    def root(w):
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w
    for keyer, least in ((lambda r: (title_key(r["abstract"] or "") or "")[:300], 150),
                         (lambda r: title_key(r["title"] or "") or "", 30)):
        first: dict[str, int] = {}
        for w, r in rows.items():
            k = keyer(r)
            if len(k) >= least:
                if k in first:
                    parent[root(w)] = root(first[k])
                else:
                    first[k] = w
    for a, b in resolve.duplicates(store, run_id):
        if a in parent and b in parent:
            parent[root(a)] = root(b)
    groups: dict[int, list[int]] = defaultdict(list)
    for w in rows:
        groups[root(w)].append(w)
    best = {}
    for members in groups.values():
        lead = max(members, key=lambda w: (weight.get(w, 0), bool(rows[w]["abstract"]), bool(rows[w]["doi"]), -w))
        best.update({w: lead for w in members})
    return best


REASONS = {"a": "causal design", "b": "longitudinal, evolutionary framing",
           "c": "explicit evolutionary test, subsistence or historical population"}


def focus_studies(findings: list[dict], feats: dict, focus: dict, out_ax: str) -> dict[int, tuple[str, list[dict]]]:
    """The studies a [focus] view keeps for one rule, each with why (a, b, c; see REASONS) and its findings there: a
    finding on the rule with an outcome other than `other` and a stated direction, and (a) a causal design, or (b)
    longitudinal data with an evolutionary framing, or (c) an explicit evolutionary framing in an ancestral context."""
    design, context = focus.get("design_field", "evidence_type"), focus.get("context_field", "context")
    by: dict[int, list[dict]] = defaultdict(list)
    for f in findings:
        if f["fields"].get(out_ax) != "other" and f["fields"].get("direction") in {d for d, _ in DIRECTIONS}:
            by[f["work_id"]].append(f)
    out = {}
    for w, fs in by.items():
        types = {t for f in fs for t in split_values(f["fields"].get(design, ""))}
        where = {c for f in fs for c in split_values(f["fields"].get(context, ""))}
        frame = set(feats.get(w, {}).get(focus.get("framing_dimension", ""), []))
        if types & set(focus.get("causal", [])):
            out[w] = ("a", fs)
        elif types & set(focus.get("longitudinal", [])) and frame & set(focus.get("framed", [])):
            out[w] = ("b", fs)
        elif frame & set(focus.get("explicit", [])) and where & set(focus.get("ancestral", [])):
            out[w] = ("c", fs)
    return out


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided p of Fisher's exact test for [[a, b], [c, d]]: the tables with these margins as likely or less likely
    than the one observed."""
    from math import comb
    r1, r2, c1, n = a + b, c + d, a + c, a + b + c + d
    if not n:
        return 1.0

    def prob(x):
        return comb(r1, x) * comb(r2, c1 - x) / comb(n, c1)
    seen = prob(a)
    return min(1.0, sum(prob(x) for x in range(max(0, c1 - r2), min(r1, c1) + 1) if prob(x) <= seen * (1 + 1e-9)))


def sign_test(k: int, n: int) -> float:
    """Two-sided exact binomial p of k successes in n against a half."""
    from math import comb
    if not n:
        return 1.0
    return min(1.0, 2 * sum(comb(n, i) for i in range(max(k, n - k), n + 1)) / 2 ** n)


def odds_ratio(a: int, b: int, c: int, d: int) -> float:
    """(a/b)/(c/d), with 0.5 added to every cell when one is empty (Haldane)."""
    if 0 in (a, b, c, d):
        a, b, c, d = a + .5, b + .5, c + .5, d + .5
    return (a * d) / (b * c)


def odds_ci(a: int, b: int, c: int, d: int, z: float = 1.96) -> tuple[float, float]:
    """Woolf's 95% interval of the odds ratio on the log scale, with the same Haldane correction."""
    from math import exp, log, sqrt
    if 0 in (a, b, c, d):
        a, b, c, d = a + .5, b + .5, c + .5, d + .5
    est, se = log((a * d) / (b * c)), sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    return exp(est - z * se), exp(est + z * se)


def _cells(findings: list[dict], focus: dict, unit=None) -> dict[str, list[tuple[str, bool]]]:
    """Per context: one (direction, causal or longitudinal design) per study, or per `unit` of studies (a shared
    sample), mixed if its findings go both ways."""
    design, context = focus.get("design_field", "evidence_type"), focus.get("context_field", "context")
    strong = set((focus.get("strength") or {}).get("strong_designs", ["natural_experiment", "experiment",
                                                                      "longitudinal"]))
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for f in findings:
        for v in split_values(f["fields"].get(context, "")):
            groups[((unit or {}).get(f["work_id"], f["work_id"]), v)].append(f)
    cells: dict[str, list[tuple[str, bool]]] = defaultdict(list)
    for (_, v), fs in groups.items():
        d = study_direction(fs)
        if d:
            cells[v].append((d, bool({t for f in fs for t in split_values(f["fields"].get(design, ""))} & strong)))
    return cells


def _tally(cells: dict, test: dict, all_in: bool = False) -> tuple[int, int, int, int, Counter, Counter]:
    """The 2x2 table of the mismatch test: studies finding raises against lowers (or, with `all_in`, against every
    other direction) in ancestral and in modern contexts."""
    anc = Counter(d for v in test.get("ancestral") or [] for d, _ in cells.get(v, []))
    mod = Counter(d for v in test.get("modern") or [] for d, _ in cells.get(v, []))
    other = ("lowers", "no_relation", "mixed") if all_in else ("lowers",)
    return anc["raises"], sum(anc[x] for x in other), mod["raises"], sum(mod[x] for x in other), anc, mod


def verdict(n: int, share: float, design_share: float, rules: dict, direction: str | None = None) -> str:
    """strong, moderate, inconsistent or limited, by the protocol's [focus.strength]. Mixed results are not a
    direction: a context whose most common result is mixed is inconsistent however large its share."""
    if n < rules.get("moderate_min_studies", 5):
        return "limited"
    if direction == "mixed":
        return "inconsistent"
    if (n >= rules.get("strong_min_studies", 8) and share >= rules.get("strong_share", .75)
            and design_share >= rules.get("strong_design_share", .5)):
        return "strong"
    return "moderate" if share >= rules.get("moderate_share", .6) else "inconsistent"


def selection(store: Store, run_id: int, protocol: dict, focus: dict | None = None):
    """The [focus] view as data: {rule: {work_id: (why, findings)}} and the studies' charted values, from the kept
    studies with second records merged (the same selection the focused map shows)."""
    focus = focus or protocol.get("focus") or {}
    spec = protocol.get("findings") or {}
    fields = {f["name"]: f for f in spec.get("fields") or []}
    rule_ax, out_ax = [a for a in spec.get("matrix") or [] if a in fields][:2]
    kept = set(kept_ids(store, run_id))
    finds = [f for f in _findings(store, run_id) if f["work_id"] in kept]
    feats = {w: d for w, d in _features(store, run_id).items() if w in kept}
    lead = same_study(store, run_id, {f["work_id"] for f in finds} | set(feats), Counter(f["work_id"] for f in finds))
    finds = [f for f in finds if lead.get(f["work_id"], f["work_id"]) == f["work_id"]]
    feats = {w: d for w, d in feats.items() if lead.get(w, w) == w}
    chosen = {r: focus_studies([f for f in finds if r in split_values(f["fields"].get(rule_ax, ""))], feats, focus,
                               out_ax) for r in focus.get("rules") or []}
    return chosen, feats


def focus_finding_ids(store: Store, run_id: int, protocol: dict) -> set[int]:
    chosen, _ = selection(store, run_id, protocol)
    return {f["finding_id"] for by_study in chosen.values() for _, fs in by_study.values() for f in fs}


def statements_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """The paper's table of evidence statements: per rule, the studies' directions by context with the verdict, and
    the pre-specified test of ancestral against modern contexts."""
    focus = protocol.get("focus") or {}
    chosen, _ = selection(store, run_id, protocol)
    n = len({w for by_study in chosen.values() for w in by_study})
    lines = [f"**{label}.** Evidence by rule and demographic context: {n} studies, each counted once per context "
             "with its direction there (↓ more of the cue goes with lower fertility, ↑ with higher, = no relation, "
             "~ mixed: its findings go both ways). Verdicts by the rules fixed in the protocol before the verified codes "
             "were counted: strong = "
             "at least 8 studies, at least 75% in one direction and at least half with a causal or longitudinal design; "
             "moderate = at least 5 studies and at least 60% in one direction; inconsistent = no direction reaches 60%; "
             "limited = fewer than 5 studies. The test compares the share finding ↑ among studies with a direction in "
             "ancestral contexts (subsistence, historical) with that in modern ones (transitioning, high-income); "
             "Fisher's exact test, two-sided. Codes were assigned by language-model agents and checked against each "
             "finding's passage by a second agent; no person checked them.", ""]
    for r, by_study in chosen.items():
        lines += _statements(r, [f for _, fs in by_study.values() for f in fs], focus, heading=False)
    return "\n".join(lines).rstrip()


def named_samples(store: Store, protocol: dict, ids: set[int]) -> dict[int, int]:
    """Studies the protocol names as reports of one sample ([[overlaps]], by DOI or title), each mapped to the
    group's first study; studies not named map to themselves."""
    from . import tables
    meta = tables.works(store, ids)
    parent = {w: w for w in ids}

    def root(w):
        while parent[w] != w:
            w = parent[w]
        return w
    for o in protocol.get("overlaps") or []:
        dois = {d.lower() for d in o.get("dois") or []}
        keys = [title_key(t) or "" for t in o.get("titles") or []]
        ws = sorted(w for w in ids if (meta[w].get("doi") or "").lower() in dois
                    or any(k and (title_key(meta[w].get("title") or "") or "").startswith(k) for k in keys))
        for w in ws[1:]:
            parent[root(w)] = root(ws[0])
    return {w: root(w) for w in ids}


def sensitivity_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """The mismatch test of Table 1 again under other choices ([focus.sensitivity]; added after peer review, so not
    pre-specified): causal or longitudinal findings only in every context, full-text studies only, each named shared
    sample once, studies with no relation or mixed results counted as not raising, completed fertility only, the
    rule's inputs split (e.g. education against other skills or body condition), and, with `sign_test`, whether each
    side on its own leans one way (a rule whose direction holds on both sides shows no difference in the test)."""
    focus = protocol.get("focus") or {}
    sens, test = focus.get("sensitivity") or {}, focus.get("mismatch") or {}
    chosen, _ = selection(store, run_id, protocol)
    full = {r["work_id"] for r in store.db.execute("SELECT work_id FROM fulltexts WHERE status = 'parsed'")}
    strong = set((focus.get("strength") or {}).get("strong_designs", ["natural_experiment", "experiment",
                                                                      "longitudinal"]))
    design = focus.get("design_field", "evidence_type")
    has = lambda f, field, values: bool(set(split_values(f["fields"].get(field, ""))) & set(values))  # noqa: E731
    lines = [f"**{label}.** Sensitivity of the ancestral-against-modern test (Table 1) to choices of selection, "
             "counting and measurement. These analyses were added after the first peer review and were not "
             "specified in advance. Each row repeats the test with Fisher's exact test (two-sided) and the odds ratio "
             "with Woolf's 95% confidence interval (0.5 added to every cell when one is empty); ↑ = studies in which "
             "more of the cue goes with higher fertility, of the studies with a direction (raises or lowers) unless "
             "the row says otherwise."
             + (" The last row of each rule asks instead whether each side on its own leans one way, so that a "
                "direction shared by both sides is told apart from no evidence; this row and the split of kin support "
                "by its input were added later still, after the article's second version." if sens.get("sign_test")
                else ""), "",
             "| Rule | Analysis | Ancestral ↑ | Modern ↑ | p | Odds ratio (95% CI) |",
             "|---|---|---:|---:|---:|---|"]
    changed: dict[str, list] = {}
    for r, by_study in chosen.items():
        finds = [f for _, fs in by_study.values() for f in fs]
        rows = [("As pre-specified (Table 1)", finds, None, False),
                ("Causal or longitudinal findings only, in every context",
                 [f for f in finds if has(f, design, strong)], None, False),
                ("Studies read in full text only", [f for f in finds if f["work_id"] in full], None, False)]
        if protocol.get("overlaps"):
            rows.append(("Each shared sample named in the protocol counted once", finds,
                         named_samples(store, protocol, set(by_study)), False))
        rows.append(("No relation and mixed counted as not raising", finds, None, True))
        if sens.get("outcomes"):
            rows.append(("Completed fertility, fertility rates, childlessness or reproductive success only",
                         [f for f in finds if has(f, focus.get("outcome_field", "outcome"), sens["outcomes"])],
                         None, False))
        field = sens.get("input_field", "modern_input")
        for values in [(sens.get("inputs") or {}).get(r)] if (sens.get("inputs") or {}).get(r) else []:
            name = ", ".join(_label(v) for v in values)
            rows += [(f"Input {name} only", [f for f in finds if has(f, field, values)], None, False),
                     (f"Inputs other than {name}", [f for f in finds if not has(f, field, values)], None, False)]
        if protocol.get("overlaps"):
            before, after = (_verdicts(_cells(finds, focus, u), focus.get("strength") or {})
                             for u in (None, named_samples(store, protocol, set(by_study))))
            changed[r] = [(v, before[v], after.get(v)) for v in before if after.get(v, ("",))[0] != before[v][0]]
        for name, fs, unit, all_in in rows:
            a, b, c, d, _, _ = _tally(_cells(fs, focus, unit), test, all_in)
            share = lambda x, n: f"{x} of {n}" + (f" ({x / n:.0%})" if n else "")  # noqa: E731
            if a + b and c + d:
                lo, hi = odds_ci(a, b, c, d)
                lines.append(f"| {_label(r)} | {name} | {share(a, a + b)} | {share(c, c + d)} | "
                             f"{fisher_exact(a, b, c, d):.3g} | {odds_ratio(a, b, c, d):.1f} ({lo:.1f} to {hi:.1f}) |")
            else:
                lines.append(f"| {_label(r)} | {name} | {share(a, a + b)} | {share(c, c + d)} | – | no test |")
        if sens.get("sign_test"):
            a, b, c, d, _, _ = _tally(_cells(finds, focus), test)
            lines.append(f"| {_label(r)} | Direction on each side: the share finding ↑ against one half (two-sided sign "
                         f"test, p for each side) | {a} of {a + b}, p = {sign_test(a, a + b):.3g} | {c} of {c + d}, "
                         f"p = {sign_test(c, c + d):.3g} | – | – |")
    if protocol.get("overlaps"):
        moves = [f"{_label(r)} in {_label(v)} populations, {was[0]} ({was[2]} of {was[1]} studies: {_label(was[3])}) "
                 f"becomes {now[0]} ({now[2]} of {now[1]}: {_label(now[3])})" if now else
                 f"{_label(r)} in {_label(v)} populations, {was[0]}, has no studies left"
                 for r, moved in changed.items() for v, was, now in moved]
        lines += ["", "With each shared sample named in the protocol counted once, the verdicts of Table 1 "
                  + ("change as follows: " + "; ".join(moves) + "; all other verdicts stay." if moves else
                     "stay as they are.")]
    return "\n".join(lines)


def freeman_halton(top: list[int], bottom: list[int]) -> float:
    """Two-sided exact p for a 2 x k table (Fisher's exact test extended by Freeman and Halton): the tables with these
    margins as likely or less likely than the one observed."""
    from math import exp, lgamma
    cols, r1 = [a + b for a, b in zip(top, bottom)], sum(top)
    n = sum(cols)
    if not n or not r1 or r1 == n:
        return 1.0
    const = lgamma(r1 + 1) + lgamma(n - r1 + 1) - lgamma(n + 1) + sum(lgamma(c + 1) for c in cols)

    def logp(row):
        return const - sum(lgamma(x + 1) + lgamma(c - x + 1) for x, c in zip(row, cols))
    seen, total = logp(top), 0.0

    def walk(i, left, row):
        nonlocal total
        if i == len(cols) - 1:
            if left <= cols[i]:
                lp = logp(row + [left])
                total += exp(lp) if lp <= seen + 1e-9 else 0.0
            return
        for x in range(min(cols[i], left) + 1):
            walk(i + 1, left - x, row + [x])
    walk(0, r1, [])
    return min(1.0, total)


def strata_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """The mismatch test of Table 1 within strata the reviewers of the second version asked for ([focus.strata]; post
    hoc): the ancestral side restricted to natural-fertility and pre-transition historical populations, or the
    populations sorted by their stage of the transition instead of their context; each rule split by the codes named
    for it (readiness by input, status by sex and measure, kin support by kin type); causal and observational
    findings apart; each possible shared sample counted once; the studies of an earlier version against those added
    since; abstract-only studies; explicit evolutionary framing against other; and no relation and mixed kept as a
    third category."""
    from . import tables
    focus = protocol.get("focus") or {}
    st, test = focus.get("strata") or {}, focus.get("mismatch") or {}
    chosen, feats = selection(store, run_id, protocol)
    context = focus.get("context_field", "context")
    stage, keep = st.get("stage_field", "population_stage"), set(st.get("restricted_ancestral") or [])
    anc = set(test.get("ancestral") or [])
    full = {r["work_id"] for r in store.db.execute("SELECT work_id FROM fulltexts WHERE status = 'parsed'")}
    causal = set(focus.get("causal") or ["experiment", "natural_experiment"])
    strong = set((focus.get("strength") or {}).get("strong_designs", ["natural_experiment", "experiment",
                                                                      "longitudinal"]))
    design = focus.get("design_field", "evidence_type")
    has = lambda f, field, values: bool(set(split_values(f["fields"].get(field, ""))) & set(values))  # noqa: E731
    before: set[int] = set()
    if st.get("before_paper"):
        row = store.db.execute("SELECT method FROM papers WHERE paper_id = ?", (st["before_paper"],)).fetchone()
        before = {int(w) for w in (json.loads(row["method"]).get("studies") or {})} if row else set()
    frame_dim = focus.get("framing_dimension", "evolutionary_framing")
    framed = {w for w, d in feats.items() if st.get("framing", "explicit") in d.get(frame_dim, [])}
    root = tables.overlaps(store, run_id, protocol)["root"]
    by_stage = dict(focus, context_field=stage)
    stage_test = {"ancestral": sorted(keep), "modern": ["transitioning", "post_transition"]}
    skip = {"not_applicable", "unclear", "not_stated", "other"}
    share = lambda x, n: f"{x} of {n}" + (f" ({x / n:.0%})" if n else "")  # noqa: E731
    lines = [f"**{label}.** The ancestral-against-modern test of Table 1 within strata. These analyses were added "
             "after the article's second peer review and were not specified in advance; the population's stage of the "
             "transition, the sex of the focal individuals, the status measure, the kind of kin and the readiness input "
             "were coded for them by a language-model agent from each finding's statement and passage, and no person "
             "checked these codes. Each row repeats the test (Fisher's exact test, two-sided; odds ratio with Woolf's "
             "95% confidence interval) on the studies with a direction (raises or lowers) unless the row says "
             "otherwise; ↑ = more of the cue goes with higher fertility, ↓ with lower. The last row of each rule keeps the studies "
             "with no relation or mixed results as a third category (↑ / ↓ / = or ~) and tests the 2 × 3 table "
             "exactly (Freeman-Halton). A row without a study with a direction on one side gives no test. The p values "
             "are not corrected for the number of rows and are best read together, not one by one.", "",
             "| Rule | Analysis | Ancestral ↑ | Ancestral ↓ | Modern ↑ | Modern ↓ | p | Odds ratio (95% CI) |",
             "|---|---|---:|---:|---:|---:|---:|---|"]
    moves: list[tuple[str, float, list]] = []
    for r, by_study in chosen.items():
        finds = [f for _, fs in by_study.values() for f in fs]
        restricted = [f for f in finds if not (has(f, context, anc) and not has(f, stage, keep))]
        n_anc = {f["work_id"] for f in finds if has(f, context, anc)}
        n_keep = {f["work_id"] for f in finds if has(f, context, anc) and has(f, stage, keep)}
        rows = [(f"Ancestral side restricted to natural-fertility and pre-transition historical populations "
                 f"({len(n_keep)} of its {len(n_anc)} studies)", restricted, None, focus, test),
                ("By stage of the transition instead of context: natural-fertility or pre-transition against "
                 "transitioning or post-transition", finds, None, by_stage, stage_test)]
        for field in (st.get("splits") or {}).get(r) or []:
            values = sorted({v for f in finds for v in split_values(f["fields"].get(field, ""))} - skip)
            rows += [(f"{_label(field).capitalize()}: {_label(v)}", [f for f in finds if has(f, field, [v])], None,
                      focus, test) for v in values]
        out_field = focus.get("outcome_field", "outcome")
        for name, values in (st.get("outcome_groups") or {}).items():
            rows.append((f"Outcome: {name}", [f for f in finds if has(f, out_field, values)], None, focus, test))
        rows += [("Causal designs only (experiment, natural experiment)", [f for f in finds if has(f, design, causal)],
                  None, focus, test),
                 ("Observational findings only (neither causal nor longitudinal)",
                  [f for f in finds if not has(f, design, strong)], None, focus, test),
                 ("Each possible shared sample of Table S8 counted once", finds, root, focus, test)]
        if before:
            rows += [("Studies of the article's second version only", [f for f in finds if f["work_id"] in before],
                      None, focus, test),
                     ("Studies added on 2026-10-27 only", [f for f in finds if f["work_id"] not in before], None,
                      focus, test)]
        rows += [("Studies read from the abstract only", [f for f in finds if f["work_id"] not in full], None, focus,
                  test),
                 ("Studies with an explicit evolutionary framing only", [f for f in finds if f["work_id"] in framed],
                  None, focus, test),
                 ("Studies without an explicit evolutionary framing", [f for f in finds if f["work_id"] not in framed],
                  None, focus, test)]
        for name, fs, unit, fc, t in rows:
            a, b, c, d, _, _ = _tally(_cells(fs, fc, unit), t)
            # the share that goes the other way too, so the majority can be read off the row
            cells = f"{share(a, a + b)} | {share(b, a + b)} | {share(c, c + d)} | {share(d, c + d)}"
            if a + b and c + d:
                lo, hi = odds_ci(a, b, c, d)
                lines.append(f"| {_label(r)} | {name} | {cells} | "
                             f"{fisher_exact(a, b, c, d):.3g} | {odds_ratio(a, b, c, d):.1f} ({lo:.1f} to {hi:.1f}) |")
            else:
                lines.append(f"| {_label(r)} | {name} | {cells} | – | no test |")
        _, _, _, _, ca, cm = _tally(_cells(finds, focus), test)
        top = [ca["raises"], ca["lowers"], ca["no_relation"] + ca["mixed"]]
        bottom = [cm["raises"], cm["lowers"], cm["no_relation"] + cm["mixed"]]
        lines.append(f"| {_label(r)} | Three categories: ↑ / ↓ / = or ~ (Freeman-Halton) | "
                     f"{' / '.join(map(str, top))} | – | {' / '.join(map(str, bottom))} | – | "
                     f"{freeman_halton(top, bottom):.3g} | – |")
        rules = focus.get("strength") or {}
        base = _verdicts(_cells(finds, focus), rules)
        for step in (-.05, .05):
            moved = dict(rules, strong_share=rules.get("strong_share", .75) + step,
                         moderate_share=rules.get("moderate_share", .6) + step)
            now = _verdicts(_cells(finds, focus), moved)
            moves.append((r, step, [(v, base[v][0], now[v][0]) for v in base if now[v][0] != base[v][0]]))
    text = []
    for r, step, changes in moves:
        cut = "5 points lower" if step < 0 else "5 points higher"
        text.append(f"{_label(r)}, with both share thresholds {cut}: "
                    + ("; ".join(f"{_label(v)} {a} becomes {b}" for v, a, b in changes) if changes else "no change"))
    lines += ["", "Verdicts of Table 1 at neighbouring thresholds (the shares needed for strong and for moderate moved "
              "together): " + "; ".join(text) + "."]
    return "\n".join(lines)


def _verdicts(cells: dict, rules: dict) -> dict[str, tuple[str, int, int, str]]:
    """Per context: (verdict, studies, studies in the most common direction, that direction)."""
    out = {}
    for v, ds in cells.items():
        top, k = Counter(d for d, _ in ds).most_common(1)[0]
        out[v] = (verdict(len(ds), k / len(ds), sum(s for _, s in ds) / len(ds), rules, top), len(ds), k, top)
    return out


def studies_list(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """The focused studies, rule by rule: why each is in, its design, context and direction, whether it was read in
    full."""
    from . import tables
    chosen, feats = selection(store, run_id, protocol)
    works = tables.works(store, {w for by_study in chosen.values() for w in by_study}, run_id)
    full = {r["work_id"] for r in store.db.execute("SELECT work_id FROM fulltexts WHERE status = 'parsed'")}
    focus = protocol.get("focus") or {}
    design = focus.get("design_field", "evidence_type")
    lines = [f"**{label}.** The studies of the focused evidence, by rule: why each is included (a causal design; "
             "longitudinal data with an evolutionary framing; or an explicit evolutionary test in a subsistence or "
             "historical population); the evidence type, context and direction of its findings on the rule, as "
             "verified and counted in Table 1 (one direction per context, mixed if its findings go both ways); the "
             "context charted for the study as a whole, which was not verified and can differ (a study's findings may "
             "concern another population than its main one, or the charting erred); and whether it was read in full "
             "(else from its abstract).", "",
             "| Rule | Study | Year | Why included | Evidence type (counted) | Context: direction (counted) | "
             "Context as charted | Full text |",
             "|---|---|---:|---|---|---|---|---|"]
    for r, by_study in chosen.items():
        for w in sorted(by_study, key=lambda x: (by_study[x][0], -(works[x].get("year") or 0), x)):
            why, fs = by_study[w]
            types = sorted({t for f in fs for t in split_values(f["fields"].get(design, ""))})
            counted = "; ".join(f"{_label(v)}: {_label(d)}" for v, ds in sorted(_cells(fs, focus).items())
                                for d, _ in ds)
            lines.append(f"| {_label(r)} | {_cite(works, w)} | {works[w].get('year') or ''} | {REASONS[why]} | "
                         f"{', '.join(_label(t) for t in types)} | {counted} | "
                         f"{', '.join(_label(x) for x in feats.get(w, {}).get('development_context', []))} | "
                         f"{'yes' if w in full else 'abstract'} |")
    return "\n".join(lines)


def _statements(rule: str, findings: list[dict], focus: dict, heading: bool = True) -> list[str]:
    """Per context: the studies' directions, the most common one, its share, the share with a strong design, and the
    verdict; then the mismatch test between ancestral and modern contexts."""
    rules, test = focus.get("strength") or {}, focus.get("mismatch") or {}
    cells = _cells(findings, focus)
    order = list(test.get("ancestral") or []) + list(test.get("modern") or [])
    out = [f"### {_label(rule).capitalize()}" if heading else f"*{_label(rule).capitalize()}*", "",
           "| Context | Studies | ↓ | ↑ | = | ~ | Most common | Share | Causal or longitudinal | Evidence |",
           "|---|---:|---:|---:|---:|---:|---|---:|---:|---|"]
    lines_after = []
    for v in sorted(cells, key=lambda c: (order.index(c) if c in order else len(order), c)):
        ds = cells[v]
        n, c = len(ds), Counter(d for d, _ in ds)
        top, k = c.most_common(1)[0]
        design_share = sum(s for _, s in ds) / n
        rating = verdict(n, k / n, design_share, rules, top)
        out.append(f"| {_label(v)} | {n} | " + " | ".join(str(c.get(d, 0)) for d, _ in DIRECTIONS)
                   + f" | {_label(top)} | {k / n:.0%} | {design_share:.0%} | **{rating}** |")
        if rating in ("strong", "moderate") and top in ("raises", "lowers"):
            lines_after.append(f"- **{rating.capitalize()}**: in {_label(v)} populations, more of the cue "
                               f"{'raises' if top == 'raises' else 'lowers'} fertility ({k} of {n} studies; "
                               f"{design_share:.0%} with a causal or longitudinal design).")
        elif rating in ("strong", "moderate"):
            lines_after.append(f"- **{rating.capitalize()}**: in {_label(v)} populations the studies most often find "
                               f"{_label(top)} ({k} of {n}).")
    out.append("")
    if test.get("ancestral") and test.get("modern"):
        a, b, c, d, anc, mod = _tally(cells, test)
        if a + b and c + d:
            p, odds = fisher_exact(a, b, c, d), odds_ratio(a, b, c, d)
            lo, hi = odds_ci(a, b, c, d)
            differs = p < test.get("alpha", 0.05)
            out.append(f"**Ancestral against modern contexts:** more of the cue raises fertility in {a} of {a + b} "
                       f"studies with a direction in ancestral contexts ({a / (a + b):.0%}) and in {c} of {c + d} in "
                       f"modern ones ({c / (c + d):.0%}); Fisher's exact p = {p:.3g}, odds ratio {odds:.1f} "
                       f"(95% CI {lo:.1f} to {hi:.1f}). "
                       + ("The share finding a rise differs between the two." if differs else
                          "No difference in that share detectable at this size.")
                       + f" (No relation or mixed: {anc['no_relation'] + anc['mixed']} ancestral, "
                       f"{mod['no_relation'] + mod['mixed']} modern.)")
            out.append("")
    return out + lines_after + ([""] if lines_after else [])


def build(store: Store, run_id: int, protocol: dict, alongside: str = "social_construct",
          focus: dict | None = None) -> str:
    """The whole map, or with `focus` (the protocol's [focus]) a view of its rules and their precise studies."""
    from . import tables
    spec = protocol.get("findings") or {}
    fields = {f["name"]: f for f in spec.get("fields") or []}
    axes = [a for a in spec.get("matrix") or [] if a in fields][:2]
    if len(axes) < 2:
        raise ValueError("the protocol's [findings] matrix needs two categorical fields")
    rule_ax, out_ax = axes
    input_ax = next((f for f in fields if f not in axes and f not in ("direction", "evidence_type", "context")
                     and fields[f].get("categories")), None)
    kept = set(kept_ids(store, run_id))
    finds = [f for f in _findings(store, run_id) if f["work_id"] in kept]
    feats = {w: d for w, d in _features(store, run_id).items() if w in kept}
    lead = same_study(store, run_id, {f["work_id"] for f in finds} | set(feats),
                      Counter(f["work_id"] for f in finds))
    merged = sum(1 for w, l in lead.items() if w != l)
    finds = [f for f in finds if lead.get(f["work_id"], f["work_id"]) == f["work_id"]]
    feats = {w: d for w, d in feats.items() if lead.get(w, w) == w}
    works = tables.works(store, {f["work_id"] for f in finds} | set(feats), run_id)
    full = {r["work_id"] for r in store.db.execute("SELECT work_id FROM fulltexts WHERE status = 'parsed'")}
    rules = [c for c in fields[rule_ax]["categories"] if c != "none"]
    outcomes = [c for c in fields[out_ax]["categories"] if c != "other"]
    # the other categorical fields of a finding (e.g. context, evidence_type): each gets a table of studies
    strata = [f for f in fields if f not in (rule_ax, out_ax, input_ax, "direction") and fields[f].get("categories")]
    by_rule = defaultdict(list)
    for f in finds:
        for r in split_values(f["fields"].get(rule_ax, "")):
            by_rule[r].append(f)
    charted = defaultdict(set)
    for w, d in feats.items():
        for r in d.get(rule_ax, []):
            charted[r].add(w)
    why: dict[str, dict[int, str]] = {}
    if focus:
        rules = [r for r in focus.get("rules") or rules if r in rules]
        for r in rules:
            chosen = focus_studies(by_rule.get(r, []), feats, focus, out_ax)
            why[r] = {w: reason for w, (reason, _) in chosen.items()}
            by_rule[r] = [f for _, fs in chosen.values() for f in fs]
            charted[r] = set(chosen)
        kept_studies = {w for r in rules for w in why[r]}
        finds = [f for r in rules for f in by_rule[r]]

    if focus:
        title = (f"# Focused evidence map: {protocol.get('title', '')}", "",
                 f"{len(kept_studies)} studies and {len(finds)} findings on {len(rules)} rules: "
                 + ", ".join(f"{_label(r)} ({len(why[r])})" for r in rules) + ". A view of the full map: a study is "
                 "here for a rule when one of its findings on that rule has a fertility outcome and a direction, and "
                 "it has (a) a causal design (natural experiment or experiment), or (b) longitudinal data with an "
                 "evolutionary framing, or (c) an explicit evolutionary test in a subsistence or historical "
                 "population. Everything else stays in the full map (evidence_map.html).")
    else:
        title = (f"# Evidence map: {protocol.get('title', '')}", "",
                 f"{len(finds)} findings from {len({f['work_id'] for f in finds})} studies; {len(feats)} studies "
                 "charted.")
    lines = [*title[:-1],
             title[-1] + " Tables only: counts are counts of findings (↓ lowers, ↑ raises, = no relation, ~ mixed), "
             "nothing is weighted or pooled, and every row names its study."
             + (f" {merged} second records of a study (same abstract, title or OpenAlex record) are merged into "
                "their study and not counted again." if merged else ""), "",
             *([f"**Direction**, as coded: {(fields.get('direction') or {}).get('question')}", ""]
               if (fields.get("direction") or {}).get("question") else []),
             "## Overview", "",
             "| " + _label(rule_ax) + " | Studies charted | Studies with findings | Findings | ↓ | ↑ | = | ~ |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in rules:
        fs = by_rule.get(r, [])
        dirs = Counter(f["fields"].get("direction") for f in fs)
        lines.append(f"| [{_label(r)}](#{r.replace('_', '-')}) | {len(charted.get(r, ()))} | "
                     f"{len({f['work_id'] for f in fs})} | {len(fs)} | "
                     + " | ".join(str(dirs.get(d, 0)) for d, _ in DIRECTIONS) + " |")
    lines.append("")
    if focus and (focus.get("strength") or focus.get("mismatch")):
        lines += ["## Evidence statements", "",
                  "Per rule and context, each study counted once with its direction there. The verdicts follow the "
                  "rules fixed in the protocol's [focus.strength] before the verified codes were counted: strong = at "
                  "least 8 studies, at least 75% in one direction, at least half with a causal or longitudinal design; "
                  "moderate = at least 5 studies, at least 60% in one direction; inconsistent = no direction reaches "
                  "60%; limited = fewer than 5 studies. The test compares ancestral contexts (subsistence, historical) "
                  "with modern ones (transitioning, high-income).", ""]
        for r in rules:
            lines += _statements(r, by_rule.get(r, []), focus)
    lines += _reliability(store, run_id, list(fields))

    for r in rules:
        fs = by_rule.get(r, [])
        studies = charted.get(r, set()) | {f["work_id"] for f in fs}
        lines += [f'<a id="{r.replace("_", "-")}"></a>', "", f"## {_label(r).capitalize()}", ""]
        if not studies:
            lines += ["No study was charted under this rule.", ""]
            continue
        for dim in STUDY_DIMS[:3]:
            c = Counter(v for w in studies for v in feats.get(w, {}).get(dim, []))
            if c:
                lines.append(f"**{_label(dim).capitalize()}:** " + ", ".join(f"{_label(k)} {n}" for k, n in c.most_common()))
                lines.append("")
        wanted = set((focus or {}).get("constructs") or [])
        c = Counter(v for w in studies for v in feats.get(w, {}).get(alongside, [])
                    if v != "none_described" and (not wanted or v in wanted))
        if c:
            lines += [f"**{_label(alongside).capitalize()} measured alongside:** "
                      + ", ".join(f"{_label(k)} {n}" for k, n in c.most_common()), ""]
            if wanted:
                for k in sorted(c):
                    ws = sorted(w for w in studies if k in feats.get(w, {}).get(alongside, []))
                    lines += [f"- {_label(k)}: " + "; ".join(_cite(works, w) for w in ws)]
                lines.append("")
        main = [f for f in fs if f["fields"].get(out_ax) != "other"]
        if main and input_ax:
            inputs = sorted({f["fields"].get(input_ax, "") for f in main})
            lines += [f"**Findings by {_label(input_ax)} and {_label(out_ax)}**", "",
                      "| " + _label(input_ax) + " | " + " | ".join(_label(o) for o in outcomes) + " |",
                      "|---|" + "---|" * len(outcomes)]
            for i in inputs:
                cells = []
                for o in outcomes:
                    d = Counter(f["fields"].get("direction") for f in main
                                if f["fields"].get(input_ax) == i and f["fields"].get(out_ax) == o)
                    cells.append(" ".join(f"{d[k]}{s}" for k, s in DIRECTIONS if d.get(k)) or "")
                lines.append(f"| {_label(i)} | " + " | ".join(cells) + " |")
            lines.append("")
            for stratum in strata:
                lines += _by_study(main, stratum)
        other = [f for f in fs if f["fields"].get(out_ax) == "other"]
        for title, rows in (("Findings", main), (f"Findings with {_label(out_ax)} other", other)):
            if not rows:
                continue
            lines += [f"**{title}** ({len(rows)})", "",
                      "| Study | " + " | ".join(_label(x) for x in fields if x != rule_ax) + " | For whom | Finding |",
                      "|---|" + "---|" * (len(fields) - 1) + "---|---|"]
            for f in rows:
                lines.append(f"| {_cite(works, f['work_id'])} | "
                             + " | ".join(_label(f["fields"].get(x, "")) for x in fields if x != rule_ax)
                             + f" | {_cell(f.get('for_whom'))} | {_cell(f.get('finding'))} |")
            lines.append("")
        reasons = why.get(r, {})
        lines += [f"**Studies** ({len(studies)})", "",
                  "| Study | Year | Country | Design | Context | " + ("Why here | Read in full | " if focus else "")
                  + "Link |", "|---|---:|---|---|---|" + ("---|---|" if focus else "") + "---|"]
        order = (lambda x: (reasons.get(x, "z"), -(works.get(x, {}).get("year") or 0), x)) if focus else \
            (lambda x: (works.get(x, {}).get("cite") or "", x))
        for w in sorted(studies, key=order):
            m, d = works.get(w, {}), feats.get(w, {})
            link = f"[DOI](https://doi.org/{m['doi']})" if m.get("doi") else ""
            extra = (f"{REASONS.get(reasons.get(w), '')} | {'yes' if w in full else ''} | " if focus else "")
            lines.append(f"| {_cite(works, w)} | {m.get('year') or ''} | {', '.join(d.get('country', []))} | "
                         f"{', '.join(_label(x) for x in d.get('design', []))} | "
                         f"{', '.join(_label(x) for x in d.get('development_context', []))} | {extra}{link} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def _reliability(store: Store, run_id: int, names: list[str]) -> list[str]:
    """How far a blind second coder agrees with the findings' codes, per field (recode --second-coder)."""
    from . import recode
    agree = recode.field_agreement(store, run_id, names)
    if not agree:
        return []
    out = ["## Reliability", "",
           "A second coder, blind to the first coder's codes, coded a sample of the findings again from their statement "
           "and passage. Agreement per field (Cohen's κ; the first value where a code lists several):", "",
           "| Field | Findings | Agreement | κ |", "|---|---:|---:|---:|"]
    for name, a in agree.items():
        out.append(f"| {_label(name)} | {a['n']} | {a['agree'] / a['n']:.0%} | "
                   f"{'–' if a['kappa'] is None else a['kappa']} |")
    return out + [""]


def study_direction(findings: list[dict]) -> str | None:
    """One direction for a study's findings on a rule: mixed if they go both ways (or one is mixed), no_relation
    if none found a relation, else the direction they found."""
    ds = {f["fields"].get("direction") for f in findings} & {d for d, _ in DIRECTIONS}
    if not ds:
        return None
    if "mixed" in ds or {"lowers", "raises"} <= ds:
        return "mixed"
    return "lowers" if "lowers" in ds else "raises" if "raises" in ds else "no_relation"


def _by_study(findings: list[dict], stratum: str) -> list[str]:
    """Studies (not findings) per value of `stratum`, each counted once with its direction there."""
    groups: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for f in findings:
        for v in split_values(f["fields"].get(stratum, "")) or ["not stated"]:
            groups[(f["work_id"], v)].append(f)
    table: dict[str, Counter] = defaultdict(Counter)
    for (w, v), fs in groups.items():
        d = study_direction(fs)
        if d:
            table[v][d] += 1
    if not table:
        return []
    out = [f"**Studies by {_label(stratum)}** (each study once per {_label(stratum)}: mixed if its findings go "
           "both ways)", "", f"| {_label(stratum)} | Studies | ↓ | ↑ | = | ~ |", "|---|---:|---:|---:|---:|---:|"]
    for v, c in sorted(table.items(), key=lambda kv: -sum(kv[1].values())):
        out.append(f"| {_label(v)} | {sum(c.values())} | " + " | ".join(str(c.get(d, 0)) for d, _ in DIRECTIONS) + " |")
    return out + [""]


def _cite(works: dict, w: int) -> str:
    m = works.get(w) or {}
    return f"{m.get('cite') or 'record'} (#{w})"


def _cell(text: str | None, n: int = 220) -> str:
    text = " ".join((text or "").split()).replace("|", "/")
    return text if len(text) <= n else text[: n - 1] + "…"


def write(store: Store, run_id: int, protocol: dict, root: Path = ROOT, focus: dict | None = None,
          name: str | None = None) -> tuple[Path, Path]:
    """The map as Markdown and HTML; `name` sets the file name (an aim's own focus: evidence_map_focus_<aim>)."""
    from . import document
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    text = build(store, run_id, protocol, focus=focus)
    out = root / "output" / f"corpus_{corpus}" / f"run_{run_id:03d}" / (
        f"{name}.md" if name else "evidence_map_focus.md" if focus else "evidence_map.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    page = out.with_suffix(".html")
    page.write_text(document.to_html(text, "Focused evidence map" if focus else "Evidence map"), encoding="utf-8")
    return out, page
