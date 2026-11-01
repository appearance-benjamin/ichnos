"""Aim-focused papers: a finished paper that answers one preselected aim, not a map of every included study.

A protocol's `[aim]` names the aim (`kind`, e.g. "solutions", and its `question`), the findings field the paper is
organised by (`by`, e.g. design_choice), the fields that say whether a finding bears on the aim (`link`: tested,
associated, reported or none; `domain`: the aim's outcomes or neither), and optionally a `comparator` corpus whose
comparative findings are summarised beside it. With an aim:

- the writer gets only the findings that bear on it (the aim set), and writes a short Method itself from the method
  facts; the generated full Method and the scoping tables go to a separate supplement;
- the main text carries three generated tables: the solutions table (the aim set by `by` and outcome), the comparator
  table (the comparator corpus's comparative findings), and the patterns table (the pre-registered patterns and the
  comparator predictions, as `patterns` counts them);
- method facts A1-A4 state how the aim set was formed, where the comparator evidence comes from, how the patterns
  were tested and what no person has checked.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict

from . import tables
from .store import Store

LINKED = ("tested", "associated", "reported")
DOMAINS = ("learning", "wellbeing", "both")
COMPARATIVE = ("randomised", "before_after", "concurrent", "within_person")
LOWERED = ("lowered", "low_vs_high_conditions")


def spec(protocol: dict) -> dict | None:
    return protocol.get("aim") or None


def with_aim(protocol: dict, aim_id: str | None) -> dict:
    """The protocol with the named aim as its [aim]: `[aim]` is the default, `[[aims]]` hold the others (each with an
    `id`); a manuscript keeps the id of the aim it was written for. A focus aim may bring its own `focus` table (rules,
    contexts, strength rules, test), which then replaces the protocol's [focus], and its own `paper` settings
    (target, abstract), which override the protocol's [paper]."""
    if not aim_id:
        return protocol
    for a in [protocol.get("aim") or {}] + list(protocol.get("aims") or []):
        if a.get("id") == aim_id:
            return dict(protocol, aim=a, **({"focus": a["focus"]} if a.get("focus") else {}),
                        **({"paper": {**(protocol.get("paper") or {}), **a["paper"]}} if a.get("paper") else {}))
    raise ValueError(f"no aim with id {aim_id!r} in the protocol ([aim] or [[aims]])")


def _main(value, order=None) -> str:
    """One value of a field coded with several ('tested; reported'): the first in `order` (the strongest link), else
    the first given."""
    vals = [v.strip() for v in str(value or "").split(";") if v.strip()]
    if order:
        for o in order:
            if o in vals:
                return o
    return vals[0] if vals else ""


def _fields(r) -> dict:
    return _normalise(json.loads(r["fields"] or "{}"))


def _normalise(f: dict) -> dict:
    f = dict(f)
    for k, order in (("aim_link", LINKED), ("outcome_domain", ("both", "learning", "wellbeing")),
                     ("design_choice", None), ("direction", None), ("evidence_type", None), ("outcome", None)):
        if k in f:
            f[k] = _main(f[k], order)
    # Workload is not well-being (round-1 decision, R2): a finding whose outcome is workload counts for learning
    # only if it was also coded for learning, and otherwise not at all.
    if f.get("outcome") == "workload_or_cost" and f.get("outcome_domain") in ("wellbeing", "both"):
        f["outcome_domain"] = "learning" if f["outcome_domain"] == "both" else "neither"
    return f


def _categories(protocol: dict, name: str) -> list[str]:
    for f in (protocol.get("findings") or {}).get("fields") or []:
        if f["name"] == name:
            return list(f.get("categories") or [])
    return []


def aim_rows(store: Store, run_id: int, protocol: dict) -> list:
    """The findings the paper rests on. For a 'solutions' aim: those that link a design choice to the aim's outcomes
    (linked_rows); for an 'effectiveness' aim (the review's own question): every finding of the paper's studies."""
    a = spec(protocol)
    if not a:
        return []
    if a.get("kind") == "focus":
        # a hypothesis-testing paper on the protocol's [focus] view: its verified findings only (rulemap.selection)
        from .rulemap import focus_finding_ids
        keep = focus_finding_ids(store, run_id, protocol)
        return [r for r in tables.finding_rows(store, run_id) if r["finding_id"] in keep]
    if a.get("kind") in ("effectiveness", "proposal"):
        only = tables.paper_set(store, run_id, protocol)
        return [r for r in tables.finding_rows(store, run_id) if only is None or r["work_id"] in only]
    return linked_rows(store, run_id, protocol)


def linked_rows(store: Store, run_id: int, protocol: dict) -> list:
    """The findings that link a design choice (`by`) to the aim's outcomes (`link`, `domain`), in the paper's studies."""
    a = spec(protocol)
    if not a:
        return []
    only = tables.paper_set(store, run_id, protocol)
    out = []
    for r in tables.finding_rows(store, run_id):
        f = _fields(r)
        if only is not None and r["work_id"] not in only:
            continue
        if f.get(a["link"]) in LINKED and f.get(a["domain"]) in DOMAINS and f.get(a["by"]) not in (None, "", "none"):
            out.append(r)
    return out


def _label(name: str) -> str:
    return name.replace("_", " ")


# The three tables in the main text

def outcomes_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """The review's answer by outcome: studies and programmes, then findings by kind of evidence and direction."""
    rows = aim_rows(store, run_id, protocol)
    if not rows:
        return ""
    ov = tables.overlaps(store, run_id, protocol)
    by: dict[str, list] = defaultdict(list)
    for r in rows:
        by[_fields(r).get("outcome") or "other"].append(r)
    order = [o for o in _categories(protocol, "outcome") if o in by]
    kinds = (("comparative", "pre_post"), ("association",), ("perception",), ("descriptive",))

    def cell(rs, ks):
        hit = [r for r in rs if _fields(r).get("evidence_type") in ks]
        if not hit:
            return "–"
        c = Counter(_fields(r).get("direction") or "not_applicable" for r in hit)
        names = (("favourable", "fav."), ("unfavourable", "unfav."), ("mixed", "mixed"), ("no_difference", "no diff."),
                 ("not_applicable", "n/a"))
        return ", ".join(f"{c[k]} {w}" for k, w in names if c[k])
    works = {r["work_id"] for r in rows}
    chart = tables.charting(store, run_id)
    meta = tables.works(store, works, run_id)
    described = {w for w in works if "described_as_programmatic_assessment" in
                 ((chart.get(w, {}).get("pa_status") or {}).get("values") or [])}
    full = {w for w in works if tables._text_source(store, meta, w) == "full text"}
    split = full != set(works)                 # a 'Full text' column only says something when some were not read in full

    def comp_share(rs):
        """Comparative or before-after findings: favourable / all, as a sensitivity check."""
        hit = [r for r in rs if _fields(r).get("evidence_type") in ("comparative", "pre_post")]
        return f"{sum(1 for r in hit if _fields(r).get('direction') == 'favourable')}/{len(hit)}" if hit else "–"
    lines = [f"**{label}.** What the {len(rows)} findings of the {len(works)} core studies "
             f"({len({ov['root'].get(w, w) for w in works})} when reports that may share a sample count once) say, by "
             "outcome: findings by kind of evidence and direction. Comparative = programmatic assessment compared with "
             "other assessment or with the time before it (with data on both sides); association = data within one "
             "programme; perception = how learners or teachers experienced it; descriptive = how the programme works. "
             "Direction is for learners or the programme, as defined in the protocol (for perceived stakes, favourable "
             "means experienced as low-stakes, as designed). Codes were assigned by language-model agents and not "
             "checked by a person; counts are findings, not studies, except the Studies columns. Sensitivity: 'Called "
             f"programmatic assessment' counts the studies whose programme the study itself calls programmatic assessment ({len(described)} of "
             f"{len(works)}; the rest are built on its principles under another name, e.g. competency-based or "
             "entrustment-based programmes)"
             + (f", 'Full text' the studies read in full ({len(full)} of {len(works)})" if split else
                f" (every study was read in full)") + "; for each, the favourable share of its comparative or before-after "
             "findings.", "",
             "| Outcome | Studies | Findings | Comparative or before-after | Association | Perception | Descriptive | "
             "Called programmatic assessment: studies, comparative fav./all |"
             + (" Full text: studies, comparative fav./all |" if split else ""),
             "| --- | ---: | ---: | --- | --- | --- | --- | --- |" + (" --- |" if split else "")]
    for o in order:
        rs = by[o]
        rd = [r for r in rs if r["work_id"] in described]
        rf = [r for r in rs if r["work_id"] in full]
        lines.append(f"| {_label(o)} | {len({r['work_id'] for r in rs})} | {len(rs)} | "
                     + " | ".join(cell(rs, ks) for ks in kinds)
                     + f" | {len({r['work_id'] for r in rd})}, {comp_share(rd)} |"
                     + (f" {len({r['work_id'] for r in rf})}, {comp_share(rf)} |" if split else ""))
    # perceived stakes, as charted per study (round-1 decision on the general paper, R1)
    ps = {w: set((chart.get(w, {}).get("perceived_stakes") or {}).get("values") or []) - {"not_reported"}
          for w in works}
    ps = {w: v for w, v in ps.items() if v}
    if ps:
        learners_hi = {w for w, v in ps.items() if "learners_higher" in v}
        assessors_hi = {w for w, v in ps.items() if "assessors_higher" in v}
        higher = learners_hi | assessors_hi
        only_moved = {w for w, v in ps.items() if v == {"concern_moved_to_decision"}}
        reporting = set(ps) - only_moved
        lines += ["", f"Perceived stakes, as charted for these studies: {len(reporting)} studies report how learners or "
                  "teachers perceived the stakes of data points designed as low-stakes; "
                  f"{len(higher & reporting)} report them experienced as higher-stakes than designed (learners in "
                  f"{len(learners_hi)}, teachers or assessors in {len(assessors_hi)}; "
                  f"{len({ov['root'].get(w, w) for w in higher & reporting})} when reports that may share a sample count "
                  f"once) and {len(reporting - higher)} report none higher; {len(only_moved)} further report only that "
                  "concern moved to the decision. "
                  + (f"Among the studies read in full text, {len(higher & reporting & full)} of {len(reporting & full)} "
                     "report data points experienced as higher-stakes than designed; among" if split else "Among")
                  + " the studies that call their programme programmatic assessment, "
                  f"{len(higher & reporting & described)} of {len(reporting & described)}"
                  + ("" if split else " report them experienced as higher-stakes than designed") + "."]
    return "\n".join(lines)


def solutions_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    a = spec(protocol)
    rows = linked_rows(store, run_id, protocol)
    if not rows:
        return ""
    ov = tables.overlaps(store, run_id, protocol)
    by_choice: dict[str, list] = defaultdict(list)
    for r in rows:
        by_choice[_fields(r)[a["by"]]].append(r)

    def cell(rs, domain):
        hit = [r for r in rs if _fields(r)[a["domain"]] in (domain, "both")]
        if not hit:
            return "–"
        c = Counter(_fields(r).get("direction") or "not_applicable" for r in hit)
        names = (("favourable", "fav."), ("unfavourable", "unfav."), ("mixed", "mixed"), ("no_difference", "no diff."),
                 ("not_applicable", "n/a"))
        parts = [f"{c[k]} {w}" for k, w in names if c[k]]
        parts += [f"{n} {_label(k)}" for k, n in c.items() if k not in dict(names)]
        return ", ".join(parts)

    def strength(rs):
        c = Counter(_fields(r)[a["link"]] for r in rs)
        return ", ".join(f"{c[k]} {k}" for k in LINKED if c[k])
    order = sorted(by_choice, key=lambda k: (-len({r["work_id"] for r in by_choice[k]}), k))
    known = [k for k in _categories(protocol, a["by"]) if k in by_choice]
    order = sorted(known, key=lambda k: (-len({r["work_id"] for r in by_choice[k]}), known.index(k))) or order
    works = {r["work_id"] for r in rows}
    chart = tables.charting(store, run_id)
    described = {w for w in works if "described_as_programmatic_assessment" in
                 ((chart.get(w, {}).get("pa_status") or {}).get("values") or [])}
    meta = tables.works(store, works, run_id)
    full = {w for w in works if tables._text_source(store, meta, w) == "full text"}
    split = full != works
    agree = coder_agreement(store, run_id, protocol)
    agree_text = ("; a second agent coded every finding again blind, and agreement (Cohen's kappa) was "
                  + ", ".join(f"{_label(k)} {v['kappa']:.2f} ({v['agree']} of {v['n']} identical)"
                              for k, v in agree.items() if v.get("kappa") is not None) if agree else
                  "; no second coder has coded these fields")
    lines = [f"**{label}.** Design choices and what the findings say about learners' learning and well-being. The "
             f"{len(rows)} findings from {len(works)} studies "
             f"({len({ov['root'].get(w, w) for w in works})} when reports that may share a sample count once) that "
             "connect a design "
             "choice to either outcome, by the design choice they concern. Learning and well-being cells count findings "
             "by direction (favourable, unfavourable, mixed, no difference); a finding on both outcomes counts in both. "
             "Evidence: tested = a comparison with other assessment, with the time before, or between variants; "
             "associated = data within one programme; reported = what learners or teachers attribute to the choice. "
             "Workload findings are not counted as well-being. 'Called programmatic assessment' counts the studies whose programme the "
             "study itself calls programmatic assessment (the rest are built on its principles under another name, for "
             f"example competency-based or entrustment-based programmes); {len(described)} of the {len(works)} studies "
             "are. "
             + (f"'Full text' counts the studies the agents read in full ({len(full)} of {len(works)}; the others from "
                "the abstract or title only). " if split else "Every study was read in full. ")
             + "Codes were assigned by language-model agents and not checked by a person"
             + agree_text + "; counts are not vote counts of effects.", "",
             "| Design choice | Studies | Called programmatic assessment |" + (" Full text |" if split else "")
             + " Findings | Learning | Well-being | Evidence |",
             "| --- | ---: | ---: |" + (" ---: |" if split else "") + " ---: | --- | --- | --- |"]
    for k in order:
        rs = by_choice[k]
        ws = {r["work_id"] for r in rs}
        lines.append(f"| {_label(k)} | {len(ws)} | {len(ws & described)} |" + (f" {len(ws & full)} |" if split else "")
                     + f" {len(rs)} | {cell(rs, 'learning')} | {cell(rs, 'wellbeing')} | {strength(rs)} |")
    return "\n".join(lines)


def studies_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """The studies the paper rests on: design, setting, country, what was read, the design choices they inform and
    the kind of evidence."""
    a = spec(protocol)
    rows = aim_rows(store, run_id, protocol)
    if not rows:
        return ""
    works = sorted({r["work_id"] for r in rows})
    meta = tables.works(store, works, run_id)
    chart = tables.charting(store, run_id)

    def vals(w, d):
        return ", ".join(_label(v) for v in (chart.get(w, {}).get(d) or {}).get("values") or []) or "not reported"

    def country(w):
        c = chart.get(w, {}).get("country") or {}
        return (c.get("text") or "not reported").replace("|", "/")[:40]
    by = defaultdict(list)
    for r in rows:
        by[r["work_id"]].append(_fields(r))
    lines = [f"**{label}.** The {len(works)} studies this paper's evidence comes from: design and setting as charted, "
             "the programme's status (called programmatic assessment by the study, or built on its principles), what "
             "the agents read (full text, abstract or title only), and the design choices and kinds of evidence of their "
             "findings in Table 1.", "",
             "| Study | Design | Setting | Country | Programme | Read from | Design choices | Evidence |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for w in sorted(works, key=lambda w: meta[w]["cite"]):
        fs = by[w]
        choices = ", ".join(sorted({_label(f[a["by"]]) for f in fs}))
        kinds = ", ".join(f"{sum(1 for f in fs if f[a['link']] == k)} {k}" for k in LINKED
                          if any(f[a["link"]] == k for f in fs))
        lines.append(f"| {meta[w]['cite']} | {vals(w, 'design')} | {vals(w, 'setting')} | {country(w)} | "
                     f"{vals(w, 'pa_status')} | {tables._text_source(store, meta, w)} | {choices} | {kinds} |")
    return "\n".join(lines)


def coder_agreement(store: Store, run_id: int, protocol: dict) -> dict:
    """The blind second coder's agreement on the aim's fields (empty when there is none): both coders' codes go
    through the same rules (one value per field, workload not counted as well-being) before they are compared."""
    from . import recode
    from .agreement import cohen_kappa
    recode.ensure(store)
    a = spec(protocol) or {}
    names = [n for n in (a.get("by"), a.get("link"), a.get("domain")) if n]
    only = tables.paper_set(store, run_id, protocol)          # the paper's studies: its codes, not the run's
    raw = {r["finding_id"]: json.loads(r["fields"] or "{}") for r in tables.finding_rows(store, run_id)
           if only is None or r["work_id"] in only}
    second: dict[int, dict] = defaultdict(dict)
    for r in store.db.execute("SELECT finding_id, field, value FROM finding_second_codings"):
        if r["finding_id"] in raw and r["field"] in names:
            second[r["finding_id"]][r["field"]] = r["value"]
    out = {}
    for name in names:
        pairs = []
        for fid, codes in second.items():
            if name in codes and name in raw[fid]:
                first = _normalise(raw[fid]).get(name)
                other = _normalise({**{k: v for k, v in raw[fid].items() if k not in names}, **codes}).get(name)
                pairs.append((first, other))
        if pairs:
            k = cohen_kappa(pairs)
            out[name] = {"n": len(pairs), "agree": sum(x == y for x, y in pairs), "kappa": None if k is None else round(k, 2)}
    return out


def comparator_run(store: Store, corpus: str) -> int | None:
    row = store.db.execute(
        "SELECT r.run_id FROM runs r WHERE r.corpus = ? AND EXISTS (SELECT 1 FROM findings f WHERE f.run_id = r.run_id) "
        "ORDER BY r.run_id DESC LIMIT 1", (corpus,)).fetchone()
    return row["run_id"] if row else None


def _comparator(store: Store, protocol: dict):
    from .config import load_protocol
    a = spec(protocol) or {}
    if not a.get("comparator"):
        return None
    run = comparator_run(store, a["comparator"])
    if run is None:
        return None
    _, _, other = load_protocol(a["comparator"])
    chart = tables.charting(store, run)
    rows = []
    named = [o for o in _categories(other, "outcome") if o != "other"]   # 'other' is a residual: it says nothing here
    for r in tables.finding_rows(store, run):
        f = _fields(r)
        stakes = set((chart.get(r["work_id"], {}).get("stakes_change") or {}).get("values") or [])
        if f.get("evidence_type") in COMPARATIVE and stakes & set(LOWERED) and f.get("outcome") in named:
            rows.append(r)
    return run, other, chart, rows


def comparator_table(store: Store, protocol: dict, label: str) -> str:
    got = _comparator(store, protocol)
    if not got:
        return ""
    run, other, chart, rows = got
    if not rows:
        return ""
    by_out: dict[str, list] = defaultdict(list)
    for r in rows:
        by_out[_fields(r).get("outcome") or "other"].append(r)
    order = [o for o in _categories(other, "outcome") if o in by_out]
    root = tables.overlaps(store, run, other)["root"]
    lowered: dict[int, set] = {}                          # reports that may share a sample count once, as in Q2
    for w, c in chart.items():
        if "lowered" in ((c.get("stakes_change") or {}).get("values") or []):
            lowered.setdefault(root.get(w, w), set()).update(
                set((c.get("displacement") or {}).get("values") or []) - {"not_reported"})
    moved = Counter()
    for vals in lowered.values():
        if vals:
            moved["moved" if vals & {"moved_to_next_assessment", "moved_to_selection"} else "not_moved"] += 1
    works = {r["work_id"] for r in rows}
    lowered_any = {w for w, c in chart.items() if set((c.get("stakes_change") or {}).get("values") or []) & set(LOWERED)}
    lines = [f"**{label}.** Comparator evidence: what lowering the stakes of an assessment (pass/fail instead of grades, "
             f"an assessment that does not count, a licensing examination reported as pass/fail) did to learners in "
             "higher education, from a separate review of assessment stakes run with the same pipeline. "
             f"The {len(rows)} comparative findings (randomised, before-after, concurrent or within-person) from "
             f"{len(works)} studies, by outcome; direction is for the lower-stakes condition. Of the {len(lowered_any)} "
             "retained studies in which stakes were lowered or compared in any design, these are the ones with a "
             "comparative finding on a named outcome; the rest are perception, association or descriptive studies. "
             "Codes were assigned by language-model agents and not checked by a person.", "",
             "| Outcome | Studies | Findings | Lower stakes better | Lower stakes worse | No difference | Mixed | "
             "No direction coded | Randomised or within-person |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for o in order:
        rs = by_out[o]
        c = Counter(_fields(r).get("direction") for r in rs)
        strong = sum(1 for r in rs if _fields(r).get("evidence_type") in ("randomised", "within_person"))
        rest = len(rs) - sum(c[k] for k in ("lower_better", "lower_worse", "no_difference", "mixed"))
        lines.append(f"| {_label(o)} | {len({r['work_id'] for r in rs})} | {len(rs)} | {c['lower_better']} | "
                     f"{c['lower_worse']} | {c['no_difference']} | {c['mixed']} | {rest} | {strong} |")
    strong = [r for r in rows if _fields(r).get("evidence_type") in ("randomised", "within_person")]
    if strong:
        parts = []
        for name, outs, want in (("stress, anxiety, burnout or well-being", ("stress_anxiety_burnout", "wellbeing"),
                                  "lower_better"),
                                 ("performance on the assessment itself", ("performance_same_assessment",), "lower_worse"),
                                 ("effort or preparation", ("effort_preparation",), "lower_worse"),
                                 ("later performance", ("performance_later",), "lower_worse")):
            rs = [r for r in strong if _fields(r).get("outcome") in outs]
            if rs:
                hit = sum(1 for r in rs if _fields(r).get("direction") == want)
                parts.append(f"{name}: lower stakes {'better' if want == 'lower_better' else 'worse'} in {hit} of "
                             f"{len(rs)} findings from {len({r['work_id'] for r in rs})} studies")
        lines += ["", "Randomised or within-person findings alone: " + "; ".join(parts) + "."]
    if sum(moved.values()):
        lines += ["", f"Of the {len(lowered)} studies in which stakes were lowered (reports that may share a sample "
                  f"counted once), {sum(moved.values())} report whether the stakes, effort or stress moved elsewhere: "
                  f"{moved['moved']} report that they moved to the next graded assessment or to selection, "
                  f"{moved['not_moved']} that they did not."]
    return "\n".join(lines)


def _wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """The Wilson score interval of a share k/n."""
    p = k / n
    centre, half = p + z * z / (2 * n), z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return max(0.0, (centre - half) / (1 + z * z / n)), min(1.0, (centre + half) / (1 + z * z / n))


def patterns_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    from . import patterns
    from .config import load_protocol
    items = protocol.get("patterns") or []
    if not items:
        return ""
    first = patterns.count(store, run_id, protocol)
    second = {r["id"]: r for r in patterns.count(store, run_id, protocol, second=True)}
    rows = [(r, second.get(r["id"])) for r in first]
    comp = protocol.get("comparator") or {}
    run = comparator_run(store, comp["corpus"]) if comp.get("corpus") else None
    kappas_q: dict = {}
    if run and comp.get("predictions"):
        _, _, other = load_protocol(comp["corpus"])
        # a prediction's unreliable categories are the comparator review's, with that review's coder agreement
        kappas_q = {f"{a['dimension']}: {a['category']}": a["kappa"] for a in patterns.agreement(store, run, other)
                    if a["category"]}
        c1 = patterns.count(store, run, other, comp["predictions"], protocol)
        c2 = {r["id"]: r for r in patterns.count(store, run, other, comp["predictions"], protocol, second=True)}
        rows += [(r, c2.get(r["id"])) for r in c1]
    names = {p["id"]: p for p in items + list(comp.get("predictions") or [])}

    def val(r):
        t = r["test"]
        v = t["value"]
        if v is None:
            return "–"
        if "n_a" in t or not t.get("n"):
            return f"{v:.2f}"
        lo, hi = _wilson(round(v * t["n"]), t["n"])          # a share: with its 95% interval
        return f"{v:.2f} [{lo:.2f}, {hi:.2f}]"

    def n(r):
        t = r["test"]
        return f"{t['n']}" + (f" ({t['n_a']} vs {t['n_b']})" if "n_a" in t else "")
    def holds(p):
        lo, hi = p.get("holds") or [None, None]
        what = "difference" if p.get("a") or p.get("b") else "share"
        if lo is None:
            return "–"
        if hi == 1:
            return f"{what} ≥ {lo:g}"
        if lo in (0, -1):
            return f"{what} < {hi:g}"
        return f"{lo:g} ≤ {what} < {hi:g}"
    retained = len(patterns.retained_charting(store, run_id))
    kappas = {f"{a['dimension']}: {a['category']}": a["kappa"] for a in patterns.agreement(store, run_id, protocol)
              if a["category"]}
    seen = [p["id"] for p in items if p.get("blind") is False]      # written after the codes they use existed
    ids = f"{items[0]['id']}-{items[-1]['id']}"
    lines = [f"**{label}.** Patterns across studies, written into the protocol before the coding they need "
             + (f"({ids}, except {', '.join(seen)}, written when the codes {'it uses' if len(seen) == 1 else 'they use'} "
                "already existed and so not blind; " if seen else f"({ids}, ")
             + f"counted as registered on all {retained} retained studies of this review, not on the core "
             "alone, and at finding level on their findings) and predictions for the comparator review (Q1-Q4). Each is "
             "judged only on the studies it was not drawn from; 'too few' means fewer than 5 studies, or fewer than 3 "
             "in a group compared. The measure is a share, with its 95% interval (Wilson), or for a comparison the "
             "difference between two shares (first group minus second); with counts this small a verdict near the "
             "threshold is a reading of the data, not a test. 'Second coder' repeats the count on an independent "
             "second coder's codes. 'Unreliable "
             "codes' marks a pattern that rests on a category on which the two coders agreed with a Cohen's kappa below "
             "0.6. The explanation that predicted the pattern is named where one did.", "",
             "| Pattern | Holds when | Counted | Measure | Verdict | Second coder: measure, verdict | Predicted by |",
             "| --- | --- | ---: | ---: | --- | --- | --- |"]
    for r, s in rows:
        p = names.get(r["id"], {})
        by = ", ".join(k for k, v in (p.get("predictions") or {}).items() if v == "holds") or "–"
        ks = kappas_q if str(r["id"]).startswith("Q") else kappas
        flag = (" (unreliable codes: " + ", ".join(
            c.split(": ")[-1].replace("_", " ") + (f", kappa {ks[c]:.2f}" if ks.get(c) is not None else "")
            for c in r["unreliable"]) + ")" if r.get("unreliable") else "")
        lines.append(f"| {r['id']} {r['name']} | {holds(p)} | {n(r)} | {val(r)} | {r['verdict']}{flag} | "
                     f"{(val(s) + ', ' + s['verdict']) if s else '–'} | {by} |")
    lines += ["", "What each pattern states: " + " ".join(
        f"{r['id']}: {' '.join(str(names.get(r['id'], {}).get('statement', '')).split())}" for r, _ in rows)]
    return "\n".join(lines)


def comparator_list(store: Store, protocol: dict, label: str) -> str:
    """The comparator studies the comparator table counts (supplement)."""
    got = _comparator(store, protocol)
    if not got:
        return ""
    run, other, chart, rows = got
    works = sorted({r["work_id"] for r in rows})
    meta = tables.works(store, works, run)
    lines = [f"**{label}.** The {len(works)} studies of the review of assessment stakes whose comparative findings on "
             "lowered stakes are counted in the comparator table.", "",
             "| Study | Title | Comparison | DOI |", "| --- | --- | --- | --- |"]
    for w in sorted(works, key=lambda w: meta[w]["cite"]):
        m = meta[w]
        comp = ", ".join(_label(v) for v in (chart.get(w, {}).get("comparison") or {}).get("values") or []) or "–"
        lines.append(f"| {m['cite']} | {str(m['title']).replace('|', '/')} | {comp} | {m['doi'] or '–'} |")
    return "\n".join(lines)


def _keep_kappa(store: Store, run: int, stage: str) -> tuple[int, float | None]:
    """Keep-or-exclude agreement between the two model screeners at a stage."""
    from .agreement import cohen_kappa
    calls: dict[int, dict[str, str]] = defaultdict(dict)
    for r in store.db.execute("SELECT work_id, screener, decision FROM screening WHERE run_id = ? AND stage = ? AND "
                              "screener LIKE 'model:%'", (run, stage)):
        calls[r["work_id"]][r["screener"]] = "keep" if r["decision"] != "exclude" else "exclude"
    pairs = [tuple(v.values())[:2] for v in calls.values() if len(v) >= 2]
    k = cohen_kappa(pairs) if pairs else None
    return len(pairs), (None if k is None else round(k, 2))


def _comparator_method(store: Store, run: int, other: dict) -> dict:
    """A9: how the comparator review was done, from its own protocol and store."""
    from .agents import kept_ids
    pool = store.db.execute("SELECT count(DISTINCT work_id) FROM retrievals WHERE run_id = ?", (run,)).fetchone()[0]
    kept = kept_ids(store, run)
    n_ta, k_ta = _keep_kappa(store, run, "title_abstract")
    n_el, k_el = _keep_kappa(store, run, "full_text")
    fts = [store.get_fulltext(w) for w in kept]
    full = sum(1 for ft in fts if ft is not None and ft["status"] == "parsed")
    n_find = store.db.execute("SELECT count(*) FROM findings WHERE run_id = ? AND status != 'superseded'",
                              (run,)).fetchone()[0]
    rule = " ".join(str((other.get("eligibility") or {}).get("summary") or "").split())
    rule = rule.replace(" (verbatim rules in the supplement)", "")     # that supplement is the comparator review's own
    started = (store.db.execute("SELECT started_at FROM runs WHERE run_id = ?", (run,)).fetchone()[0] or "")[:10]
    return {"id": "A9", "group": "Aim", "text": (
        f"The comparator review searched titles and abstracts in {len(other.get('sources') or [])} sources on {started} "
        "and by "
        f"citation searching ({pool} unique records). Two language-model agents screened every record independently "
        f"(keep-or-exclude agreement on {n_ta} records: Cohen's kappa {k_ta}) and judged the kept records again at an "
        f"eligibility stage ({n_el} records, kappa {k_el}); a record either agent kept went on. Its eligibility rule: "
        f"{rule} It retained {len(kept)} studies ({full} read in full text), from which an agent charted the kind of "
        f"stakes change, the design and the outcomes and extracted {n_find} findings with their verbatim passages; "
        "comparative findings are randomised, before-after, concurrent or within-person comparisons of stakes "
        "conditions. No person checked its screening, coding or findings.")}


# What the Method must be able to say

def focus_facts(store: Store, run_id: int, protocol: dict) -> list[dict]:
    """Method facts of a [focus] paper: how its studies were chosen, how their codes were verified, the rules and the
    test fixed before counting, the coders' agreement, and what no person checked."""
    from .rulemap import REASONS, selection
    a, focus = spec(protocol), protocol.get("focus") or {}
    chosen, feats = selection(store, run_id, protocol)
    rows = aim_rows(store, run_id, protocol)
    studies = {w for by_study in chosen.values() for w in by_study}
    why = Counter(r for by_study in chosen.values() for r, _ in by_study.values())
    full = {r["work_id"] for r in store.db.execute("SELECT work_id FROM fulltexts WHERE status = 'parsed'")}
    out = [{"id": "A0", "group": "Aim", "text": (
        f"This paper asks: {' '.join(str(a.get('question', '')).split())} It rests on {len(rows)} findings from "
        f"{len(studies)} studies on {len(chosen)} candidate rules ("
        + ", ".join(f"{_label(r)} {len(by_study)}" for r, by_study in chosen.items()) + "), "
        f"{len(studies & full)} of them read in full and the rest from their abstracts.")},
        {"id": "A1", "group": "Aim", "text": (
            "From all charted studies, a study entered the evidence on a rule when one of its findings on that rule "
            "concerned a fertility outcome and stated a direction, and the study had "
            + "; or ".join(f"({k}) {v}" for k, v in REASONS.items())
            + f" (by these: {why['a']} by design, {why['b']} longitudinal with an evolutionary framing, {why['c']} "
            "explicit evolutionary tests in ancestral populations). These criteria were written into the protocol "
            "before the evidence statements were counted; a study recorded twice (the same abstract, title or "
            "OpenAlex record) counts once.")}]
    from . import recode
    recode.ensure(store)                                   # a run never verified has no recode tables yet
    ids = [r["finding_id"] for r in rows]
    if ids:
        marks = ",".join("?" * len(ids))
        changed = store.db.execute(f"SELECT field, count(DISTINCT finding_id) FROM finding_recodes WHERE finding_id IN "
                                   f"({marks}) GROUP BY field", ids).fetchall()
        n_changed = store.db.execute(f"SELECT count(DISTINCT finding_id) FROM finding_recodes WHERE finding_id IN "
                                     f"({marks})", ids).fetchone()[0]
        if changed:
            out.append({"id": "A2", "group": "Aim", "text": (
                "Before any statement was counted, a second language-model agent checked the codes of the findings in "
                f"the focused view against their verbatim passages; {n_changed} of the {len(ids)} findings the paper "
                "rests on were corrected ("
                + ", ".join(f"{_label(f)} {n}" for f, n in sorted(changed, key=lambda x: -x[1])) + "); the corrected "
                "codes are used, and each correction is stored with its reason.")})
    s, m = focus.get("strength") or {}, focus.get("mismatch") or {}
    if s or m:
        out.append({"id": "A3", "group": "Aim", "text": (
            "The strength of each statement and the test were fixed in the protocol before the checked codes were "
            "counted, but after descriptive tables of the unchecked codes had been seen; the protocol was not publicly "
            "registered. Per rule and context, each study counts once with its direction there (mixed if its findings go "
            f"both ways); strong = at least {s.get('strong_min_studies', 8)} studies, at least "
            f"{s.get('strong_share', .75):.0%} in one direction and at least {s.get('strong_design_share', .5):.0%} with "
            f"a causal or longitudinal design; moderate = at least {s.get('moderate_min_studies', 5)} studies and at "
            f"least {s.get('moderate_share', .6):.0%} in one direction; otherwise inconsistent or, below "
            f"{s.get('moderate_min_studies', 5)} studies, limited. Per rule, Fisher's exact test (two-sided, alpha "
            f"{m.get('alpha', 0.05)}) compares the share of studies finding a positive relation among studies with a "
            f"direction in ancestral contexts ({', '.join(_label(c) for c in m.get('ancestral') or [])}) with that in "
            f"modern ones ({', '.join(_label(c) for c in m.get('modern') or [])}).")})
    agree = recode.field_agreement(store, run_id, ["candidate_rule", "outcome", "direction", "context", "evidence_type"])
    if agree:
        n = max(v["n"] for v in agree.values())
        out.append({"id": "A4", "group": "Aim", "text": (
            f"Coder agreement: on a sample of {n} findings stratified by rule, a second language-model agent coded the "
            "fields again without seeing the first codes; Cohen's kappa "
            + "; ".join(f"{_label(k)} {v['kappa']:.2f}" for k, v in agree.items() if v.get("kappa") is not None)
            + ".")})
    out.append({"id": "A5", "group": "Aim", "text": (
        "What no person checked: screening, eligibility, charting, findings and their verification were done by "
        "language-model agents (two independent models at screening and eligibility); every quote was checked in code "
        "against the record's text, but no person has checked the findings or the codes against the studies.")})
    return out


def facts(store: Store, run_id: int, protocol: dict) -> list[dict]:
    """Method facts A1-A4, citable like the generated ones."""
    a = spec(protocol)
    if not a:
        return []
    if a.get("kind") == "focus":
        return focus_facts(store, run_id, protocol)
    if a.get("kind") == "strong":
        from . import strong
        return strong.facts(store, run_id, protocol)
    out = []
    all_rows = tables.finding_rows(store, run_id)
    if a.get("kind") in ("effectiveness", "proposal"):
        rows = aim_rows(store, run_id, protocol)
        ev = Counter(_fields(r).get("evidence_type") for r in rows)
        linked = linked_rows(store, run_id, protocol)
        out.append({"id": "A0", "group": "Aim", "text": (
            f"This paper answers the review's question ({' '.join(str(a.get('question', '')).split())}) from all "
            f"{len(rows)} findings of the {len({r['work_id'] for r in rows})} core studies: {ev['comparative']} "
            f"comparative, {ev['pre_post']} before-after, {ev['association']} association, {ev['perception']} perception "
            f"and {ev['descriptive']} descriptive findings, each with the verbatim passage it rests on. For the design "
            f"choices, the {len(linked)} findings that link a choice to learning or well-being are used.")})
    rows = linked_rows(store, run_id, protocol)
    c = Counter(_fields(r)[a["link"]] for r in rows)
    out.append({"id": "A1", "group": "Aim", "text": (
        f"For the aim ({' '.join(str(a.get('question', '')).split())}), a language-model agent coded each of the "
        f"{len(all_rows)} findings for the design choice it concerns, how it connects that choice to learners' learning "
        f"or well-being (tested, associated, reported or not at all) and which of the two outcomes it concerns, keeping "
        f"each finding's statement and passage. Of these, {len(rows)} findings from "
        f"{len({r['work_id'] for r in rows})} core studies connect a design choice to learning or well-being and are "
        + ("the evidence on design choices" if a.get("kind") == "effectiveness" else "the evidence of this paper")
        + f" ({c['tested']} tested, {c['associated']} associated, {c['reported']} reported).")})
    got = _comparator(store, protocol)
    if got:
        run, other, chart, comp_rows = got
        from .agents import kept_ids
        pool = store.db.execute("SELECT count(DISTINCT work_id) FROM retrievals WHERE run_id = ?", (run,)).fetchone()[0]
        kept = len(kept_ids(store, run))
        out.append({"id": "A2", "group": "Aim", "text": (
            f"The comparator evidence comes from a separate review of assessment stakes in higher education "
            f"({' '.join(str(other.get('title', '')).split())}): titles and abstracts were searched in "
            f"{len(other.get('sources') or [])} sources ({pool} unique records), screened by two language-model agents, "
            f"judged again at an eligibility stage ({kept} records retained), charted and coded with the same pipeline; "
            f"its {len(comp_rows)} comparative findings on lowered stakes come from "
            f"{len({r['work_id'] for r in comp_rows})} studies.")})
    if got:
        out.append(_comparator_method(store, run, other))
    from . import patterns
    agree = [r for r in patterns.agreement(store, run_id, protocol) if r["category"] is None and r["kappa"] is not None]
    if protocol.get("patterns") and agree:
        ks = sorted(r["kappa"] for r in agree)
        out.append({"id": "A3", "group": "Aim", "text": (
            f"For the patterns across studies, {len(agree)} further dimensions were coded for "
            f"{max(r['n'] for r in agree)} retained studies by two language-model agents independently "
            f"(Cohen's kappa per dimension {ks[0]:.2f} to {ks[-1]:.2f}); {len(protocol['patterns'])} patterns and "
            f"{len((protocol.get('comparator') or {}).get('predictions') or [])} comparator predictions were written "
            "into the protocol before that coding and counted by code, each on the studies it was not drawn from.")})
    recodes = store.db.execute(
        "SELECT count(DISTINCT fr.finding_id) FROM finding_recodes fr JOIN findings f ON f.finding_id = fr.finding_id "
        "WHERE f.run_id = ? AND fr.field = ? AND fr.recoder = 'model:claude-opus-5-5'", (run_id, a["link"])).fetchone()[0]
    workload = sum(1 for r in tables.finding_rows(store, run_id)
                   if (json.loads(r["fields"] or "{}").get("outcome") == "workload_or_cost"
                       and _main(json.loads(r["fields"] or "{}").get(a["domain"])) in ("wellbeing", "both")))
    out.append({"id": "A6", "group": "Aim", "text": (
        "After the first editorial decision, the codes of the findings first coded as tested or associated were checked "
        f"again by a second agent against the definitions, which changed the link of {recodes} findings (two "
        "perception findings from tested to reported; within-programme findings that relate formative scores to exam "
        "scores from associated to none, because they concern how well the assessments predict, not learning); and "
        f"findings on workload ({workload} coded as well-being) were no longer counted as well-being. The "
        + ("design-choice evidence" if a.get("kind") == "effectiveness" else "evidence of this paper") + " is what remained.")})
    agree = coder_agreement(store, run_id, protocol)
    if agree:
        out.append({"id": "A7", "group": "Aim", "text": (
            "A second language-model agent then coded every finding again for the design choice, the link and the "
            "outcome, without seeing the first codes; agreement (Cohen's kappa) was "
            + "; ".join(f"{_label(k)} {v['kappa']:.2f}, with {v['agree']} of {v['n']} findings coded identically"
                        for k, v in agree.items() if v.get("kappa") is not None) + ". The first agent's codes are used.")})
    from . import recode
    other = recode.field_agreement(store, run_id, ["direction", "evidence_type"],
                                   works=tables.paper_set(store, run_id, protocol))
    if other:
        out.append({"id": "A10", "group": "Aim", "text": (
            "A second language-model agent also coded the direction and the kind of evidence of every finding again, "
            "without seeing the first codes; agreement (Cohen's kappa) was "
            + "; ".join(f"{_label(k)} {v['kappa']:.2f}, with {v['agree']} of {v['n']} findings coded identically"
                        for k, v in other.items() if v.get("kappa") is not None) + ". The first agent's codes are used.")})
    cats = [c for c in _categories(protocol, a["by"]) if c != "none"]
    out.append({"id": "A8", "group": "Aim", "text": (
        f"The {len(cats)} design-choice categories ({', '.join(_label(c) for c in cats)}) were written for the aim "
        "from the elements of programmatic assessment and the conditions bearing on its stakes that the protocol "
        "already charted; each finding was given one category, its main one, so a finding that touches several "
        "choices is counted once.")})
    # The lenses the patterns were predicted from, stated so the paper can name the theory it tests (approved cores).
    try:
        from . import lenses
        _, _, cfg = lenses.load(protocol.get("corpus", ""))
        cards = [f"{x['name']} ({x['role']}): " + " ".join(" ".join(str(c).split()) for c in x["core"])
                 for x in cfg.get("lenses") or [] if x.get("role") != "placebo"]
        if cards:
            out.append({"id": "A5", "group": "Aim", "text": (
                "The patterns and predictions were derived from rival explanations written into the protocol's lens "
                "file before the coding, each with a hard core, alongside a placebo lens that predicts nothing. "
                + " ".join(cards))})
    except (ValueError, OSError):
        pass
    human_screen = store.db.execute("SELECT count(DISTINCT work_id) FROM screening WHERE run_id = ? AND screener LIKE "
                                    "'human:%'", (run_id,)).fetchone()[0]
    from . import checks
    checks.ensure(store)
    checked = store.db.execute("SELECT count(DISTINCT finding_id) FROM finding_checks WHERE checker LIKE 'human:%' AND "
                               "finding_id IN (SELECT finding_id FROM findings WHERE run_id = ?)", (run_id,)).fetchone()[0]
    coded = store.db.execute("SELECT count(DISTINCT work_id) FROM coding_checks WHERE run_id = ?", (run_id,)).fetchone()[0] \
        if store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'coding_checks'").fetchone() else 0
    out.append({"id": "A4", "group": "Aim", "text": (
        "Verification was automated: every quote an agent gave was checked in code against the record's text, "
        "screening and eligibility used two models, the stakes coding a second coder, and every sentence of this "
        "paper was checked in code for its sources and numbers and audited by a separate agent. "
        + (f"The author screened {human_screen} records at title and abstract; " if human_screen else "")
        + (f"a person checked {checked} findings against their sources; " if checked else
           "no person has checked the findings against their sources; ")
        + (f"and a person coded {coded} studies blind for the stakes dimensions." if coded else
           "and no person has checked the coding.")).replace("; and", ", and")})
    return out
